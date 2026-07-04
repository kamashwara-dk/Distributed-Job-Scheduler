ER Diagram and Schema Reasoning

```mermaid
erDiagram
    users ||--o{ org_members : "belongs to"
    organizations ||--o{ org_members : "has"
    organizations ||--o{ projects : "owns"
    projects ||--o{ queues : "owns"
    projects ||--o{ retry_policies : "defines"
    retry_policies |o--o{ queues : "applied to"
    queues ||--o{ jobs : "holds"
    queues ||--o{ scheduled_jobs : "cron templates"
    queues ||--o{ dead_letter_jobs : "failures from"
    jobs ||--o{ job_executions : "attempts"
    jobs ||--o{ job_logs : "log lines"
    job_executions |o--o{ job_logs : "scoped to"
    workers |o--o{ jobs : "claims"
    workers ||--o{ job_executions : "ran"
    workers ||--o{ worker_heartbeats : "emits"

    users { int id PK  string email UK  string name  string password_hash }
    organizations { int id PK  string name  int owner_id FK }
    org_members { int org_id PK,FK  int user_id PK,FK  string role }
    projects { int id PK  int org_id FK  string name  }
    retry_policies { int id PK  int project_id FK  string strategy  float base_delay_s  float max_delay_s  int max_retries }
    queues { int id PK  int project_id FK  string name  int priority  int concurrency_limit  bool paused  int retry_policy_id FK }
    jobs { string id PK  int queue_id FK  string type  json payload  string status  int priority  datetime run_at  int attempts  int max_attempts  string idempotency_key  string batch_id  string claimed_by FK }
    job_executions { int id PK  string job_id FK  string worker_id FK  int attempt  string status  datetime started_at  datetime finished_at  text error }
    job_logs { int id PK  string job_id FK  int execution_id FK  datetime ts  string level  text message }
    workers { string id PK  string hostname  int pid  int concurrency  string status  datetime last_heartbeat_at }
    worker_heartbeats { int id PK  string worker_id FK  datetime ts  int running_jobs }
    scheduled_jobs { int id PK  int queue_id FK  string cron_expr  string job_type  json payload  bool enabled  datetime next_run_at }
    dead_letter_jobs { int id PK  string job_id  int queue_id FK  string job_type  json payload  int attempts  text last_error  datetime requeued_at }
```

Keys

- Integer PKs for low-volume, human-managed entities (users, orgs,
  projects, queues, policies).
- UUID string PKs for jobs and workers: created at high rate,
  potentially from many API replicas — UUIDs avoid coordinating a sequence,
  are safe to expose in URLs (non-guessable, non-enumerable), and remain
  unique if queues are ever sharded across databases.

Normalization

Third normal form throughout, with two deliberate denormalizations:

1. dead_letter_jobs snapshots job_type/payload instead of only
   referencing jobs — the DLQ must survive job-row archival and be
   independently auditable.
2. jobs.attempts is a counter even though it equals COUNT(job_executions) —
   the claim path filters on it constantly and must not pay a join/aggregate per poll.

Cascade behavior

- Org → projects → queues → jobs → executions/logs: ON DELETE CASCADE —
  deleting a tenant cleanly removes their tree.
- queues.retry_policy_id and jobs.claimed_by: ON DELETE SET NULL — a
  deleted policy or worker must not delete jobs; the queue falls back to the
  default policy, the job gets requeued by the reaper.

Indexes (the ones that matter)

| Index | Serves |
|---|---|
| ix_jobs_claim (queue_id, status, run_at, priority) | the hot claim query — every worker, every second |
| uq_jobs_idempotency (queue_id, idempotency_key) | duplicate-create prevention, enforced by the DB not the app |
| ix_jobs_batch (batch_id) | batch listing |
| ix_job_logs_job_ts (job_id, ts) | job detail view (logs in order) |
| ix_sched_due (enabled, next_run_at) | cron tick scans only enabled+due rows |
| workers.last_heartbeat_at | reaper's staleness scan |
