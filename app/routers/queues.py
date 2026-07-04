from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.access import get_project, get_queue
from app.database import get_db
from app.models import Job, JobExecution, JobStatus, Queue, User, utcnow
from app.schemas import QueueIn, QueueUpdate
from app.security import get_current_user
from app.serialize import queue_out

router = APIRouter(tags=["queues"])


@router.post("/projects/{project_id}/queues", status_code=201, summary="Create a queue")
def create_queue(project_id: int, body: QueueIn,
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_project(db, user, project_id)
    queue = Queue(project_id=project_id, **body.model_dump())
    db.add(queue)
    db.commit()
    return queue_out(queue)


@router.get("/projects/{project_id}/queues", summary="List queues in a project")
def list_queues(project_id: int, user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    get_project(db, user, project_id)
    queues = db.scalars(
        select(Queue).where(Queue.project_id == project_id).order_by(Queue.priority.desc())
    ).all()
    return {"items": [queue_out(q) for q in queues]}


@router.patch("/queues/{queue_id}", summary="Update queue configuration")
def update_queue(queue_id: int, body: QueueUpdate,
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    queue = get_queue(db, user, queue_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(queue, field, value)
    db.commit()
    return queue_out(queue)


@router.post("/queues/{queue_id}/pause", summary="Pause a queue (workers stop claiming)")
def pause_queue(queue_id: int, user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    queue = get_queue(db, user, queue_id)
    queue.paused = True
    db.commit()
    return queue_out(queue)


@router.post("/queues/{queue_id}/resume", summary="Resume a paused queue")
def resume_queue(queue_id: int, user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    queue = get_queue(db, user, queue_id)
    queue.paused = False
    db.commit()
    return queue_out(queue)


@router.get("/queues/{queue_id}/stats", summary="Queue statistics")
def queue_stats(queue_id: int, user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    queue = get_queue(db, user, queue_id)
    counts = dict(db.execute(
        select(Job.status, func.count()).where(Job.queue_id == queue_id).group_by(Job.status)
    ).all())
    hour_ago = utcnow() - timedelta(hours=1)
    completed_last_hour = db.scalar(
        select(func.count()).select_from(Job).where(
            Job.queue_id == queue_id,
            Job.status == JobStatus.COMPLETED,
            Job.finished_at >= hour_ago,
        )
    )
    # average successful execution duration over the last hour
    durations = db.execute(
        select(JobExecution.started_at, JobExecution.finished_at)
        .join(Job, Job.id == JobExecution.job_id)
        .where(
            Job.queue_id == queue_id,
            JobExecution.status == "completed",
            JobExecution.finished_at >= hour_ago,
        )
    ).all()
    avg_duration = (
        round(sum((f - s).total_seconds() for s, f in durations) / len(durations), 2)
        if durations else None
    )
    return {
        "queue": queue_out(queue),
        "counts": {s.value: counts.get(s.value, 0) for s in JobStatus},
        "depth": counts.get(JobStatus.QUEUED, 0) + counts.get(JobStatus.SCHEDULED, 0),
        "active": counts.get(JobStatus.CLAIMED, 0) + counts.get(JobStatus.RUNNING, 0),
        "completed_last_hour": completed_last_hour,
        "avg_duration_s": avg_duration,
    }
