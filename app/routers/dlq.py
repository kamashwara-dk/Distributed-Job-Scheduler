from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access import get_dlq_entry, get_project
from app.database import get_db
from app.models import DeadLetterJob, Job, JobStatus, Queue, User, utcnow
from app.pagination import PageParams, paginate
from app.security import get_current_user
from app.serialize import dlq_out
from app.services.lifecycle import retry_terminal_job

router = APIRouter(tags=["dead letter queue"])


@router.get("/projects/{project_id}/dlq", summary="List dead-lettered jobs")
def list_dlq(project_id: int, page: PageParams = Depends(),
             user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_project(db, user, project_id)
    stmt = (
        select(DeadLetterJob)
        .join(Queue, Queue.id == DeadLetterJob.queue_id)
        .where(Queue.project_id == project_id)
        .order_by(DeadLetterJob.failed_at.desc())
    )
    return paginate(db, stmt, page, dlq_out)


@router.post("/dlq/{entry_id}/retry", summary="Requeue a dead-lettered job")
def retry_dlq(entry_id: int, user: User = Depends(get_current_user),
              db: Session = Depends(get_db)):
    entry = get_dlq_entry(db, user, entry_id)
    if entry.requeued_at is not None:
        raise HTTPException(409, "Entry already requeued")
    job = db.get(Job, entry.job_id)
    if job is None or job.status != JobStatus.DEAD:
        raise HTTPException(409, "Original job is no longer in dead state")
    retry_terminal_job(db, job)
    entry.requeued_at = utcnow()
    db.commit()
    return dlq_out(entry)
