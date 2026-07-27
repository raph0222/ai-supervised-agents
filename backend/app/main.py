"""FastAPI entrypoint.

Startup must never fail on missing LLM config. The database is
required, Vertex is not. An unconfigured deployment boots, seeds, and serves
the API, only the chat path reports the missing variables.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import admin as admin_api
from app.api import chat as chat_api
from app.config import get_settings
from app.db.session import init_schema, session_scope
from app.seed.seeder import seed_if_empty, seed_knowledge

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("northbridge")

UI_URL = "http://localhost:5173"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    init_schema()
    if settings.seed_on_startup:
        with session_scope() as session:
            counts = seed_if_empty(session)
        log.info("seeded %s", counts) if counts else log.info("database already seeded")

        # Chunking is idempotent and credential-free, so it runs on every boot:
        # an edited policy document is picked up without a manual reseed.
        try:
            with session_scope() as session:
                log.info("knowledge: %s", seed_knowledge(session))
        except Exception:  # noqa: BLE001 - never let the corpus break startup
            log.exception("knowledge seeding failed; the app continues without RAG")

    if settings.vertex_configured:
        log.info("Vertex configured: model=%s", settings.vertex_llm_model)
    else:
        # Loud but not fatal — this is the documented degraded mode.
        log.warning(
            "Vertex not configured (%s). The app is fully usable except chat; "
            "/admin, seed data and logs all work.",
            ", ".join(settings.missing_vertex_vars()),
        )

    log.info("API ready. Open the UI at %s", UI_URL)

    yield


app = FastAPI(title="Northbridge Support Agent", version="1.0.0", lifespan=lifespan)

# Dev mode : the browser only ever talks to the Vite server, which
# proxies to this process server-side. Nothing is cross-origin.

app.include_router(chat_api.router)
app.include_router(admin_api.router)


@app.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "vertex_configured": settings.vertex_configured,
        "missing_vertex_vars": settings.missing_vertex_vars(),
        "single_user": settings.default_customer_id,
    }


@app.get("/")
def root() -> dict:
    return {
        "status": "ok",
        "ui": UI_URL,
        "api": ["/api/chat", "/api/stream", "/api/admin/pending", "/health", "/docs"],
    }
