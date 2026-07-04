import logging
import os
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("api.config")

# On Vercel there is no .env file and the filesystem is read-only.
# Skip the env_file lookup entirely to avoid a startup warning/error.
_on_vercel = bool(os.environ.get("VERCEL"))

_DEFAULT_SECRET = "dev-only-secret-change-in-production-0123456789"

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
    secret_key: str = _DEFAULT_SECRET
    access_token_expire_minutes: int = 720

    worker_stale_after_s: int = 30
    heartbeat_retention_s: int = 3600


settings = Settings()

# Emit a loud warning if the default insecure key is used outside of obvious
# dev/test contexts. This fires at import time so it appears in every
# startup log and is hard to miss.
if settings.secret_key == _DEFAULT_SECRET and not _on_vercel:
    log.warning(
        "⚠️  SECRET_KEY is set to the default development value. "
        "Set a strong, random SECRET_KEY in your environment before "
        "deploying to production. Tokens signed with this key are insecure."
    )
