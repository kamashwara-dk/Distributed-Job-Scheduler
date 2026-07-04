Distributed Job Scheduler — Design Decisions

This document records every major decision and its trade-offs in plain language
so the design can be understood and defended, not just demonstrated.


1. The Problem

Applications constantly have work that shouldn't happen inside an HTTP
request: sending emails, generating reports, calling flaky third-party APIs.
This platform lets an application hand that work off as a job, and takes
over everything that makes background work hard: making sure each job runs
exactly one time even with many workers running, retrying failures with
increasing delays, quarantining jobs that will never succeed, surviving
worker crashes, and showing operators what's going on.


2. Job Lifecycle (the state machine)

SCHEDULED → QUEUED → CLAIMED → RUNNING → COMPLETED | (retry → QUEUED) | DEAD
plus CANCELLED for user aborts.

Why each state exists:

- SCHEDULED — delayed/future jobs. A separate state (not just a future
  run_at on queued) so backlog metrics can distinguish "waiting on
  purpose" from "waiting because workers are behind".
- QUEUED — claimable. The only state workers ever take jobs from.
- CLAIMED — owned by a worker, not yet started. Deliberately separate from
  RUNNING: it makes the claim step itself atomic and observable, and lets the
  reaper distinguish "worker grabbed it then died" from "handler is executing".
- RUNNING — a handler is executing; a job_executions row (attempt N) is open.
- COMPLETED / DEAD / CANCELLED — terminal. DEAD additionally snapshots the
  job into the dead letter queue.

A failed attempt goes back to QUEUED with run_at = now + backoff, so
retries reuse the exact same claiming machinery as new jobs — one code path,
not two.


3. The Hard Problem: atomic claiming

Requirement: N workers polling the same queue must never execute the same
job twice.

Chosen mechanism (PostgreSQL): a single UPDATE whose candidate SELECT uses
FOR UPDATE SKIP LOCKED. Row locks make competing claims disjoint; SKIP LOCKED
means a worker that loses a race skips to the next row instead of blocking.
This is the standard Postgres queue pattern (used by Sidekiq-like systems and
pg-backed queues such as Graphile Worker / Solid Queue).

SQLite fallback (local dev, tests): SELECT candidates, then per-row
compare-and-set: UPDATE jobs SET status='claimed', ... WHERE id=? AND
status='queued'. The WHERE clause re-checks the state, so of two racers
exactly one sees rowcount == 1. Correct, just O(rows) instead of one statement.

Verified by: tests/test_claiming.py — 4 threads race over 30 jobs;
every job claimed exactly once. Also verified live with 2 Docker workers
(SQL check: no completed job has ≠1 successful execution).


4. Coordination through the database — no message broker

The obvious alternative was Redis/RabbitMQ for the queue. Rejected because:

1. It splits the source of truth (queue state in the broker, history in the
   DB) and keeping them consistent needs outbox-style machinery.
2. It's a second stateful service to run, secure, and back up.
3. At this scale, Postgres row locking already gives contention-free claims.

Costs accepted: workers poll (1s interval) instead of being pushed work, and
the DB is the eventual scaling bottleneck. Both are fine at project scale;
the claiming logic is isolated in one module so a broker could replace it
without touching handlers or the API.


5. Retries and Dead Letter Queue

- Per-project retry policies (strategy + base delay + cap + max retries),
  attached per queue; jobs also carry max_attempts so a single job can override.
- Strategies: fixed / linear / exponential (base * 2^(n-1)), all capped by
  max_delay_s. Exponential is the default: transient failures (rate limits,
  restarts) usually need more room each time, and the cap stops the wait exploding.
- After max_attempts, the job goes to DEAD and is snapshotted into
  dead_letter_jobs (type + payload copied, not referenced — the DLQ must
  survive job archival). One click / one API call requeues it with attempts reset.
- Idempotency: clients can pass an idempotency_key; a unique constraint on
  (queue_id, idempotency_key) makes create-retries safe, enforced by the
  database rather than application logic.


6. Crash recovery (heartbeats + reaper)

Workers heartbeat every 5s. A maintenance tick (run by every worker, 2s
interval) requeues claimed/running jobs whose worker hasn't heartbeaten for
30s and marks that worker offline. Design choices:

- The reaper does not increment attempts — the job didn't fail, its worker did.
  An infrastructure death shouldn't push a job toward the DLQ.
- Graceful shutdown (SIGTERM) is the complement: stop claiming, drain running
  handlers (25s grace), deregister. docker compose stop therefore loses
  nothing, and docker kill loses at most 30s before the reaper recovers.


7. Exactly-once cron

Every worker runs the scheduler tick, so firing an occurrence must be
race-safe. Each due scheduled_jobs row is advanced with a compare-and-set
(UPDATE ... WHERE next_run_at = <the value I read>); only the winner's
rowcount is 1, and only the winner enqueues the job. No leader election
needed — the same trick as claiming, applied to time.


8. API design

- REST, resource-nested (/projects/{id}/queues, /queues/{id}/jobs),
  JWT bearer auth, Pydantic validation on every input.
- Multi-tenancy: every resource resolves up an ownership chain
  (job → queue → project → org → membership). Non-members get 404, not
  403 — a 403 confirms the resource exists; 404 leaks nothing.
- Uniform error envelope; limit/offset pagination with totals; filtering on
  the job list (status/type/batch).
- OpenAPI/Swagger generated from the code at /docs — documentation that can't drift.


9. Frontend

Vanilla JS + one CSS file, served statically by the API. No framework because
the dashboard is a thin read-view with a few actions: the system's actual
difficulty lives in the backend, and zero build steps keeps the demo unbreakable.
Live updates via 3s polling — WebSockets were the alternative, but polling is
stateless, survives reconnects for free, and at demo scale the difference is invisible.


10. Decision Ledger

| Decision | Alternative considered | Why this one |
|---|---|---|
| Postgres as queue + SKIP LOCKED | Redis/RabbitMQ broker | one source of truth, transactional transitions, no second stateful service |
| Separate worker process | background threads in the API | independent scaling/deployment, a wedged handler can't take down the API |
| Poll-based workers (1s) | LISTEN/NOTIFY push | simpler, portable to SQLite, 1s latency is fine for background jobs |
| Retry = requeue with future run_at | separate retry queue/table | one claiming code path handles both fresh jobs and retries |
| DLQ as snapshot table | status flag on jobs only | survives job archival; independent audit + requeue surface |
| Reaper doesn't burn attempts | count crash as a failed attempt | infra failure ≠ job failure; crashes shouldn't push jobs to DLQ |
| CAS for cron firing | leader election / singleton scheduler | no coordination service; any worker can die and cron still fires |
| UUID job ids | auto-increment ints | no sequence coordination across API replicas; non-enumerable in URLs |
| 404 for cross-tenant access | 403 | existence of other tenants' resources is itself information |
| Threads per worker | asyncio workers | handlers are sync (sleep/HTTP/CPU); threads keep handler code simple |
| SQLite fallback path | Postgres-only | zero-setup local run + fast tests; forced the claim logic to be explicit about its atomicity assumptions |
| create_all at startup | Alembic migrations | project scope; noted as the first thing to add for real production |


11. Known limitations

- No per-job execution timeout enforcement (field exists; enforcing it needs
  killable handler execution — process pool or async cancellation).
- Handlers are trusted code registered in the worker image — no sandboxing.
- Single-database ceiling; sharding by queue is the documented path.
- Metrics are computed on read; a real deployment would emit to Prometheus.
- RBAC is membership-only (owner/member roles exist but aren't differentiated).
