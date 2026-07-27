"""Application settings.

Nothing here is required. The app must boot with an empty environment — only the
chat endpoint degrades when Vertex is unconfigured.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> parents[2] is the repo root (and /srv in the container,
# where compose mounts seed/ at /srv/seed).
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://northbridge:northbridge@localhost:5433/northbridge"

    seed_on_startup: bool = True
    seed_dir: Path = REPO_ROOT / "seed"

    # The single hardcoded user
    default_customer_id: str = "CUST-0001"

    # Bootstrap values for the policy engine. These are written into policy_rules
    # at seed time; the DB row is the runtime authority so /admin can edit it.
    refund_auto_approve_under_cents: int = 5000
    return_window_days: int = 30
    risk_score_approval_threshold: int = 70

    # Vertex AI — all optional
    vertex_project_id: str | None = None
    vertex_location: str = "us-central1"
    vertex_llm_model: str = "gemini-2.5-flash"
    vertex_embedding_model: str = "text-embedding-005"

    @property
    def vertex_configured(self) -> bool:
        return bool(self.vertex_project_id)

    def missing_vertex_vars(self) -> list[str]:
        """Names of the variables the operator still needs to set."""
        missing = []
        if not self.vertex_project_id:
            missing.append("VERTEX_PROJECT_ID")
        return missing


@lru_cache
def get_settings() -> Settings:
    return Settings()
