# Job state transitions shared by the worker and the API.

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    DeadLetterJob,
    Job,
    JobExecution,
    JobLog,
    JobStatus,
    Queue,
    RetryPolicy,
    Worker,
    utcnow,
)
from app.services.retry import compute_backoff

# Fallback when a queue has no retry policy attached.
DEFAULT_POLICY = {"strategy": "exponential", "base_delay_s": 5.0, "max_delay_s": 300.0}


def add_log(db: Session, job_id: str, message: str, level: str = "info",
            execution_id: int | None = None) -> None:
    db.add(JobLog(job_id=job_id, execution_id=execution_id, level=level, message=message))


def start_execution(db: Session, job: Job, worker_id: str) -> JobExecution:
    job.status = JobStatus.RUNNING
    job.started_at = utcnow()
    execution = JobExecution(
        job_id=job.id, worker_id=worker_id, attempt=job.attempts + 1, status="running"
    )
    db.add(execution)
    db.flush()
    add_log(db, job.id, f"attempt {execution.attempt} started on worker {worker_id[:8]}",
            execution_id=execution.id)
    db.commit()
    return execution


def complete_execution(db: Session, job: Job, execution: JobExecution,
                       result: dict | None = None) -> None:
    now = utcnow()
    job.status = JobStatus.COMPLETED
    job.finished_at = now
    job.attempts += 1
    job.result = result
    job.last_error = None
    execution.status = "completed"
    execution.finished_at = now
    execution.result = result
    add_log(db, job.id, "completed", execution_id=execution.id)
    db.commit()


def fail_execution(db: Session, job: Job, execution: JobExecution, error: str) -> str:
    """Returns the resulting job status: 'queued' (will retry) or 'dead' (DLQ)."""
    now = utcnow()
    job.attempts += 1
    job.last_error = error
    execution.status = "failed"
    execution.finished_at = now
    execution.error = error
    add_log(db, job.id, f"attempt {job.attempts} failed: {error}", level="error",
            execution_id=execution.id)

    if job.attempts >= job.max_attempts:
        job.status = JobStatus.DEAD
        job.finished_at = now
        db.add(DeadLetterJob(
            job_id=job.id, queue_id=job.queue_id, job_type=job.type,
            payload=job.payload, attempts=job.attempts, last_error=error,
        ))
        add_log(db, job.id, f"moved to dead letter queue after {job.attempts} attempts",
                level="error")
        db.commit()
        return JobStatus.DEAD

    policy = _policy_for(db, job.queue_id)
    delay = compute_backoff(
        policy["strategy"], policy["base_delay_s"], policy["max_delay_s"], job.attempts
    )
    job.status = JobStatus.QUEUED
    job.run_at = now + timedelta(seconds=delay)
    job.claimed_by = None
    job.claimed_at = None
    add_log(db, job.id, f"retry scheduled in {delay:.0f}s ({policy['strategy']} backoff)")
    db.commit()
    return JobStatus.QUEUED


def _policy_for(db: Session, queue_id: int) -> dict:
    row = db.execute(
        select(RetryPolicy)
        .join(Queue, Queue.retry_policy_id == RetryPolicy.id)
        .where(Queue.id == queue_id)
    ).scalar_one_or_none()
    if row is None:
        return DEFAULT_POLICY
    return {
        "strategy": row.strategy,
        "base_delay_s": row.base_delay_s,
        "max_delay_s": row.max_delay_s,
    }


def reap_stale_workers(db: Session, stale_after_s: int) -> int:
    """Requeue jobs held by workers that stopped heartbeating (crash/kill -9).
    Doesn't burn an attempt — the job didn't fail, its worker did."""
    cutoff = utcnow() - timedelta(seconds=stale_after_s)
    stale = db.scalars(
        select(Worker).where(Worker.status == "online", Worker.last_heartbeat_at < cutoff)
    ).all()
    requeued = 0
    for w in stale:
        w.status = "offline"
        orphans = db.scalars(
            select(Job).where(
                Job.claimed_by == w.id,
                Job.status.in_([JobStatus.CLAIMED, JobStatus.RUNNING]),
            )
        ).all()
        for job in orphans:
            job.status = JobStatus.QUEUED
            job.claimed_by = None
            job.claimed_at = None
            job.run_at = utcnow()
            add_log(db, job.id, f"requeued: worker {w.id[:8]} went offline", level="warn")
            requeued += 1
    db.commit()
    return requeued


def retry_terminal_job(db: Session, job: Job) -> None:
    """Manual retry from the dashboard/API for dead or cancelled jobs."""
    job.status = JobStatus.QUEUED
    job.attempts = 0
    job.run_at = utcnow()
    job.claimed_by = None
    job.claimed_at = None
    job.finished_at = None
    job.last_error = None
    add_log(db, job.id, "manually requeued")
    db.commit()
