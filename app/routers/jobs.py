import re
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.access import get_job, get_queue
from app.database import get_db
from app.models import Job, JobExecution, JobLog, JobStatus, Queue, User, new_id, utcnow
from app.pagination import PageParams, paginate
from app.schemas import BatchIn, JobIn
from app.security import get_current_user
from app.serialize import execution_out, job_out, log_out
from app.services.lifecycle import add_log, retry_terminal_job

router = APIRouter(tags=["jobs"])


# ── helpers ──────────────────────────────────────────────────────────────────

def _enforce_rate_limit(db: Session, queue: Queue) -> None:
    """Sliding-window rate limit: reject if ≥ rate_limit_per_minute jobs were
    created in the last 60 seconds.  0 means unlimited (default)."""
    if not queue.rate_limit_per_minute:
        return
    since = utcnow() - timedelta(seconds=60)
    recent = db.scalar(
        select(func.count()).select_from(Job).where(
            Job.queue_id == queue.id,
            Job.created_at >= since,
        )
    )
    if recent >= queue.rate_limit_per_minute:
        raise HTTPException(
            429,
            f"Rate limit exceeded: queue '{queue.name}' allows "
            f"{queue.rate_limit_per_minute} job(s) per minute "
            f"(currently {recent} in the last 60 s).",
        )


def _build_job(queue_id: int, body: JobIn) -> Job:
    now = utcnow()
    run_at = now
    if body.run_at is not None:
        run_at = body.run_at.replace(tzinfo=None)
    elif body.delay_s:
        run_at = now + timedelta(seconds=body.delay_s)
    status = (JobStatus.SCHEDULED if run_at > now else JobStatus.QUEUED).value
    return Job(
        queue_id=queue_id, type=body.type, payload=body.payload,
        priority=body.priority, run_at=run_at, status=status,
        max_attempts=body.max_attempts, timeout_s=body.timeout_s,
        idempotency_key=body.idempotency_key,
    )


# ── job CRUD ──────────────────────────────────────────────────────────────────

@router.post("/queues/{queue_id}/jobs", status_code=201,
             summary="Create a job (immediate, delayed via delay_s, or scheduled via run_at)")
def create_job(queue_id: int, body: JobIn,
               user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    queue = get_queue(db, user, queue_id)
    if body.idempotency_key:
        existing = db.scalar(select(Job).where(
            Job.queue_id == queue_id, Job.idempotency_key == body.idempotency_key
        ))
        if existing:
            return job_out(existing)
    _enforce_rate_limit(db, queue)
    job = _build_job(queue_id, body)
    db.add(job)
    db.flush()
    add_log(db, job.id, f"created ({job.status})")
    db.commit()
    return job_out(job)


@router.post("/queues/{queue_id}/jobs/batch", status_code=201,
             summary="Create a batch of jobs sharing one batch_id")
def create_batch(queue_id: int, body: BatchIn,
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    queue = get_queue(db, user, queue_id)
    _enforce_rate_limit(db, queue)
    batch_id = new_id()
    jobs = []
    for item in body.jobs:
        job = _build_job(queue_id, item)
        job.batch_id = batch_id
        db.add(job)
        jobs.append(job)
    db.commit()
    return {"batch_id": batch_id, "count": len(jobs), "items": [job_out(j) for j in jobs]}


@router.get("/queues/{queue_id}/jobs", summary="List jobs with filtering + pagination")
def list_jobs(queue_id: int, status: str | None = None, type: str | None = None,
              batch_id: str | None = None, page: PageParams = Depends(),
              user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_queue(db, user, queue_id)
    stmt = select(Job).where(Job.queue_id == queue_id)
    if status:
        stmt = stmt.where(Job.status == status)
    if type:
        stmt = stmt.where(Job.type == type)
    if batch_id:
        stmt = stmt.where(Job.batch_id == batch_id)
    stmt = stmt.order_by(Job.created_at.desc())
    return paginate(db, stmt, page, job_out)


# NOTE: /jobs/{job_id}/analysis must be registered BEFORE /jobs/{job_id}
# so the literal path segment "analysis" is matched first, not captured as
# a job_id value by the dynamic route below it.

@router.get("/jobs/{job_id}/analysis",
            summary="AI-style failure analysis — root cause, recommendation, retry trend")
def analyze_job(job_id: str,
                user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    """Pattern-based failure diagnosis.  No external API — works offline,
    zero latency, deterministic.  Categories: network_connectivity, timeout,
    rate_limited, auth_failure, upstream_error, data_format,
    resource_exhaustion, misconfiguration, simulated_failure, unknown."""
    job = get_job(db, user, job_id)

    executions = db.scalars(
        select(JobExecution).where(JobExecution.job_id == job_id)
        .order_by(JobExecution.attempt)
    ).all()
    logs = db.scalars(
        select(JobLog).where(JobLog.job_id == job_id).order_by(JobLog.ts)
    ).all()

    errors = [e.error or "" for e in executions if e.error]
    combined = (" ".join(errors) + " " + " ".join(l.message for l in logs)).lower()

    _PATTERNS = [
        (r"connection refused|econnrefused|cannot connect|connection reset",
         "network_connectivity", "Network / connectivity failure",
         "Verify the target service is reachable and its port is open. "
         "Consider adding a readiness check before the job runs."),
        (r"timeout|timed out|deadline exceeded",
         "timeout", "Execution timeout",
         "Increase timeout_s, or break the work into smaller sub-jobs."),
        (r"429|rate limit|too many requests|quota exceeded",
         "rate_limited", "Downstream rate limit / quota",
         "Add exponential backoff and lower throughput. "
         "Use rate_limit_per_minute on the queue to smooth dispatch."),
        (r"401|403|unauthorized|forbidden|permission denied|authentication",
         "auth_failure", "Authentication / authorization failure",
         "Verify credentials, API keys, and IAM roles. "
         "Secrets may have expired or been rotated."),
        (r"500|internal server error|service unavailable|503",
         "upstream_error", "Upstream service error (5xx)",
         "The downstream service returned a server error. "
         "Check its status page and retry with exponential backoff."),
        (r"json|parse|syntax error|invalid.*format|decode",
         "data_format", "Data / serialization error",
         "The payload or response could not be parsed. "
         "Validate the input schema and check for upstream API changes."),
        (r"memory|oom|killed|out of memory",
         "resource_exhaustion", "Memory / resource exhaustion",
         "Reduce batch size or increase worker memory allocation."),
        (r"no handler|handler not found|unknown.*type",
         "misconfiguration", "Handler not registered",
         "Deploy the handler on the worker and restart it."),
        (r"simulated|flaky|destined to fail",
         "simulated_failure", "Simulated / test failure",
         "This handler is designed to fail for demo purposes. "
         "It will succeed on a later attempt."),
    ]

    category, title, recommendation = "unknown", "Unknown failure", \
        "Inspect the error message and execution logs for more detail."
    for pattern, cat, ttl, rec in _PATTERNS:
        if re.search(pattern, combined):
            category, title, recommendation = cat, ttl, rec
            break

    # Retry gap trend (are delays escalating as expected with backoff?)
    retry_delays = []
    for i in range(1, len(executions)):
        prev, curr = executions[i - 1], executions[i]
        if prev.finished_at and curr.started_at:
            retry_delays.append(
                round((curr.started_at - prev.finished_at).total_seconds(), 1)
            )

    trend = (
        "escalating" if len(retry_delays) >= 2 and retry_delays[-1] > retry_delays[0]
        else "stable" if retry_delays
        else "no_retries"
    )

    return {
        "job_id": job_id,
        "job_type": job.type,
        "status": job.status,
        "total_attempts": job.attempts,
        "analysis": {
            "category": category,
            "title": title,
            "recommendation": recommendation,
            "retry_trend": trend,
            "retry_delays_s": retry_delays,
            "unique_errors": list(dict.fromkeys(e for e in errors if e))[:5],
            "confidence": "high" if category != "unknown" else "low",
        },
    }


@router.get("/jobs/{job_id}", summary="Job detail with executions and logs")
def job_detail(job_id: str, user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    job = get_job(db, user, job_id)
    logs = db.scalars(
        select(JobLog).where(JobLog.job_id == job_id).order_by(JobLog.ts)
    ).all()
    return {
        **job_out(job),
        "executions": [execution_out(e) for e in job.executions],
        "logs": [log_out(l) for l in logs],
    }


@router.post("/jobs/{job_id}/cancel", summary="Cancel a job that hasn't started")
def cancel_job(job_id: str, user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    job = get_job(db, user, job_id)
    if job.status not in (JobStatus.QUEUED, JobStatus.SCHEDULED):
        raise HTTPException(409, f"Cannot cancel a job in status '{job.status}'")
    job.status = JobStatus.CANCELLED
    job.finished_at = utcnow()
    add_log(db, job.id, "cancelled by user")
    db.commit()
    return job_out(job)


@router.post("/jobs/{job_id}/retry", summary="Requeue a dead or cancelled job")
def retry_job(job_id: str, user: User = Depends(get_current_user),
              db: Session = Depends(get_db)):
    job = get_job(db, user, job_id)
    if job.status not in (JobStatus.DEAD, JobStatus.CANCELLED, JobStatus.COMPLETED):
        raise HTTPException(409, f"Cannot retry a job in status '{job.status}'")
    retry_terminal_job(db, job)
    return job_out(job)
