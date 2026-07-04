import os
from pydantic_settings import BaseSettings, SettingsConfigDict

# On Vercel there is no .env file and the filesystem is read-only.
# Skip the env_file lookup entirely to avoid a startup warning/error.
_on_vercel = bool(os.environ.get("VERCEL"))

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Only load .env locally; on Vercel all values come from env vars
        # set in vercel.json or the Vercel dashboard.
        env_file=None if _on_vercel else ".env",
        extra="ignore",
    )

    # On Vercel: in-memory SQLite (no persistent filesystem).
    # Locally / Docker: file-based SQLite or Postgres via DATABASE_URL env var.
    database_url: str = "sqlite:///:memory:" if _on_vercel else "sqlite:///./scheduler.db"
    secret_key: str = "dev-only-secret-change-in-production-0123456789"
    access_token_expire_minutes: int = 720

    worker_stale_after_s: int = 30
    heartbeat_retention_s: int = 3600


settings = Settings()
