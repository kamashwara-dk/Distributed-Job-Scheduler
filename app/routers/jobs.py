from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access import get_job, get_queue
from app.database import get_db
from app.models import Job, JobLog, JobStatus, User, new_id, utcnow
from app.pagination import PageParams, paginate
from app.schemas import BatchIn, JobIn
from app.security import get_current_user
from app.serialize import execution_out, job_out, log_out
from app.services.lifecycle import add_log, retry_terminal_job

router = APIRouter(tags=["jobs"])


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


@router.post("/queues/{queue_id}/jobs", status_code=201,
             summary="Create a job (immediate, delayed via delay_s, or scheduled via run_at)")
def create_job(queue_id: int, body: JobIn,
               user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_queue(db, user, queue_id)
    if body.idempotency_key:
        existing = db.scalar(select(Job).where(
            Job.queue_id == queue_id, Job.idempotency_key == body.idempotency_key
        ))
        if existing:  # idempotent create: return the original, don't duplicate
            return job_out(existing)
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
    get_queue(db, user, queue_id)
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
