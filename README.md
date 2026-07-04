# JobForge — Distributed Job Scheduler

A **production-inspired distributed job scheduling platform** built with FastAPI, SQLAlchemy, and a vanilla JS dashboard. Designed to demonstrate every hard problem in background job processing: atomic claiming, configurable retries, crash recovery, cron scheduling, multi-tenancy, RBAC, rate limiting, and live observability.

---

## Feature Overview

| Category | Features |
|---|---|
| **Auth & Multi-tenancy** | JWT auth · bcrypt passwords · Organizations → Projects hierarchy · RBAC (owner / member roles) · Cross-tenant 404 isolation |
| **Queue Management** | Priority queues · Concurrency limits · Per-queue rate limiting (sliding window) · Pause / Resume · Retry policy attachment |
| **Job Types** | Immediate · Delayed (`delay_s`) · Scheduled (`run_at`) · Recurring cron · Batch (up to 500 per request) · Idempotent creation |
| **Job Lifecycle** | `QUEUED → CLAIMED → RUNNING → COMPLETED` with full state machine, per-attempt execution history, and structured logs |
| **Retry Strategies** | Fixed · Linear · Exponential backoff — all capped, all per-queue configurable |
| **Reliability** | Atomic claiming (`FOR UPDATE SKIP LOCKED` / SQLite CAS) · Exactly-once cron (CAS on `next_run_at`) · Crash recovery (heartbeats + reaper, no attempt burned) · Graceful shutdown (SIGTERM drain) |
| **Dead Letter Queue** | Permanent failures snapshotted independently · One-click requeue via API or dashboard |
| **Observability** | Live dashboard (WebSocket feed + REST polling) · Per-queue stats · 30-min throughput chart · Worker heartbeat monitoring · AI failure analysis |
| **API** | 30+ REST endpoints · Pydantic validation · Uniform error envelope · Pagination · Filtering · Interactive Swagger at `/docs` |
| **Testing** | 24 tests — race-condition atomicity, retry math, lifecycle transitions, DLQ, auth, tenancy, idempotency, pagination |

---

## Quick Start

### Docker (recommended)

```bash
docker compose up --scale worker=2
python scripts/seed_demo.py
```

Open **http://localhost:8000** and sign in with `demo@example.com` / `demo1234`.

### Without Docker (SQLite, zero setup)

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Terminal 1 — API + dashboard
uvicorn app.main:app --port 8000

# Terminal 2 — worker (start several to demo distributed execution)
python -m worker.main

# Terminal 3 — load demo data
python scripts/seed_demo.py
```

---

## Demo Walkthrough

After seeding, work through these in the dashboard:

**1. Overview tab**
Live throughput chart updates every 2 s via WebSocket. Queue backlog bars show depth per queue. Stat cards reflect real-time job counts.

**2. Jobs tab — click the `flaky` job**
Its log shows two failed attempts with exponential backoff (`2 s → 4 s`), then success on attempt 3. Full per-attempt execution history, timing, and log stream.

**3. Dead Letters tab**
The `always_fail` job exhausted its 3 retries and landed here. Click **Retry** — it requeues with `attempts = 0`. The **AI Analysis** panel in the job drawer explains the likely root cause and recommends action.

**4. Schedules tab**
A `* * * * *` cron entry fires a new job every minute — exactly once, even with 2 workers both running the scheduler tick. Watch `next_run_at` advance after each tick.

**5. Kill a worker**
```bash
docker kill <worker-container>    # not docker stop — simulate a crash
```
Within 30 s the reaper detects the missed heartbeat, requeues orphaned jobs, and marks the worker offline — without incrementing their attempt counter.

**6. Graceful shutdown**
```bash
docker compose stop worker        # sends SIGTERM
```
The worker finishes running jobs (up to 25 s), then deregisters cleanly. No jobs are lost.

**7. Rate limiting**
Update a queue's `rate_limit_per_minute` to 5, then try to enqueue 10 jobs quickly — the API returns `429` after the 5th.

---

## Architecture

```
┌─────────────────────┐         HTTP + JWT        ┌─────────────────────┐
│  Browser Dashboard  │ ────────────────────────► │   FastAPI API       │
│  WebSocket feed     │                            │   (stateless)       │
│  polls every 3 s    │                            └──────────┬──────────┘
└─────────────────────┘                                       │ SQLAlchemy
                                                   ┌──────────▼──────────┐
                                                   │   PostgreSQL / SQLite│
                                                   │   (coordination hub) │
                                                   └──────────┬──────────┘
                                                              │
                            ┌─────────────────────────────────┤
                     ┌──────▼──────┐  ┌─────────┴───┐  ┌────┴────────┐
                     │  Worker 1   │  │  Worker 2   │  │  Worker N   │
                     │ thread pool │  │ thread pool │  │ thread pool │
                     └─────────────┘  └─────────────┘  └─────────────┘
```

Workers share no state and never talk to each other or the API. Every distributed guarantee — single execution, exactly-once cron, crash recovery — is enforced with database primitives (row locks, CAS updates, unique constraints).

Full documentation:
- [Architecture](docs/architecture.md) — components, data flow, scaling story
- [ER Diagram](docs/er_diagram.md) — schema, keys, indexes, cascade behavior
- [Design Decisions](docs/DESIGN.md) — every major trade-off with alternatives considered
- [API Reference](docs/api.md) — all 30+ endpoints with request/response examples

---

## Project Structure

```
app/
  routers/       auth · orgs · queues · jobs · schedules · workers · dlq · metrics
  services/      claiming · retry backoff · lifecycle transitions · cron materialization
  models.py      SQLAlchemy schema (12 tables)
  schemas.py     Pydantic request models with validation
  access.py      Ownership-chain auth + RBAC enforcement
  security.py    JWT creation / verification, bcrypt
  main.py        FastAPI app, WebSocket feed, AI failure analysis

worker/
  main.py        WorkerService (3 loops: poll / heartbeat / maintenance)
  runner.py      Per-job execution with lifecycle bookkeeping
  handlers.py    Registered job handlers (send_email, http_request, flaky, etc.)

dashboard/       Vanilla JS SPA (no build step)
  index.html     Sidebar layout, drawer, all panels
  style.css      Professional dark theme, responsive
  app.js         API client, WebSocket, chart, AI analysis integration

tests/           24 pytest tests
docs/            Architecture · ER diagram · Design decisions · API reference
scripts/         Demo data seeder
```

---

## API

Interactive Swagger UI: **http://localhost:8000/docs**

Key endpoint groups:

| Prefix | Description |
|---|---|
| `/api/v1/auth/` | Register, login, current user |
| `/api/v1/orgs/` | Organizations and projects |
| `/api/v1/projects/{id}/queues` | Queue management |
| `/api/v1/queues/{id}/jobs` | Job creation and listing |
| `/api/v1/queues/{id}/jobs/batch` | Batch job creation |
| `/api/v1/jobs/{id}/` | Detail, cancel, retry, AI analysis |
| `/api/v1/queues/{id}/schedules` | Cron schedule management |
| `/api/v1/workers` | Worker registry |
| `/api/v1/projects/{id}/dlq` | Dead letter queue |
| `/api/v1/projects/{id}/metrics/overview` | Dashboard metrics |
| `WS /api/v1/ws/{project_id}` | Live metrics WebSocket feed |

---

## Running Tests

```bash
pip install -r requirements.txt
python -m pytest tests/ -v
```

24 tests covering:
- Race-condition claiming atomicity (4 threads, 30 jobs)
- Retry backoff math (fixed / linear / exponential)
- Complete lifecycle: fail → retry → DLQ → requeue
- Reaper behavior on stale workers
- Auth, JWT expiry, wrong credentials
- Cross-tenant isolation (users see 404 on others' resources)
- Idempotency key deduplication
- Batch job creation and batch_id filtering
- Pagination, queue pause/resume, cron validation

---

## Configuration

| Env var | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./scheduler.db` | PostgreSQL: `postgresql+psycopg://user:pass@host/db` |
| `SECRET_KEY` | `dev-only-secret-...` | JWT signing key — **change in production** |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `720` | JWT validity window |
| `WORKER_CONCURRENCY` | `4` | Threads per worker process |
| `WORKER_POLL_INTERVAL_S` | `1.0` | How often a worker checks for new jobs |
| `VERCEL` | unset | Set to `1` for Vercel deployment (in-memory SQLite) |

Copy `.env.example` to `.env` and update values for local development.

---

## Reliability Guarantees

| Guarantee | Mechanism |
|---|---|
| No duplicate execution | `FOR UPDATE SKIP LOCKED` (PG) / CAS UPDATE (SQLite) in a single transaction |
| Crash recovery | 30 s heartbeat staleness → reaper requeues orphans; no attempt burned |
| Retries with backoff | Fixed / linear / exponential, per-queue policy, capped delay |
| Exactly-once cron | CAS on `scheduled_jobs.next_run_at`; only one worker wins per tick |
| Dead letter queue | Permanent failures snapshotted, independently requeueable |
| Idempotent job creation | Unique `(queue_id, idempotency_key)` DB constraint |
| Rate limiting | 60 s sliding window count; 429 on breach; no external state |
| Graceful shutdown | SIGTERM → drain running jobs → deregister; `docker compose stop` loses nothing |
