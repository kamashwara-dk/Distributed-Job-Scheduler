# Design Decisions

This document records every major decision and its trade-offs in plain language so the design can be understood and defended, not just demonstrated.

---

## 1. The Problem

Applications constantly have work that shouldn't happen inside an HTTP request: sending emails, generating reports, calling flaky third-party APIs, processing uploads. This platform lets an application hand that work off as a job and takes over everything that makes background work hard:

- Running each job **exactly once**, even with dozens of workers competing
- **Retrying failures** with configurable backoff strategies
- **Quarantining** jobs that will never succeed into a dead letter queue
- **Surviving worker crashes** — a killed worker loses nothing
- **Controlling throughput** — per-queue rate limits and concurrency caps
- Giving operators **full visibility** into what's running, what failed, and why

---

## 2. Job Lifecycle (the state machine)

```
                   run_at in future
  create ──────────────────────────► SCHEDULED ──┐
     │                                           │ (run_at due)
     └────────────────────────────────────────► QUEUED ◄────────────────────┐
                                                  │                         │
                                           atomic claim                 retry with
                                                  ▼                      backoff
                                               CLAIMED                      │
                                                  │                         │
                                                  ▼            no  ┌────────┴──────┐
                                               RUNNING ──────────► │ attempts ≥    │
                                                  │     fail       │ max_attempts? │
                                             success               └────────┬──────┘
                                                  ▼                         │ yes
                                             COMPLETED                    DEAD ──► DLQ
```

Plus **CANCELLED** (user aborts a job that hasn't started).

**Why each state exists:**

- `SCHEDULED` — delayed/future jobs. A separate state (not just a future `run_at` on queued) means backlog metrics can distinguish "waiting on purpose" from "workers are behind".
- `QUEUED` — the only state workers ever take jobs from. Clean invariant.
- `CLAIMED` — worker holds the job, handler not yet started. Distinct from `RUNNING` so a worker that dies between claim and start is detectable and recoverable by the reaper.
- `RUNNING` — a handler is executing; a `job_executions` row is open. The reaper can distinguish a crashed worker from a slow one by checking `claimed_at`.
- `COMPLETED` / `DEAD` / `CANCELLED` — terminal states. `DEAD` additionally snapshots the job into `dead_letter_jobs`.

A failed attempt goes back to `QUEUED` with `run_at = now + backoff`, so retries reuse the exact same claiming machinery as new jobs. **One code path, not two.**

---

## 3. The Hard Problem: Atomic Claiming

**Requirement:** N workers polling the same queue must never execute the same job twice.

### PostgreSQL (production)

```sql
UPDATE jobs
SET status = 'claimed', claimed_by = :worker, claimed_at = :now
WHERE id IN (
    SELECT id FROM jobs
    WHERE queue_id = :qid AND status = 'queued' AND run_at <= :now
    ORDER BY priority DESC, run_at ASC
    LIMIT :n
    FOR UPDATE SKIP LOCKED
)
RETURNING id
```

`FOR UPDATE SKIP LOCKED` makes competing claims disjoint — a worker that loses a race skips to the next available row instead of blocking. This is the standard PostgreSQL queue pattern, used by Graphile Worker, Solid Queue, and Sidekiq-PG.

### SQLite (local dev / tests)

SQLite has no `SKIP LOCKED`. Instead:

```python
# SELECT candidates, then CAS each row
UPDATE jobs SET status='claimed', ... WHERE id=? AND status='queued'
# rowcount == 1 means WE won; racers see rowcount == 0
```

The `WHERE status='queued'` re-check is the compare-and-set. Two racers over the same row: exactly one sees `rowcount == 1`. Correct, just O(candidates) round-trips instead of one statement.

**Verified by:** `tests/test_claiming.py` — 4 threads race over 30 jobs; every job is claimed exactly once. Also verified live with 2 Docker workers and a SQL assertion: no completed job has ≠ 1 successful execution.

---

## 4. Coordination Through the Database — No Message Broker

The classic answer is Redis or RabbitMQ. Rejected for this project because:

1. **Splits the source of truth.** Broker knows the queue; DB knows the history. Keeping them consistent under failures requires outbox-style machinery (transactional outbox pattern) — more complexity, not less.

2. **Second stateful service.** Another thing to run, monitor, back up, and secure. PostgreSQL's row locking is already present and already reliable.

3. **At this scale, Postgres wins.** `FOR UPDATE SKIP LOCKED` gives correct, contention-free claiming with zero additional infrastructure.

**Costs accepted:**
- Workers poll (1 s interval) instead of being pushed work
- The DB is the eventual scaling bottleneck

Both are acceptable at this scale. The claiming logic is fully isolated in `services/claiming.py` — swapping in a broker later would not touch handlers, the API, or the lifecycle service.

---

## 5. Retry Strategies and Dead Letter Queue

**Per-project retry policies** define: strategy + base delay + cap + max retries. Attached per queue; individual jobs can override `max_attempts`.

| Strategy | Formula | When to use |
|---|---|---|
| `fixed` | `base` | Transient errors where time doesn't matter |
| `linear` | `base × attempt` | Rate-limited APIs where you want steady backoff |
| `exponential` | `base × 2^(attempt-1)`, capped | Default — transient failures (rate limits, restarts) need more room each time |

**Dead Letter Queue design:**
- After `max_attempts`, the job status becomes `DEAD` and a `dead_letter_jobs` snapshot is created
- Snapshot copies `job_type` and `payload` — **not** a reference to `jobs` — so the DLQ survives job archival and is independently auditable
- One API call (`POST /dlq/{id}/retry`) requeues the job with `attempts = 0`
- The AI analysis endpoint (`GET /jobs/{id}/analysis`) surfaces likely root cause and recommended action

**Idempotency:** clients can pass `idempotency_key`; a unique DB constraint on `(queue_id, idempotency_key)` makes create-retries safe without application-level deduplication logic.

---

## 6. Crash Recovery (Heartbeats + Reaper)

Workers heartbeat every **5 seconds**. A maintenance tick (every worker, every 2 s) checks for workers whose last heartbeat was > 30 s ago and requeues their `claimed`/`running` jobs.

**Key design choices:**

- **The reaper does not increment `attempts`.** The job didn't fail — its worker died. An infrastructure death shouldn't push a job toward the DLQ. Attempts count handler failures, not infrastructure failures.

- **Graceful shutdown** (`SIGTERM`) is the complement: stop claiming, drain running handlers (25 s grace window), then mark offline. `docker compose stop` therefore loses nothing; `docker kill` (or an OOM) loses at most 30 s before the reaper recovers.

- **Every worker runs maintenance**, not a designated leader. No leader election needed — both operations (reaping + cron) are CAS-guarded, so racing workers produce the same result.

---

## 7. Exactly-Once Cron

Every worker runs the scheduler tick, so materializing a cron occurrence must be race-safe:

```python
# CAS: only the winner's rowcount is 1
UPDATE scheduled_jobs
SET next_run_at = <next>, last_enqueued_at = <now>
WHERE id = :id AND next_run_at = <the value I read>
```

Only the worker whose `next_run_at` still matches the value it read wins the race and enqueues the job. Others see `rowcount = 0` and skip. No leader election, no distributed lock — the same trick as job claiming, applied to time advancement.

---

## 8. Rate Limiting

Per-queue **sliding window** rate limit:

```python
# Reject if ≥ rate_limit_per_minute jobs were created in the last 60 s
recent = COUNT(*) FROM jobs WHERE queue_id=? AND created_at >= now()-60s
if recent >= limit: raise HTTP 429
```

Why a sliding window over a fixed window:
- Fixed windows allow bursts at boundaries (e.g., 100 jobs at 00:59 and 100 more at 01:00)
- A sliding 60-second window provides a smoother rate guarantee
- Implementation is a single SQL aggregate — no Redis, no token bucket state

`rate_limit_per_minute = 0` disables the check entirely (default).

---

## 9. RBAC

Two roles within each organization:

| Role | Structural ops | Operational ops |
|---|---|---|
| `owner` | ✅ create/update/delete queues, projects, schedules, retry policies | ✅ all of member |
| `member` | ❌ | ✅ create/cancel jobs, pause/resume queues, retry DLQ, view everything |

Role is stored on `org_members.role`. The `require_owner()` function in `access.py` raises **403** (not 404) when an authenticated member tries an owner-only operation — because at that point the resource's existence is already established.

---

## 10. WebSocket Live Feed

The dashboard uses a dual-track update strategy:
- **REST polling** every 3 s for full table data (jobs, queues, workers)
- **WebSocket push** every 2 s for the lightweight metrics snapshot (status counts, worker count)

The WebSocket endpoint authenticates via `?token=<jwt>` query param because browser WebSocket APIs cannot set custom headers. Connection closes with code `4001` on invalid or missing tokens.

---

## 11. AI Failure Analysis

The analysis endpoint (`GET /jobs/{id}/analysis`) provides structured root-cause diagnosis using **deterministic pattern matching** against the job's error messages and log lines.

Benefits over an LLM API call:
- Zero latency (no network hop), zero cost, works offline
- Deterministic — same error always gets the same category
- No data leaves the system — job payloads and errors may contain PII

Pattern categories: `network_connectivity`, `timeout`, `rate_limited`, `auth_failure`, `upstream_error`, `data_format`, `resource_exhaustion`, `misconfiguration`, `simulated_failure`, `unknown`.

The endpoint also computes retry trend (escalating / stable / no_retries) from execution timestamps, giving operators an at-a-glance picture of whether backoff is working.

---

## 12. Decision Ledger

| Decision | Alternative | Why this one |
|---|---|---|
| Postgres + `SKIP LOCKED` | Redis/RabbitMQ | One source of truth, transactional transitions, no second stateful service |
| Separate worker process | Background threads in API | Independent scaling; a wedged handler can't take down the API |
| Poll-based workers (1 s) | `LISTEN/NOTIFY` push | Simpler, portable to SQLite, 1 s latency is fine for background jobs |
| Retry = requeue with future `run_at` | Separate retry table | One claiming code path handles both new jobs and retries |
| DLQ as snapshot table | Status flag on `jobs` | Survives job archival; independent audit + requeue surface |
| Reaper doesn't burn attempts | Count crash as failure | Infrastructure failure ≠ job failure; crashes shouldn't push jobs to DLQ |
| CAS for cron firing | Leader election | No coordination service; any worker can die and cron still fires |
| UUID PKs for jobs + workers | Auto-increment ints | No sequence coordination across replicas; non-enumerable in URLs |
| 404 for cross-tenant access | 403 | 403 confirms the resource exists — existence is itself information |
| Sliding window rate limit | Redis token bucket | No additional infrastructure; one SQL aggregate |
| Pattern matching for AI analysis | LLM API | Zero latency, zero cost, offline, deterministic, no data egress |
| `create_all` at startup | Alembic migrations | Project scope; noted as first change for real production |
| Threads per worker | `asyncio` workers | Handlers are sync (I/O-bound); threads keep handler code simple |
| Vanilla JS dashboard | React/Vue | Zero build step, unbreakable demo, the complexity lives in the backend |

---

## 13. Known Limitations and Next Steps

| Limitation | Mitigation / Status |
|---|---|
| Per-job execution timeout | **Implemented**: enforced via `concurrent.futures.Future.result(timeout=)` in `worker/runner.py`. A timed-out job is failed and retried with the queue's backoff policy. |
| Batch rate limiting bypass | **Fixed**: `create_batch` checks `recent + len(body.jobs) > limit`, not just `recent >= limit`. |
| Handlers are trusted code in the worker image | Sandboxing with a subprocess or microVM boundary |
| Single-database ceiling | Shard by queue — `queue_id` is the natural shard key; claiming logic is isolated |
| Metrics computed on read | **Improved**: throughput binning uses DB-side `GROUP BY` instead of pulling all timestamps to Python. Emit to Prometheus/OpenTelemetry for production-grade observability. |
| `create_all` instead of Alembic | Add Alembic for schema migration tracking — first change for real production |
| No workflow dependencies (job A waits for job B) | Add a `depends_on` field + a scheduler tick to unblock waiting jobs |
| Internal error details leaked in 500 responses | **Fixed**: `str(exc)` only included in `details` when `DEBUG=1`; scrubbed in production |
| Default `SECRET_KEY` deployed silently | **Fixed**: startup warning emitted whenever the dev-only default is detected |
| `Session.bind` deprecated in SQLAlchemy 2.x | **Fixed**: `claiming.py` now uses `db.get_bind().dialect.name` |
| Org members could not be invited | **Implemented**: `POST /orgs/{id}/members`, `GET /orgs/{id}/members`, `DELETE /orgs/{id}/members/{user_id}` |
| WebSocket `onmessage` dropped data | **Fixed**: stat cards updated directly from WebSocket push payload |
