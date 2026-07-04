import os
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_db_url() -> str:
    """
    Pick a sensible default database URL based on the runtime environment.
    - Vercel serverless: use in-memory SQLite (no persistent filesystem).
    - Everywhere else: use a local SQLite file (Docker / local dev / no .env).
    The DATABASE_URL env var always overrides this default.
    """
    if os.environ.get("VERCEL"):
        return "sqlite:///:memory:"
    return "sqlite:///./scheduler.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = _default_db_url()
    secret_key: str = "dev-only-secret-change-in-production-0123456789"
    access_token_expire_minutes: int = 720

    # A worker whose last heartbeat is older than this is considered dead;
    # its claimed/running jobs get requeued by the reaper.
    worker_stale_after_s: int = 30
    # Heartbeat rows older than this are pruned to keep the table small.
    heartbeat_retention_s: int = 3600


settings = Settings()
