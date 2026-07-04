from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access import get_queue, get_schedule
from app.database import get_db
from app.models import ScheduledJob, User, utcnow
from app.schemas import ScheduleIn, ScheduleUpdate
from app.security import get_current_user
from app.serialize import schedule_out

router = APIRouter(tags=["recurring jobs"])


def _validate_cron(expr: str) -> None:
    if not croniter.is_valid(expr):
        raise HTTPException(422, f"Invalid cron expression: '{expr}'")


@router.post("/queues/{queue_id}/schedules", status_code=201,
             summary="Create a recurring (cron) job")
def create_schedule(queue_id: int, body: ScheduleIn,
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_queue(db, user, queue_id)
    _validate_cron(body.cron_expr)
    now = utcnow()
    sched = ScheduledJob(
        queue_id=queue_id, name=body.name, cron_expr=body.cron_expr,
        job_type=body.job_type, payload=body.payload, priority=body.priority,
        next_run_at=croniter(body.cron_expr, now).get_next(type(now)),
    )
    db.add(sched)
    db.commit()
    return schedule_out(sched)


@router.get("/queues/{queue_id}/schedules", summary="List recurring jobs on a queue")
def list_schedules(queue_id: int, user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    get_queue(db, user, queue_id)
    scheds = db.scalars(
        select(ScheduledJob).where(ScheduledJob.queue_id == queue_id)
    ).all()
    return {"items": [schedule_out(s) for s in scheds]}


@router.patch("/schedules/{schedule_id}", summary="Edit / enable / disable a schedule")
def update_schedule(schedule_id: int, body: ScheduleUpdate,
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    sched = get_schedule(db, user, schedule_id)
    data = body.model_dump(exclude_unset=True)
    if "cron_expr" in data:
        _validate_cron(data["cron_expr"])
        now = utcnow()
        sched.next_run_at = croniter(data["cron_expr"], now).get_next(type(now))
    for field, value in data.items():
        setattr(sched, field, value)
    db.commit()
    return schedule_out(sched)


@router.delete("/schedules/{schedule_id}", status_code=204, summary="Delete a schedule")
def delete_schedule(schedule_id: int, user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    sched = get_schedule(db, user, schedule_id)
    db.delete(sched)
    db.commit()
