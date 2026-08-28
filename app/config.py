"""Application configuration loaded from environment / .env.

All tunables (Gemini model, rate-limit pacing, JWT, storage & DB paths) live
here so they can be overridden via environment variables — which is how the
Docker / docker-compose deployment injects them.
"""
from __future__ import annotations

from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root = parent of the `app` package directory. Used to resolve the
# (possibly relative) SQLite path so the database ALWAYS lands under a known
# location regardless of the process working directory (uvicorn launch dir,
# Docker WORKDIR, systemd, etc.). This is what keeps the DB inside the mounted
# volume in Docker instead of leaking into an ephemeral layer.
_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---------- Google AI Studio ----------
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash-image"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/models"

    # ---------- Rate limiting (anti 429) ----------
    request_interval_seconds: float = 3.5   # forced sleep after each success
    semaphore_concurrency: int = 1          # single in-flight channel
    max_retries: int = 3                    # 429/5xx retries
    retry_backoff_seconds: str = "5,10,20"  # comma-separated backoff schedule

    # ---------- JWT ----------
    jwt_secret_key: str = "change-me-to-a-long-random-string"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 1440  # 24h

    # ---------- Storage & Database ----------
    storage_dir: str = "storage"
    uploads_dir: str = "storage/uploads"
    outputs_dir: str = "storage/outputs"
    data_dir: str = "data"
    sqlite_path: str = "data/app.db"

    @property
    def resolved_db_path(self) -> Path:
        """Absolute, canonical path to the SQLite file.

        A relative ``sqlite_path`` is resolved against the project root so the
        DB never accidentally lands outside the Docker-mounted ``/app/data``
        volume (which would make it ephemeral and wipe on container rebuild).
        """
        p = Path(self.sqlite_path)
        if not p.is_absolute():
            p = (_ROOT / p).resolve()
        return p

    @property
    def database_url(self) -> str:
        """Compose an async SQLite URL, creating the parent dir if needed."""
        p = self.resolved_db_path
        p.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{p}"

    @property
    def retry_backoff_list(self) -> List[float]:
        return [
            float(x) for x in self.retry_backoff_seconds.split(",") if x.strip()
        ]


settings = Settings()
