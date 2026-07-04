# Atomic job claiming — the heart of "never run the same job twice".
#
# Two paths, chosen by database dialect:
#
# * PostgreSQL: a single UPDATE whose candidate SELECT uses
#   FOR UPDATE SKIP LOCKED. Competing workers lock disjoint rows, so each job
#   is claimed exactly once, and workers never block each other.
#
# * SQLite (local dev / tests): SELECT candidates, then compare-and-set each
#   row (UPDATE ... WHERE id=:id AND status='queued'). The rowcount tells us
#   whether WE flipped the row; a racing worker's UPDATE finds status already
#   'claimed' and matches zero rows. Correct, just less scalable.

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.models import Job, JobStatus, Queue, utcnow

_PG_CLAIM = text(
    """
    UPDATE jobs
    SET status = 'claimed', claimed_by = :worker, claimed_at = :now, updated_at = :now
    WHERE id IN (
        SELECT id FROM jobs
        WHERE queue_id = :queue_id AND status = 'queued' AND run_at <= :now
        ORDER BY priority DESC, run_at ASC
        LIMIT :n
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id
    """
)


def promote_due_scheduled(db: Session) -> int:
    """Flip scheduled jobs whose run_at has arrived to queued.
    Idempotent — safe for every worker to run every poll."""
    res = db.execute(
        update(Job)
        .where(Job.status == JobStatus.SCHEDULED, Job.run_at <= utcnow())
        .values(status=JobStatus.QUEUED, updated_at=utcnow())
    )
    db.commit()
    return res.rowcount


def _active_counts(db: Session) -> dict[int, int]:
    rows = db.execute(
        select(Job.queue_id, func.count())
        .where(Job.status.in_([JobStatus.CLAIMED, JobStatus.RUNNING]))
        .group_by(Job.queue_id)
    ).all()
    return dict(rows)


def claim_jobs(db: Session, worker_id: str, max_jobs: int) -> list[Job]:
    """Claim up to max_jobs across all unpaused queues, honoring each queue's
    concurrency limit and priority order."""
    if max_jobs <= 0:
        return []
    promote_due_scheduled(db)

    queues = db.scalars(
        select(Queue).where(Queue.paused.is_(False)).order_by(Queue.priority.desc())
    ).all()
    active = _active_counts(db)
    claimed_ids: list[str] = []
    now = utcnow()

    for q in queues:
        want = min(max_jobs - len(claimed_ids), q.concurrency_limit - active.get(q.id, 0))
        if want <= 0:
            continue
        if db.bind.dialect.name == "postgresql":
            rows = db.execute(
                _PG_CLAIM, {"worker": worker_id, "now": now, "queue_id": q.id, "n": want}
            ).fetchall()
            db.commit()
            claimed_ids += [r[0] for r in rows]
        else:
            candidates = db.scalars(
                select(Job.id)
                .where(Job.queue_id == q.id, Job.status == JobStatus.QUEUED, Job.run_at <= now)
                .order_by(Job.priority.desc(), Job.run_at.asc())
                .limit(want)
            ).all()
            for job_id in candidates:
                res = db.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.status == JobStatus.QUEUED)
                    .values(
                        status=JobStatus.CLAIMED,
                        claimed_by=worker_id,
                        claimed_at=now,
                        updated_at=now,
                    )
                )
                if res.rowcount == 1:  # we won the race for this row
                    claimed_ids.append(job_id)
            db.commit()
        if len(claimed_ids) >= max_jobs:
            break

    if not claimed_ids:
        return []
    return list(db.scalars(select(Job).where(Job.id.in_(claimed_ids))).all())
