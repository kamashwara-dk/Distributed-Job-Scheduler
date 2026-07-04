# Materialize recurring (cron) templates into real Job rows.
#
# Multiple workers all run this tick; the compare-and-set on next_run_at makes
# sure only ONE of them enqueues each occurrence: the UPDATE only matches if
# next_run_at still holds the old value, so exactly one racer wins.

from croniter import croniter
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Job, JobStatus, ScheduledJob, utcnow


def materialize_due(db: Session) -> int:
    now = utcnow()
    due = db.scalars(
        select(ScheduledJob).where(
            ScheduledJob.enabled.is_(True), ScheduledJob.next_run_at <= now
        )
    ).all()
    created = 0
    for sched in due:
        next_run = croniter(sched.cron_expr, now).get_next(type(now))
        res = db.execute(
            update(ScheduledJob)
            .where(
                ScheduledJob.id == sched.id,
                ScheduledJob.next_run_at == sched.next_run_at,  # CAS guard
            )
            .values(next_run_at=next_run, last_enqueued_at=now)
        )
        if res.rowcount == 1:  # we won: enqueue this occurrence
            db.add(Job(
                queue_id=sched.queue_id,
                type=sched.job_type,
                payload=sched.payload,
                priority=sched.priority,
                status=JobStatus.QUEUED,
                run_at=now,
            ))
            created += 1
    db.commit()
    return created
