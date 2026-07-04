JobForge — Distributed Job Scheduling Platform

A production-inspired distributed job scheduler built on FastAPI with independent worker
services and PostgreSQL. Features atomic job claiming, configurable retries,
cron scheduling, a dead letter queue, worker heartbeats, and a live dashboard.

Quick start (Docker — recommended)

```bash
docker compose up --scale worker=2
```

Then:

```bash
python scripts/seed_demo.py
```

Open http://localhost:8000 and log in with `demo@example.com` / `demo1234`.
Interactive API docs (Swagger) are available at http://localhost:8000/docs

Quick start (no Docker)

Runs on SQLite out of the box:

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# terminal 1 — API + dashboard
uvicorn app.main:app --port 8000

# terminal 2 — a worker (start several to go distributed)
python -m worker.main

# terminal 3 — demo data
python scripts/seed_demo.py
```

What to look at in the demo

1. Overview tab — live throughput chart, queue backlog, workers online.
2. Jobs tab — click the `flaky` job: its log shows two failed attempts with
   exponential backoff (2s → 4s), then success on attempt 3. Full execution
   history and per-attempt logs.
3. Dead Letters tab — the `always_fail` job landed here after exhausting
   retries; one click requeues it.
4. Schedules tab — a cron entry (`* * * * *`) materializes a new job every
   minute; exactly once, even with multiple workers running.
5. Kill a worker (`docker kill`, not `stop`) while jobs run — within ~30s
   the reaper detects the missed heartbeats and requeues its orphaned jobs.
6. `docker compose stop worker` (SIGTERM) — graceful shutdown: the worker
   stops claiming, finishes running jobs, then deregisters.

Architecture overview

```
        ┌────────────┐      HTTP/JSON       ┌──────────────┐
browser │  Dashboard │ ──────────────────►  │   FastAPI    │
        │ (static JS)│                      │   REST API   │
        └────────────┘                      └──────┬───────┘
                                                   │ SQL
                                            ┌──────▼───────┐
                                            │  PostgreSQL  │  single source of truth:
                                            │              │  jobs, queues, executions,
                                            └──────▲───────┘  logs, workers, DLQ
                                                   │ SQL (claim w/ SKIP LOCKED,
                                                   │      heartbeats, cron CAS)
                              ┌────────────────────┼────────────────────┐
                        ┌─────┴─────┐        ┌─────┴─────┐        ┌─────┴─────┐
                        │  Worker 1 │        │  Worker 2 │  ...   │  Worker N │
                        └───────────┘        └───────────┘        └───────────┘
```

The database is the coordination point — workers share no state and never talk
to each other or to the API. Full details, diagrams, and reasoning:

- docs/architecture.md — components and data flow
- docs/er_diagram.md — schema with keys and indexes
- docs/DESIGN.md — design decisions and trade-offs
- docs/api.md — API surface summary (live spec at /docs)

Job lifecycle

```
                      run_at in future
        create ──────────────────────────► SCHEDULED ──┐ (run_at due)
           │                                           ▼
           └────────────────────────────────────────► QUEUED ◄────────────┐
                                                        │                 │
                                                 atomic claim        retry with
                                                        ▼             backoff
                                                     CLAIMED               │
                                                        │                  │
                                                        ▼        no ┌──────┴──────┐
                                                     RUNNING ──────►│ attempts ≥  │
                                                        │   fail    │ max?        │
                                                   success          └──────┬──────┘
                                                        ▼                  │ yes
                                                    COMPLETED             DEAD ──► DLQ
```

Plus CANCELLED (user cancels a job that hasn't started) and manual
requeue from the DLQ.

Reliability guarantees

- No duplicate execution. Claims are atomic: FOR UPDATE SKIP LOCKED on
  PostgreSQL, compare-and-set updates on SQLite. Verified by a
  4-thread race test and by running 2 workers against the demo seed.
- Retries with backoff. Fixed / linear / exponential, per-queue policies,
  capped delays; every attempt recorded as a job_execution row.
- Dead letter queue. Permanent failures are snapshotted and requeueable.
- Crash recovery. Workers heartbeat every 5s; a reaper requeues jobs
  orphaned by dead workers (without burning an attempt).
- Cron exactly-once. Recurring jobs materialize via compare-and-set on
  next_run_at, so N workers can all run the scheduler tick safely.
- Idempotent creation. Optional idempotency_key per queue makes job
  creation safe to retry from the client side.

Tests

```bash
venv/bin/python -m pytest tests/ -v
```

24 tests cover the critical paths: backoff math, racing-worker claim
atomicity, concurrency limits, lifecycle (fail → retry → DLQ → requeue),
reaper behavior, auth, tenant isolation, idempotency, batches, pagination,
and validation.

Project structure

```
app/            FastAPI application
  routers/      REST endpoints (auth, orgs, queues, jobs, schedules, workers, dlq, metrics)
  services/     claiming, retry backoff, lifecycle transitions, cron materialization
  models.py     SQLAlchemy schema (12 tables)
worker/         standalone worker service (python -m worker.main)
dashboard/      static dashboard (vanilla JS, no build step)
tests/          pytest suite
scripts/        demo seeder
docs/           architecture, ER diagram, design decisions, API summary
```
