import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from app.database import Base, SessionLocal, engine
from app.errors import install_error_handlers
from app.routers import auth, dlq, jobs, metrics, orgs, queues, schedules, workers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # create_all is idempotent; fine for this project's scope.
    # A production deployment would use Alembic migrations instead.
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as e:
        log.error("Failed to create database tables: %s", e)
    yield


import os

IS_VERCEL = bool(os.environ.get("VERCEL"))

app_kwargs = {
    "title": "JobForge — Distributed Job Scheduler",
    "description": (
        "A production-inspired distributed job scheduling platform: "
        "queues, atomic claiming, retries with backoff, cron schedules, "
        "dead letter queue, workers with heartbeats, and a live dashboard."
    ),
    "version": "1.0.0",
    "lifespan": lifespan,
}

app = FastAPI(**app_kwargs)
install_error_handlers(app)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    if request.url.path.startswith("/api"):
        log.info("%s %s -> %s (%.1fms)", request.method, request.url.path,
                 response.status_code, (time.perf_counter() - start) * 1000)
    return response


@app.get("/api/health", tags=["health"])
def health():
    return {"status": "ok"}


# ── WebSocket live feed ────────────────────────────────────────────────────
# Clients subscribe to a project's metrics stream. The server pushes a compact
# snapshot every 2 seconds so the dashboard can update without polling.
# ws://host/api/v1/ws/{project_id}?token=<jwt>

@app.websocket("/api/v1/ws/{project_id}")
async def ws_live(websocket: WebSocket, project_id: int, token: str | None = None):
    from sqlalchemy import func, select
    from app.models import Job, JobStatus, Queue, Worker, utcnow
    from app.config import settings
    from datetime import timedelta
    import jwt as pyjwt

    # Authenticate via ?token= query param (browsers can't set WS headers)
    if token:
        try:
            payload = pyjwt.decode(token, settings.secret_key, algorithms=["HS256"])
            user_id = int(payload["sub"])
        except Exception:
            await websocket.close(code=4001)
            return
    else:
        await websocket.close(code=4001)
        return

    await websocket.accept()
    log.info("WS connected: project=%d user=%d", project_id, user_id)
    try:
        while True:
            db = SessionLocal()
            try:
                queue_ids = list(db.scalars(
                    select(Queue.id).where(Queue.project_id == project_id)
                ))
                counts = {}
                if queue_ids:
                    counts = dict(db.execute(
                        select(Job.status, func.count())
                        .where(Job.queue_id.in_(queue_ids))
                        .group_by(Job.status)
                    ).all())
                stale_cutoff = utcnow() - timedelta(seconds=settings.worker_stale_after_s)
                online = db.scalar(
                    select(func.count()).select_from(Worker).where(
                        Worker.status == "online",
                        Worker.last_heartbeat_at >= stale_cutoff,
                    )
                )
                snapshot = {
                    "type": "metrics",
                    "counts": {s.value: counts.get(s.value, 0) for s in JobStatus},
                    "online_workers": online,
                    "ts": utcnow().isoformat() + "Z",
                }
            finally:
                db.close()
            await websocket.send_text(json.dumps(snapshot))
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        log.info("WS disconnected: project=%d user=%d", project_id, user_id)


# ── AI failure analysis ────────────────────────────────────────────────────
# Analyses a job's error history and returns a structured diagnosis with
# likely root cause, category, and recommended action. Uses deterministic
# pattern matching — no external API needed, works offline, always fast.

@app.get("/api/v1/jobs/{job_id}/analysis", tags=["jobs"],
         summary="AI-style failure analysis with root cause and recommendation")
def analyze_job(job_id: str, request: Request):
    from fastapi import HTTPException
    from sqlalchemy import select
    from app.database import SessionLocal
    from app.models import Job, JobExecution, JobLog
    from app.security import get_current_user
    import re

    # lightweight auth without Depends (needed outside router)
    from fastapi.security import HTTPBearer
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated")
    token = auth_header[7:]
    from app.config import settings
    import jwt as pyjwt
    try:
        payload = pyjwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except Exception:
        raise HTTPException(401, "Invalid token")

    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        executions = db.scalars(
            select(JobExecution).where(JobExecution.job_id == job_id)
            .order_by(JobExecution.attempt)
        ).all()
        logs = db.scalars(
            select(JobLog).where(JobLog.job_id == job_id).order_by(JobLog.ts)
        ).all()
    finally:
        db.close()

    errors = [e.error or "" for e in executions if e.error]
    all_errors = " ".join(errors).lower()
    log_messages = " ".join(l.message for l in logs).lower()
    combined = all_errors + " " + log_messages

    # Pattern-based root cause classification
    patterns = [
        (r"connection refused|econnrefused|cannot connect|connection reset",
         "network_connectivity", "Network / connectivity failure",
         "Check that the target service is reachable and its port is open. "
         "Consider adding a health-check before the job runs."),
        (r"timeout|timed out|deadline exceeded",
         "timeout", "Execution timeout",
         "Increase timeout_s on the job, or optimize the handler to complete faster. "
         "Consider breaking the work into smaller sub-jobs."),
        (r"429|rate limit|too many requests|quota exceeded",
         "rate_limited", "Downstream rate limit / quota",
         "Add exponential backoff between retries and reduce throughput. "
         "Use a rate_limit_per_minute on the queue to throttle dispatch."),
        (r"401|403|unauthorized|forbidden|permission denied|authentication",
         "auth_failure", "Authentication / authorization failure",
         "Verify credentials, API keys, and IAM permissions. "
         "Secrets may have expired or been rotated."),
        (r"500|internal server error|service unavailable|503",
         "upstream_error", "Upstream service error",
         "The called service returned a 5xx error. Check its status page and logs. "
         "Retries with exponential backoff are appropriate here."),
        (r"json|parse|syntax error|invalid.*format|decode",
         "data_format", "Data / serialization error",
         "The payload or response could not be parsed. Validate input schema "
         "and check for API contract changes upstream."),
        (r"memory|oom|killed|out of memory",
         "resource_exhaustion", "Memory / resource exhaustion",
         "The job consumed too much memory. Reduce batch size or increase worker memory."),
        (r"no handler|handler not found|unknown.*type",
         "misconfiguration", "Handler not registered",
         "The job type has no registered handler on this worker. "
         "Deploy the handler and restart the worker."),
        (r"simulated|flaky|destined to fail",
         "simulated_failure", "Simulated / test failure",
         "This is a demo handler designed to fail. It will succeed on a later attempt."),
    ]

    category = "unknown"
    title = "Unknown failure"
    recommendation = "Inspect the error message and logs for more detail."
    for pattern, cat, ttl, rec in patterns:
        if re.search(pattern, combined):
            category = cat
            title = ttl
            recommendation = rec
            break

    # Retry trend analysis
    retry_delays = []
    if len(executions) > 1:
        for i in range(1, len(executions)):
            if executions[i-1].finished_at and executions[i].started_at:
                gap = (executions[i].started_at - executions[i-1].finished_at).total_seconds()
                retry_delays.append(round(gap, 1))

    trend = "escalating" if (
        len(retry_delays) >= 2 and retry_delays[-1] > retry_delays[0]
    ) else "stable" if retry_delays else "no_retries"

    unique_errors = list(dict.fromkeys(e for e in errors if e))

    return {
        "job_id": job_id,
        "job_type": job.type,
        "status": job.status,
        "total_attempts": job.attempts,
        "analysis": {
            "category": category,
            "title": title,
            "recommendation": recommendation,
            "retry_trend": trend,
            "retry_delays_s": retry_delays,
            "unique_errors": unique_errors[:5],
            "confidence": "high" if category != "unknown" else "low",
        },
    }


API = "/api/v1"
app.include_router(auth.router, prefix=API)
app.include_router(orgs.router, prefix=API)
app.include_router(queues.router, prefix=API)
app.include_router(jobs.router, prefix=API)
app.include_router(schedules.router, prefix=API)
app.include_router(workers.router, prefix=API)
app.include_router(dlq.router, prefix=API)
app.include_router(metrics.router, prefix=API)

# Dashboard (static files) served at /
# Resolve dashboard/ relative to this file's location (app/main.py → ../dashboard).
# On Vercel, dashboard/ is bundled inside the function via includeFiles in vercel.json,
# so this path resolves correctly both locally and in the serverless runtime.
_here = Path(__file__).resolve().parent          # .../app/
_dashboard = _here.parent / "dashboard"          # .../dashboard/  (local + Docker)

# Vercel unpacks the function bundle under /var/task; try that path as a fallback.
if not _dashboard.is_dir() and IS_VERCEL:
    _dashboard = Path("/var/task/dashboard")

if _dashboard.is_dir():
    app.mount("/", StaticFiles(directory=_dashboard, html=True), name="dashboard")
