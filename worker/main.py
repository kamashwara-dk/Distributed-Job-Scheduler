# Worker service: polls queues, claims jobs atomically, executes them in a
# thread pool, heartbeats, and shuts down gracefully on SIGTERM/SIGINT.
#
# Usage: python -m worker.main
# Env vars: WORKER_CONCURRENCY (default 4), WORKER_POLL_INTERVAL_S (default 1.0)

import logging
import os
import signal
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import delete

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.models import Worker, WorkerHeartbeat, utcnow
from app.services.claiming import claim_jobs
from app.services.cron import materialize_due
from app.services.lifecycle import reap_stale_workers
from worker.runner import run_job

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("worker")

CONCURRENCY = int(os.environ.get("WORKER_CONCURRENCY", "4"))
POLL_INTERVAL = float(os.environ.get("WORKER_POLL_INTERVAL_S", "1.0"))
HEARTBEAT_INTERVAL = 5.0
MAINTENANCE_INTERVAL = 2.0
SHUTDOWN_GRACE_S = 25


class WorkerService:
    def __init__(self):
        self.stop_event = threading.Event()
        self.in_flight: set[str] = set()
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=CONCURRENCY)
        db = SessionLocal()
        self.worker = Worker(
            hostname=socket.gethostname(), pid=os.getpid(), concurrency=CONCURRENCY
        )
        db.add(self.worker)
        db.commit()
        self.worker_id = self.worker.id
        db.close()
        log.info("worker %s online (concurrency=%d)", self.worker_id[:8], CONCURRENCY)

    # --- background threads -------------------------------------------------

    def heartbeat_loop(self):
        while not self.stop_event.wait(HEARTBEAT_INTERVAL):
            db = SessionLocal()
            try:
                w = db.get(Worker, self.worker_id)
                w.last_heartbeat_at = utcnow()
                db.add(WorkerHeartbeat(
                    worker_id=self.worker_id, running_jobs=len(self.in_flight)
                ))
                # prune old heartbeat rows so the table doesn't grow forever
                cutoff = utcnow() - timedelta(seconds=settings.heartbeat_retention_s)
                db.execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.ts < cutoff))
                db.commit()
            except Exception:
                log.exception("heartbeat failed")
                db.rollback()
            finally:
                db.close()

    def maintenance_loop(self):
        """Cron materialization + dead-worker reaping. Every worker runs this;
        both operations are CAS-guarded so racing workers can't double-fire."""
        while not self.stop_event.wait(MAINTENANCE_INTERVAL):
            db = SessionLocal()
            try:
                created = materialize_due(db)
                if created:
                    log.info("cron: enqueued %d job(s)", created)
                reaped = reap_stale_workers(db, settings.worker_stale_after_s)
                if reaped:
                    log.warning("reaper: requeued %d orphaned job(s)", reaped)
            except Exception:
                log.exception("maintenance tick failed")
                db.rollback()
            finally:
                db.close()

    # --- main loop ----------------------------------------------------------

    def _job_done(self, job_id: str):
        with self.lock:
            self.in_flight.discard(job_id)

    def poll_loop(self):
        while not self.stop_event.is_set():
            free = CONCURRENCY - len(self.in_flight)
            if free > 0:
                db = SessionLocal()
                try:
                    jobs = claim_jobs(db, self.worker_id, free)
                except Exception:
                    log.exception("claim failed")
                    db.rollback()
                    jobs = []
                finally:
                    db.close()
                for job in jobs:
                    with self.lock:
                        self.in_flight.add(job.id)
                    log.info("claimed job %s (%s)", job.id[:8], job.type)
                    fut = self.pool.submit(run_job, job.id, self.worker_id)
                    fut.add_done_callback(lambda _f, jid=job.id: self._job_done(jid))
            self.stop_event.wait(POLL_INTERVAL)

    def shutdown(self, *_):
        if self.stop_event.is_set():
            return
        log.info("shutdown requested: draining %d running job(s)...", len(self.in_flight))
        self.stop_event.set()
        db = SessionLocal()
        try:
            w = db.get(Worker, self.worker_id)
            w.status = "stopping"
            db.commit()
        finally:
            db.close()

    def run(self):
        signal.signal(signal.SIGTERM, self.shutdown)
        signal.signal(signal.SIGINT, self.shutdown)
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()
        threading.Thread(target=self.maintenance_loop, daemon=True).start()
        self.poll_loop()

        # graceful drain: let running handlers finish, then go offline
        deadline = time.monotonic() + SHUTDOWN_GRACE_S
        while self.in_flight and time.monotonic() < deadline:
            time.sleep(0.2)
        self.pool.shutdown(wait=False, cancel_futures=True)
        db = SessionLocal()
        try:
            w = db.get(Worker, self.worker_id)
            w.status = "offline"
            db.commit()
        finally:
            db.close()
        log.info("worker %s offline (drained=%s)", self.worker_id[:8], not self.in_flight)


if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)
    WorkerService().run()
