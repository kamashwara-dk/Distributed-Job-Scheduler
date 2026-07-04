import asyncio
import json
import logging
import os
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

IS_VERCEL = bool(os.environ.get("VERCEL"))

# ── Eager DB init ─────────────────────────────────────────────────────────────
# On Vercel (serverless), ASGI lifespan startup events are NOT triggered by the
# @vercel/python runtime.  Calling create_all() at module level ensures the
# tables exist when the first request arrives.  For local / Docker this is a
# harmless no-op because create_all() is idempotent.
try:
    Base.metadata.create_all(bind=engine)
    log.info("Database tables ensured (eager init)")
except Exception as e:
    log.error("Eager DB init failed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Still keep this for local uvicorn — it runs after the event loop starts.
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as e:
        log.error("Failed to create database tables: %s", e)
    yield


app = FastAPI(
    title="JobForge — Distributed Job Scheduler",
    description=(
        "A production-inspired distributed job scheduling platform: "
        "queues, atomic claiming, retries with backoff, cron schedules, "
        "dead letter queue, workers with heartbeats, and a live dashboard."
    ),
    version="1.0.0",
    lifespan=lifespan,
)
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


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    from fastapi.responses import Response
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 36 36">'
        '<rect width="36" height="36" rx="10" fill="#6366f1"/>'
        '<path d="M10 18l5 5 11-11" stroke="#fff" stroke-width="2.5" '
        'stroke-linecap="round" stroke-linejoin="round" fill="none"/>'
        '</svg>'
    )
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


# ── REST API ──────────────────────────────────────────────────────────────────

API = "/api/v1"
app.include_router(auth.router,      prefix=API)
app.include_router(orgs.router,      prefix=API)
app.include_router(queues.router,    prefix=API)
app.include_router(jobs.router,      prefix=API)   # includes /jobs/{id}/analysis
app.include_router(schedules.router, prefix=API)
app.include_router(workers.router,   prefix=API)
app.include_router(dlq.router,       prefix=API)
app.include_router(metrics.router,   prefix=API)


# ── WebSocket live feed ───────────────────────────────────────────────────────
# Pushes a compact metrics snapshot every 2 s.
# Auth via ?token=<jwt> — browsers cannot set WebSocket request headers.
# Close code 4001 = unauthenticated.

@app.websocket("/api/v1/ws/{project_id}")
async def ws_live(websocket: WebSocket, project_id: int, token: str | None = None):
    import jwt as pyjwt
    from datetime import timedelta
    from sqlalchemy import func, select

    from app.config import settings
    from app.models import Job, JobStatus, Queue, Worker, utcnow

    if not token:
        await websocket.close(code=4001)
        return
    try:
        payload = pyjwt.decode(token, settings.secret_key, algorithms=["HS256"])
        user_id = int(payload["sub"])
    except Exception:
        await websocket.close(code=4001)
        return

    await websocket.accept()
    log.info("WS open  project=%d user=%d", project_id, user_id)
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
        log.info("WS close project=%d user=%d", project_id, user_id)


# ── Static dashboard ──────────────────────────────────────────────────────────
# Served at / (must come LAST — after all API routes are registered).
# On Vercel: dashboard/ is bundled via includeFiles; /var/task/dashboard is the
# runtime fallback path when __file__ resolves inside the function sandbox.

_here = Path(__file__).resolve().parent        # .../app/
_dashboard = _here.parent / "dashboard"        # .../dashboard/

if not _dashboard.is_dir() and IS_VERCEL:
    _dashboard = Path("/var/task/dashboard")

if _dashboard.is_dir():
    app.mount("/", StaticFiles(directory=_dashboard, html=True), name="dashboard")
