# Executes one claimed job: lifecycle bookkeeping + handler dispatch.
# Each execution opens its own DB session (sessions aren't thread-safe).

import logging
import traceback

from app.database import SessionLocal
from app.models import Job
from app.services.lifecycle import (
    add_log,
    complete_execution,
    fail_execution,
    start_execution,
)
from worker.handlers import HANDLERS, HandlerContext

log = logging.getLogger("worker.runner")


def run_job(job_id: str, worker_id: str) -> None:
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None or job.status != "claimed" or job.claimed_by != worker_id:
            return  # reaped or cancelled between claim and start
        execution = start_execution(db, job, worker_id)

        def log_fn(message: str, level: str = "info"):
            add_log(db, job.id, message, level=level, execution_id=execution.id)
            db.commit()

        handler = HANDLERS.get(job.type)
        if handler is None:
            fail_execution(db, job, execution, f"no handler registered for type '{job.type}'")
            return
        try:
            ctx = HandlerContext(execution.attempt, job.timeout_s, log_fn)
            result = handler(job.payload or {}, ctx)
            complete_execution(db, job, execution, result)
            log.info("job %s completed (attempt %d)", job.id[:8], execution.attempt)
        except Exception as exc:
            db.rollback()
            # re-fetch: the failed handler may have left the session dirty
            job = db.get(Job, job_id)
            error = f"{type(exc).__name__}: {exc}"
            log.warning("job %s failed: %s", job.id[:8], error)
            log.debug(traceback.format_exc())
            fail_execution(db, job, execution, error)
    finally:
        db.close()
