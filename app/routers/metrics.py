from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.access import get_project
from app.config import settings
from app.database import get_db
from app.models import Job, JobStatus, Queue, User, Worker, utcnow
from app.security import get_current_user

router = APIRouter(tags=["metrics"])


@router.get("/projects/{project_id}/metrics/overview",
            summary="Dashboard overview: status counts, per-queue depth, throughput")
def overview(project_id: int, user: User = Depends(get_current_user),
             db: Session = Depends(get_db)):
    get_project(db, user, project_id)
    queue_ids = list(db.scalars(select(Queue.id).where(Queue.project_id == project_id)))
    now = utcnow()

    counts = dict(db.execute(
        select(Job.status, func.count())
        .where(Job.queue_id.in_(queue_ids))
        .group_by(Job.status)
    ).all()) if queue_ids else {}

    # completed-per-minute buckets for the last 30 minutes (drives the chart)
    window_start = now - timedelta(minutes=30)
    finished = db.execute(
        select(Job.finished_at).where(
            Job.queue_id.in_(queue_ids),
            Job.status == JobStatus.COMPLETED,
            Job.finished_at >= window_start,
        )
    ).all() if queue_ids else []
    buckets = [0] * 30
    for (ts,) in finished:
        idx = int((now - ts).total_seconds() // 60)
        if 0 <= idx < 30:
            buckets[29 - idx] += 1

    per_queue = db.execute(
        select(Queue.id, Queue.name, Queue.paused, func.count(Job.id))
        .outerjoin(Job, (Job.queue_id == Queue.id) & Job.status.in_(
            [JobStatus.QUEUED, JobStatus.SCHEDULED]))
        .where(Queue.project_id == project_id)
        .group_by(Queue.id, Queue.name, Queue.paused)
    ).all() if queue_ids else []

    stale_cutoff = now - timedelta(seconds=settings.worker_stale_after_s)
    online_workers = db.scalar(
        select(func.count()).select_from(Worker).where(
            Worker.status == "online", Worker.last_heartbeat_at >= stale_cutoff
        )
    )

    return {
        "counts": {s.value: counts.get(s.value, 0) for s in JobStatus},
        "throughput_per_min": buckets,
        "queues": [
            {"id": qid, "name": name, "paused": paused, "depth": depth}
            for qid, name, paused, depth in per_queue
        ],
        "online_workers": online_workers,
        "generated_at": now.isoformat() + "Z",
    }
