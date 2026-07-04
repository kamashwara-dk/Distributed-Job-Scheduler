from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./scheduler.db"
    secret_key: str = "dev-only-secret-change-in-production-0123456789"
    access_token_expire_minutes: int = 720

    # A worker whose last heartbeat is older than this is considered dead;
    # its claimed/running jobs get requeued by the reaper.
    worker_stale_after_s: int = 30
    # Heartbeat rows older than this are pruned to keep the table small.
    heartbeat_retention_s: int = 3600


settings = Settings()
