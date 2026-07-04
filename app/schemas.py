from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models import RetryStrategy


class RegisterIn(BaseModel):
    email: str = Field(min_length=3, max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    email: str
    password: str


class OrgIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""


class RetryPolicyIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    strategy: RetryStrategy = RetryStrategy.EXPONENTIAL
    base_delay_s: float = Field(5.0, gt=0, le=86400)
    max_delay_s: float = Field(300.0, gt=0, le=86400)
    max_retries: int = Field(3, ge=0, le=50)


class QueueIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    priority: int = Field(0, ge=-100, le=100)
    concurrency_limit: int = Field(10, ge=1, le=1000)
    retry_policy_id: int | None = None


class QueueUpdate(BaseModel):
    priority: int | None = Field(None, ge=-100, le=100)
    concurrency_limit: int | None = Field(None, ge=1, le=1000)
    retry_policy_id: int | None = None


class JobIn(BaseModel):
    type: str = Field(min_length=1, max_length=120)
    payload: dict = {}
    priority: int = Field(0, ge=-100, le=100)
    # exactly one way to defer: an absolute time OR a relative delay
    run_at: datetime | None = None
    delay_s: int | None = Field(None, ge=0, le=30 * 86400)
    max_attempts: int = Field(4, ge=1, le=51)
    timeout_s: int | None = Field(None, ge=1, le=3600)
    idempotency_key: str | None = Field(None, max_length=255)

    @field_validator("delay_s")
    @classmethod
    def not_both(cls, v, info):
        if v is not None and info.data.get("run_at") is not None:
            raise ValueError("give run_at or delay_s, not both")
        return v


class BatchIn(BaseModel):
    jobs: list[JobIn] = Field(min_length=1, max_length=500)


class ScheduleIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    cron_expr: str = Field(min_length=1, max_length=120)
    job_type: str = Field(min_length=1, max_length=120)
    payload: dict = {}
    priority: int = Field(0, ge=-100, le=100)


class ScheduleUpdate(BaseModel):
    enabled: bool | None = None
    cron_expr: str | None = None
    payload: dict | None = None
    priority: int | None = Field(None, ge=-100, le=100)
