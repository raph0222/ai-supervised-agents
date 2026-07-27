"""Vertex AI Gemini client.

The SDK is imported inside the call so an unconfigured deployment still boots;
the failure is a typed `LlmUnavailable` naming the missing variables.
`generate_json` asks Vertex for `application/json` and parses defensively. Every
call lands in `llm_calls` with tokens, latency and cost.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import models as m

log = logging.getLogger(__name__)

# Vertex list prices, USD per million tokens. Estimate, for the cost panel only.
PRICING_PER_MTOK = {
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
}
_DEFAULT_PRICE = (0.10, 0.40)

_MAX_TOKENS_FINISH = 2
RETRY_BUDGET_FACTOR = 2

_model_cache: dict[str, object] = {}


class LlmUnavailable(RuntimeError):
    """Vertex is not configured. Carries the operator-facing message."""


@dataclass
class LlmResponse:
    text: str
    agent: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    time_to_first_token_ms: int | None = None
    raw_json: dict | None = None
    meta: dict = field(default_factory=dict)
    # The model ran out of budget mid-answer, so `text` stops mid-token.
    truncated: bool = False

    @property
    def cost_micros(self) -> int:
        return estimate_cost_micros(self.model, self.input_tokens, self.output_tokens)


def is_configured() -> bool:
    return get_settings().vertex_configured


def require_configured() -> None:
    settings = get_settings()
    if not settings.vertex_configured:
        raise LlmUnavailable(
            f"{' / '.join(settings.missing_vertex_vars())} not set in .env — "
            "the chat agents need Vertex AI. Seed data, orders, policies, logs "
            "and /admin all work without it."
        )


def estimate_cost_micros(model: str, input_tokens: int, output_tokens: int) -> int:
    in_price, out_price = PRICING_PER_MTOK.get(model, _DEFAULT_PRICE)
    dollars = (input_tokens * in_price + output_tokens * out_price) / 1_000_000
    return int(round(dollars * 1_000_000))


def _get_model(system_instruction: str | None):
    require_configured()
    settings = get_settings()
    key = f"{settings.vertex_llm_model}|{hash(system_instruction)}"
    if key in _model_cache:
        return _model_cache[key]

    import vertexai  # noqa: PLC0415 - deliberately lazy
    from vertexai.generative_models import GenerativeModel  # noqa: PLC0415

    vertexai.init(project=settings.vertex_project_id, location=settings.vertex_location)
    model = GenerativeModel(
        settings.vertex_llm_model,
        system_instruction=system_instruction or None,
    )
    _model_cache[key] = model
    return model


def reset_cache() -> None:
    _model_cache.clear()


# ----------------------------------------------------------------------


def generate(
    prompt: str,
    *,
    system: str | None = None,
    agent: str = "unknown",
    temperature: float = 0.2,
    max_output_tokens: int = 1024,
    json_mode: bool = False,
    session: Session | None = None,
    conversation_id: str | None = None,
) -> LlmResponse:
    """One non-streaming completion. Records the call when a session is given."""
    from vertexai.generative_models import GenerationConfig  # noqa: PLC0415

    settings = get_settings()
    model = _get_model(system)
    config = GenerationConfig(
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        response_mime_type="application/json" if json_mode else "text/plain",
    )

    started = time.perf_counter()
    try:
        raw = model.generate_content(prompt, generation_config=config)
    except Exception as exc:  # noqa: BLE001 - boundary; the caller degrades
        _record(session, agent, settings.vertex_llm_model, 0, 0,
                int((time.perf_counter() - started) * 1000), None,
                conversation_id, ok=False, error=str(exc)[:250])
        raise

    latency_ms = int((time.perf_counter() - started) * 1000)
    usage = getattr(raw, "usage_metadata", None)
    truncated = _truncated(raw)
    response = LlmResponse(
        text=_text_of(raw),
        agent=agent,
        model=settings.vertex_llm_model,
        input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
        output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
        latency_ms=latency_ms,
        truncated=truncated,
    )
    if truncated:
        log.warning(
            "%s hit max_output_tokens (%s): %s output tokens of %s total — "
            "raise the budget for this call",
            agent, max_output_tokens, response.output_tokens,
            getattr(usage, "total_token_count", "?"),
        )
    _record(session, agent, response.model, response.input_tokens,
            response.output_tokens, latency_ms, None, conversation_id,
            ok=not truncated,
            error="truncated at max_output_tokens" if truncated else None)
    return response


def generate_json(
    prompt: str,
    *,
    system: str | None = None,
    agent: str = "unknown",
    temperature: float = 0.0,
    max_output_tokens: int = 1024,
    session: Session | None = None,
    conversation_id: str | None = None,
) -> LlmResponse:
    """A completion the caller will read as JSON.

    Temperature defaults to 0: routing and planning are classification tasks, and
    a creative router is a routing bug.

    Retries once at a larger budget when the answer was cut off. Thinking tokens
    are charged to `max_output_tokens`, so an unusually long deliberation leaves
    too few tokens for the JSON and the caller gets a half-written object, a
    parse failure that no amount of prompt wording prevents. Every caller's
    fallback for unparseable JSON is user-visible, so it is worth one more call.
    """
    response = generate(
        prompt,
        system=system,
        agent=agent,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        json_mode=True,
        session=session,
        conversation_id=conversation_id,
    )
    response.raw_json = parse_json(response.text)

    if response.raw_json is None and response.truncated:
        log.info("%s: retrying at %s output tokens after truncation",
                 agent, max_output_tokens * RETRY_BUDGET_FACTOR)
        response = generate(
            prompt,
            system=system,
            agent=agent,
            temperature=temperature,
            max_output_tokens=max_output_tokens * RETRY_BUDGET_FACTOR,
            json_mode=True,
            session=session,
            conversation_id=conversation_id,
        )
        response.raw_json = parse_json(response.text)

    return response


def stream(
    prompt: str,
    *,
    system: str | None = None,
    agent: str = "unknown",
    temperature: float = 0.3,
    max_output_tokens: int = 1024,
    session: Session | None = None,
    conversation_id: str | None = None,
) -> Iterator[str]:
    """Yield text chunks. Time to first token is recorded."""
    from vertexai.generative_models import GenerationConfig  # noqa: PLC0415

    settings = get_settings()
    model = _get_model(system)
    config = GenerationConfig(temperature=temperature, max_output_tokens=max_output_tokens)

    started = time.perf_counter()
    ttft: int | None = None
    usage = None
    try:
        for piece in model.generate_content(prompt, generation_config=config, stream=True):
            if ttft is None:
                ttft = int((time.perf_counter() - started) * 1000)
            usage = getattr(piece, "usage_metadata", usage)
            text = _text_of(piece)
            if text:
                yield text
    except Exception as exc:  # noqa: BLE001
        _record(session, agent, settings.vertex_llm_model, 0, 0,
                int((time.perf_counter() - started) * 1000), ttft,
                conversation_id, ok=False, error=str(exc)[:250])
        raise

    _record(
        session, agent, settings.vertex_llm_model,
        getattr(usage, "prompt_token_count", 0) or 0,
        getattr(usage, "candidates_token_count", 0) or 0,
        int((time.perf_counter() - started) * 1000), ttft, conversation_id,
    )


# ----------------------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_json(text: str) -> dict | None:
    """Best-effort JSON extraction.

    Almost always a plain `json.loads`, but a code-fenced answer should not take
    an agent down. Callers treat `None` as "off-contract" and fall back.
    """
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    if (fenced := _FENCE.search(text)) is not None:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _text_of(raw) -> str:
    try:
        return raw.text or ""
    except Exception:  # noqa: BLE001 - blocked or empty candidate
        return ""


def _truncated(raw) -> bool:
    """True when the model ran out of budget mid-answer."""
    try:
        return int(raw.candidates[0].finish_reason) == _MAX_TOKENS_FINISH
    except Exception:  # noqa: BLE001 - no candidate, nothing to judge
        return False


def _record(
    session: Session | None,
    agent: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    ttft_ms: int | None,
    conversation_id: str | None,
    *,
    ok: bool = True,
    error: str | None = None,
) -> None:
    if session is None:
        return
    try:
        session.add(
            m.LlmCall(
                conversation_id=conversation_id,
                agent=agent,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_micros=estimate_cost_micros(model, input_tokens, output_tokens),
                latency_ms=latency_ms,
                time_to_first_token_ms=ttft_ms,
                ok=ok,
                error=error,
            )
        )
        session.flush()
    except Exception:  # noqa: BLE001 - metrics must never break a conversation
        log.exception("failed to record llm call")
