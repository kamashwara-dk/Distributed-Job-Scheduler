from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Job, JobStatus, User, Worker, utcnow
from app.security import get_current_user
from app.serialize import worker_out

router = APIRouter(tags=["workers"])


@router.get("/workers", summary="List workers with liveness derived from heartbeats")
def list_workers(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    now = utcnow()
    workers = db.scalars(select(Worker).order_by(Worker.started_at.desc())).all()
    running = dict(db.execute(
        select(Job.claimed_by, Job.id).where(Job.status == JobStatus.RUNNING)
    ).all())
    items = []
    for w in workers:
        age = (now - w.last_heartbeat_at).total_seconds()
        data = worker_out(w, settings.worker_stale_after_s, age)
        data["running_jobs"] = sum(1 for wid in running if wid == w.id)
        items.append(data)
    return {"items": items}
