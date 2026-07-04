# Executes one claimed job: lifecycle bookkeeping + handler dispatch.
# Each execution opens its own DB session (sessions aren't thread-safe).
#
# timeout_s enforcement: uses a concurrent.futures.ThreadPoolExecutor to run
# the handler in a sub-thread with a real wall-clock deadline.  On timeout the
# handler thread is abandoned (Python can't forcibly kill threads, but the
# worker slot is freed immediately and the job is marked failed so the retry
# machinery takes over).

import logging
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

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

# One-thread pool per job execution — avoids sharing state between the outer
# worker pool and the timeout sub-thread.
_TIMEOUT_EXECUTOR = ThreadPoolExecutor(thread_name_prefix="job-timeout")


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
            timeout = job.timeout_s  # None means no limit

            if timeout:
                # Run the handler in a sub-thread so we can apply a deadline.
                future = _TIMEOUT_EXECUTOR.submit(handler, job.payload or {}, ctx)
                try:
                    result = future.result(timeout=timeout)
                except FutureTimeout:
                    error = f"Job timed out after {timeout}s (timeout_s={timeout})"
                    log.warning("job %s timed out", job.id[:8])
                    db.rollback()
                    job = db.get(Job, job_id)
                    fail_execution(db, job, execution, error)
                    return
            else:
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
