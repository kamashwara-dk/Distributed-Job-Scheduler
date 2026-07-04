import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from app.database import Base, engine
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
