# ER Diagram and Schema Reasoning

## Entity Relationship Diagram

```mermaid
erDiagram
    users ||--o{ org_members : "belongs to"
    organizations ||--o{ org_members : "has members"
    organizations ||--o{ projects : "owns"
    projects ||--o{ queues : "owns"
    projects ||--o{ retry_policies : "defines"
    retry_policies |o--o{ queues : "applied to"
    queues ||--o{ jobs : "holds"
    queues ||--o{ scheduled_jobs : "cron templates"
    queues ||--o{ dead_letter_jobs : "failures archived to"
    jobs ||--o{ job_executions : "has attempts"
    jobs ||--o{ job_logs : "emits log lines"
    job_executions |o--o{ job_logs : "scoped to attempt"
    workers |o--o{ jobs : "claims"
    workers ||--o{ job_executions : "ran"
    workers ||--o{ worker_heartbeats : "emits"

    users {
        int     id              PK
        string  email           UK
        string  name
        string  password_hash
        datetime created_at
    }
    organizations {
        int     id              PK
        string  name
        int     owner_id        FK
        datetime created_at
    }
    org_members {
        int     org_id          PK,FK
        int     user_id         PK,FK
        string  role            "owner | member"
    }
    projects {
        int     id              PK
        int     org_id          FK
        string  name            UK-per-org
        string  description
        datetime created_at
    }
    retry_policies {
        int     id              PK
        int     project_id      FK
        string  name
        string  strategy        "fixed | linear | exponential"
        float   base_delay_s
        float   max_delay_s
        int     max_retries
    }
    queues {
        int     id              PK
        int     project_id      FK
        string  name            UK-per-project
        int     priority
        int     concurrency_limit
        int     rate_limit_per_minute
        bool    paused
        int     retry_policy_id FK-nullable
        datetime created_at
    }
    jobs {
        string  id              PK-UUID
        int     queue_id        FK
        string  type
        json    payload
        string  status          "queued|scheduled|claimed|running|completed|dead|cancelled"
        int     priority
        datetime run_at
        int     timeout_s
        int     attempts
        int     max_attempts
        string  idempotency_key UK-per-queue
        string  batch_id        IX
        string  claimed_by      FK-nullable
        datetime claimed_at
        datetime started_at
        datetime finished_at
        text    last_error
        json    result
        datetime created_at
        datetime updated_at
    }
    job_executions {
        int     id              PK
        string  job_id          FK
        string  worker_id       FK-nullable
        int     attempt
        string  status          "running|completed|failed"
        datetime started_at
        datetime finished_at
        text    error
        json    result
    }
    job_logs {
        int     id              PK
        string  job_id          FK
        int     execution_id    FK-nullable
        datetime ts
        string  level           "info|warn|error"
        text    message
    }
    workers {
        string  id              PK-UUID
        string  hostname
        int     pid
        int     concurrency
        string  status          "online|stopping|offline"
        datetime started_at
        datetime last_heartbeat_at  IX
    }
    worker_heartbeats {
        int     id              PK
        string  worker_id       FK
        datetime ts
        int     running_jobs
    }
    scheduled_jobs {
        int     id              PK
        int     queue_id        FK
        string  name
        string  cron_expr
        string  job_type
        json    payload
        int     priority
        bool    enabled
        datetime next_run_at    IX
        datetime last_enqueued_at
        datetime created_at
    }
    dead_letter_jobs {
        int     id              PK
        string  job_id          IX
        int     queue_id        FK
        string  job_type
        json    payload
        int     attempts
        text    last_error
        datetime failed_at
        datetime requeued_at
    }
```

---

## Schema Design Decisions

### Primary Key Strategy

**Integer PKs** for low-volume, human-managed entities: `users`, `organizations`, `projects`, `queues`, `retry_policies`, `scheduled_jobs`, `dead_letter_jobs`, `job_executions`, `job_logs`, `worker_heartbeats`.

**UUID (hex string) PKs** for `jobs` and `workers`:
- Created at high rate from potentially many API replicas — no sequence coordinator needed
- Safe to expose in URLs: non-guessable, non-enumerable
- Remain globally unique if queues are ever sharded across database instances
- 32-char hex (`uuid.hex`) rather than hyphenated to save 4 bytes per FK

### Normalization

**Third normal form throughout**, with two deliberate denormalizations:

1. `dead_letter_jobs` snapshots `job_type` and `payload` instead of only referencing `jobs.id`.
   The DLQ must remain auditable and requeueable even after the original job row is archived
   or purged. A foreign-key-only design would make DLQ entries meaningless after archival.

2. `jobs.attempts` is a counter even though it equals `COUNT(job_executions)` for that job.
   The claim query filters on `attempts` constantly (every worker, every second).
   Paying a subquery aggregate on the hot path would be orders of magnitude slower.

### Cascade Behavior

| Relationship | On Delete | Rationale |
|---|---|---|
| org → projects → queues → jobs → executions/logs | CASCADE | Deleting a tenant cleanly removes their entire tree |
| queues.retry_policy_id | SET NULL | A deleted policy must not delete queues; the queue falls back to the default policy |
| jobs.claimed_by (worker_id) | SET NULL | A deleted worker row must not delete its jobs; the reaper handles orphan requeuing |
| workers → worker_heartbeats | CASCADE | Heartbeat rows have no value after the worker is gone |

### Indexes

| Index | Columns | Serves |
|---|---|---|
| `ix_jobs_claim` | `(queue_id, status, run_at, priority)` | The hot claim query — every worker, every second. Composite order matches the WHERE+ORDER BY exactly |
| `uq_jobs_idempotency` | `(queue_id, idempotency_key)` | Duplicate-create prevention, enforced by the DB, not the application |
| `ix_jobs_batch` | `(batch_id)` | Batch listing / cancellation |
| `ix_job_logs_job_ts` | `(job_id, ts)` | Job detail view loads logs ordered by time |
| `ix_sched_due` | `(enabled, next_run_at)` | Cron tick scans only enabled + due rows; skips disabled schedules |
| `workers.last_heartbeat_at` | `(last_heartbeat_at)` | Reaper's staleness scan — finds offline workers without a full table scan |
| `ix_heartbeats_worker_ts` | `(worker_id, ts)` | Heartbeat history lookup per worker |

### Multi-tenancy Isolation

Every resource resolves up an ownership chain to `org_members`. Access checks use this chain:

```
job → queue.project_id → project.org_id → org_members(org_id, user_id)
```

Non-members receive **404**, not 403. A 403 confirms the resource exists, which leaks information across tenants. 404 reveals nothing.

RBAC differentiates two roles within each org:
- `owner` — structural changes: create/update/delete queues, create projects, manage schedules
- `member` — operational access: create/cancel jobs, pause/resume queues, retry from DLQ
