"""Vertex AI text embeddings — same credentials as the LLM.

Lazy throughout: the module imports with an empty environment and only touches
the Vertex SDK when an embedding is requested. Missing credentials raise a typed
exception naming the variables, not an SDK stack trace.
"""

from __future__ import annotations

import logging

from app.config import get_settings
from app.db.models import EMBEDDING_DIM

log = logging.getLogger(__name__)

# Vertex caps a single embed request; batching is on us. 16 keeps each request
# well inside the token limit for policy-sized chunks.
BATCH_SIZE = 16

_model = None  # cached TextEmbeddingModel


class MissingCredentials(RuntimeError):
    """Raised instead of letting the SDK fail obscurely."""


def is_configured() -> bool:
    return get_settings().vertex_configured


def require_configured() -> None:
    settings = get_settings()
    if not settings.vertex_configured:
        raise MissingCredentials(
            f"{' / '.join(settings.missing_vertex_vars())} not set in .env — "
            "embeddings and chat are unavailable. Everything else still works."
        )


def _get_model():
    global _model
    if _model is not None:
        return _model

    require_configured()
    settings = get_settings()

    import vertexai  # noqa: PLC0415 - deliberately lazy
    from vertexai.language_models import TextEmbeddingModel  # noqa: PLC0415

    vertexai.init(project=settings.vertex_project_id, location=settings.vertex_location)
    _model = TextEmbeddingModel.from_pretrained(settings.vertex_embedding_model)
    log.info(
        "vertex embeddings ready: model=%s project=%s location=%s",
        settings.vertex_embedding_model,
        settings.vertex_project_id,
        settings.vertex_location,
    )
    return _model


def reset_cache() -> None:
    """Drop the cached client — used after settings change in a long process."""
    global _model
    _model = None


def _embed(texts: list[str], task_type: str) -> list[list[float]]:
    from vertexai.language_models import TextEmbeddingInput  # noqa: PLC0415

    model = _get_model()
    out: list[list[float]] = []
    for start in range(0, len(texts), BATCH_SIZE):
        batch = [
            TextEmbeddingInput(text=t, task_type=task_type)
            for t in texts[start : start + BATCH_SIZE]
        ]
        try:
            results = model.get_embeddings(batch, output_dimensionality=EMBEDDING_DIM)
        except TypeError:
            # Older SDKs do not accept output_dimensionality; text-embedding-005
            # returns 768 dimensions by default, which is what the column is.
            results = model.get_embeddings(batch)
        out.extend(r.values for r in results)

    bad = [len(v) for v in out if len(v) != EMBEDDING_DIM]
    if bad:
        raise RuntimeError(
            f"embedding dimension mismatch: got {bad[0]}, column expects "
            f"{EMBEDDING_DIM}. Check VERTEX_EMBEDDING_MODEL."
        )
    return out


def embed_documents(texts: list[str]) -> list[list[float]]:
    """Embed corpus chunks. RETRIEVAL_DOCUMENT is the indexing-side task type."""
    return _embed(texts, "RETRIEVAL_DOCUMENT")


def embed_query(text: str) -> list[float]:
    """Embed a user question. The query task type is asymmetric with the
    document one on purpose — Vertex trains them as a pair."""
    return _embed([text], "RETRIEVAL_QUERY")[0]
