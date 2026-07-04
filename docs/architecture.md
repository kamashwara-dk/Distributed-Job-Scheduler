# Architecture

## System Overview

JobForge is a **distributed job scheduling platform** built on three independently deployable components that share a single database as their coordination point. No message broker, no shared in-process state — the database is the queue, the lock, and the audit log.

```
┌─────────────────────────────────────────────────────────────────────┐
│  Client Layer                                                       │
│                                                                     │
│  ┌──────────────────┐   ┌──────────────────────────────────────┐   │
│  │  Browser / SPA   │   │  External apps (REST API consumers)  │   │
│  │  polls every 3s  │   │  job producers / status checkers     │   │
│  │  WebSocket feed  │   │                                      │   │
│  └────────┬─────────┘   └──────────────┬───────────────────────┘   │
└───────────┼──────────────────────────  │  ──────────────────────────┘
            │ HTTP + JWT                 │ HTTP + JWT
            ▼                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│  API Layer (horizontally scalable, stateless)                       │
│                                                                     │
│  ┌────────────────────────────────────────────────────────────┐    │
│  │  FastAPI  (uvicorn)                                        │    │
│  │  • auth / orgs / projects / queues / jobs / schedules     │    │
│  │  • metrics / DLQ / workers                                │    │
│  │  • WebSocket live feed  (/api/v1/ws/{project_id})         │    │
│  │  • AI failure analysis  (/api/v1/jobs/{id}/analysis)      │    │
│  │  • static dashboard     (/)                               │    │
│  └─────────────────────────────┬──────────────────────────────┘    │
└────────────────────────────────┼────────────────────────────────────┘
                                 │ SQLAlchemy (ORM + raw SQL)
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Database  (single source of truth)                                 │
│                                                                     │
│  PostgreSQL (production) · SQLite (local dev / CI tests)            │
│                                                                     │
│  12 tables — users, orgs, projects, retry policies, queues, jobs,  │
│  job_executions, job_logs, workers, worker_heartbeats,             │
│  scheduled_jobs, dead_letter_jobs                                   │
└─────────────────────────────────────────────────────────────────────┘
                                 ▲
                    SQL (claim / heartbeat / cron CAS)
          ┌──────────────────────┼───────────────────────┐
          │                      │                       │
  ┌───────┴──────┐      ┌────────┴─────┐      ┌─────────┴────┐
  │   Worker 1   │      │   Worker 2   │      │   Worker N   │
  │  thread pool │      │  thread pool │  ···  │  thread pool │
  └──────────────┘      └──────────────┘      └──────────────┘
```

---

## Components

### API Server (`app/`)

A **stateless** FastAPI process. Because it holds no in-process state, any number of replicas can run behind a load balancer.

Responsibilities:
- **Authentication** — JWT-based, bcrypt password hashing, configurable expiry
- **Multi-tenancy** — org → project → queue → job ownership chain; every read/write validates membership; cross-tenant access returns 404 (not 403)
- **RBAC** — `owner` role required for structural changes (create/update/delete queues, schedules); `member` role sufficient for operational work (create jobs, pause/resume, retry DLQ)
- **REST API** — 30+ endpoints, Pydantic validation, limit/offset pagination, uniform error envelope
- **Rate limiting** — per-queue token bucket: rejects `POST /jobs` when more than `rate_limit_per_minute` jobs were created in the last 60 seconds
- **WebSocket feed** — live metrics push at `/api/v1/ws/{project_id}` (auth via `?token=` query param)
- **AI failure analysis** — pattern-based root-cause classification at `/api/v1/jobs/{id}/analysis`
- **Static dashboard** — serves `dashboard/` at `/`

### Worker Service (`worker/`)

A **separate process** (`python -m worker.main`), deliberately not part of the API. Isolation means a wedged handler can't take down the API, and workers scale independently.

Each worker runs **three concurrent loops**:

| Loop | Interval | Responsibility |
|---|---|---|
| `poll_loop` | 1 s | Promote due scheduled jobs → claim up to `concurrency` jobs → dispatch to thread pool |
| `heartbeat_loop` | 5 s | Update `workers.last_heartbeat_at` + write a `worker_heartbeats` row; prune old rows |
| `maintenance_loop` | 2 s | Materialize due cron jobs (CAS-guarded); reap stale workers (requeue orphaned jobs) |

Graceful shutdown on `SIGTERM`/`SIGINT`:
1. Stop the poll loop (no new claims)
2. Mark worker as `stopping`
3. Drain running handlers up to 25 s
4. Mark worker as `offline`

### Database

The **coordination point** for all distributed guarantees — no separate broker needed. See [Design Decisions §4](DESIGN.md) for the full rationale.

---

## Data Flow: Life of a Job

```
1.  POST /api/v1/queues/{id}/jobs
    └── validated + written as jobs row
        status = QUEUED  (or SCHEDULED if run_at is in the future)

2.  Worker poll tick (every 1 s)
    └── promote_due_scheduled()  → flip SCHEDULED → QUEUED when run_at ≤ now
    └── claim_jobs()             → atomic claim (SKIP LOCKED / CAS)
        status = CLAIMED, claimed_by = worker_id

3.  Thread pool picks up the job
    └── start_execution()        → status = RUNNING, job_executions row opened
    └── handler(payload, ctx)    → runs; ctx.log() writes job_logs rows

4a. Handler succeeds
    └── complete_execution()     → status = COMPLETED, result stored

4b. Handler raises an exception
    └── fail_execution()
        ├── attempts < max_attempts  → status = QUEUED, run_at = now + backoff
        └── attempts ≥ max_attempts  → status = DEAD, snapshot → dead_letter_jobs

5.  Dashboard / API consumer
    └── GET /jobs/{id}  or  WS feed  → reads status, logs, executions in real time
```

---

## Concurrency Control

Every distributed guarantee is enforced in exactly one place:

| Problem | Mechanism | Location |
|---|---|---|
| Two workers claim the same job | `FOR UPDATE SKIP LOCKED` (PG) / CAS `UPDATE … WHERE status='queued'` (SQLite) | `services/claiming.py` |
| Queue concurrency limit | Claim count capped at `concurrency_limit − active` inside the claim transaction | `services/claiming.py` |
| Cron fires exactly once with N workers | CAS on `scheduled_jobs.next_run_at` — only the winner's rowcount is 1 | `services/cron.py` |
| Worker crash mid-job | Heartbeat staleness (30 s) triggers reaper → requeues orphans without burning an attempt | `services/lifecycle.py` |
| Client retries job creation | Unique `(queue_id, idempotency_key)` constraint; return-existing on collision | `routers/jobs.py` |
| Queue rate limit | 60-second sliding window count before insert; 429 on breach | `routers/jobs.py` |

---

## Scaling Story

```
Workers:    purely horizontal — docker compose up --scale worker=N
            SKIP LOCKED means more workers never block each other

API:        stateless; N replicas behind any load balancer

Database:   the eventual bottleneck. Mitigation path:
            1. Composite claim index (already present)
            2. Archive completed jobs (move to cold table)
            3. Partition jobs table by status + time range
            4. Shard queues across database instances
               (queue_id is the natural shard key)
```
