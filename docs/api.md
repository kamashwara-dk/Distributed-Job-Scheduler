# API Reference

**Base path:** `/api/v1`  
**Auth:** `Authorization: Bearer <JWT>` (obtain from `POST /auth/login`)  
**Interactive docs:** `/docs` (Swagger UI, auto-generated from code — always current)  
**Alt docs:** `/redoc` (ReDoc)

---

## Conventions

### Error Envelope

Every error response uses a uniform envelope:

```json
{
  "error": {
    "code": "not_found",
    "message": "Queue not found",
    "details": null
  }
}
```

| HTTP | `code` | Meaning |
|---|---|---|
| 400 | `bad_request` | Malformed request |
| 401 | `unauthorized` | Missing or expired JWT |
| 403 | `forbidden` | Authenticated but insufficient role (owner required) |
| 404 | `not_found` | Resource doesn't exist **or** caller lacks access (cross-tenant) |
| 409 | `conflict` | State conflict (duplicate email, already cancelled, etc.) |
| 422 | `validation_error` | Pydantic validation failure; `details` contains field errors |
| 429 | `rate_limited` | Queue rate limit exceeded |

### Pagination

All list endpoints accept `?limit=N&offset=N` and return:

```json
{
  "items": [...],
  "total": 42,
  "limit": 20,
  "offset": 0
}
```

### RBAC

| Role | Capabilities |
|---|---|
| `owner` | Everything: create/update/delete orgs, projects, queues, schedules, retry policies |
| `member` | Operational: create jobs, cancel jobs, pause/resume queues, retry from DLQ, view everything |

---

## Auth

### `POST /auth/register`
Create a new user account.

**Body:**
```json
{ "email": "you@company.com", "name": "Alice", "password": "minimum8chars" }
```

**Response 201:**
```json
{ "id": 1, "email": "you@company.com", "name": "Alice" }
```

---

### `POST /auth/login`
Obtain a JWT access token.

**Body:** `{ "email": "...", "password": "..." }`

**Response 200:**
```json
{ "access_token": "<jwt>", "token_type": "bearer" }
```

---

### `GET /auth/me`
Return the authenticated user's profile.

---

## Organizations

### `POST /orgs`
Create an organization. Caller becomes `owner`.

**Body:** `{ "name": "Acme Corp" }`

---

### `GET /orgs`
List organizations the current user is a member of.

---

### `POST /orgs/{org_id}/members`
Invite an existing user to the organization. **Owner only.**

**Body:** `{ "email": "colleague@company.com", "role": "member" }` — `role` is `"owner"` or `"member"`.

**Response 201:** `{ "org_id": 1, "user_id": 2, "email": "colleague@company.com", "role": "member" }`

**Errors:** 404 if no user with that email exists · 409 if already a member · 422 if role is invalid.

---

### `GET /orgs/{org_id}/members`
List all members of an organization.

**Response:** `{ "items": [{ "user_id": 1, "email": "...", "name": "...", "role": "owner" }, ...] }`

---

### `DELETE /orgs/{org_id}/members/{user_id}`
Remove a member from the organization. **Owner only.** Cannot remove yourself.

**Response:** 204 No Content.

---

### `POST /orgs/{org_id}/projects`
Create a project inside an org. **Owner only.**

**Body:** `{ "name": "Notifications", "description": "optional" }`

---

### `GET /orgs/{org_id}/projects`
List projects in an org.

---

## Retry Policies

### `POST /projects/{project_id}/retry-policies`
Define a named retry policy. **Owner only.**

**Body:**
```json
{
  "name": "fast-exponential",
  "strategy": "exponential",
  "base_delay_s": 5,
  "max_delay_s": 300,
  "max_retries": 4
}
```

`strategy` must be one of: `fixed`, `linear`, `exponential`

| Strategy | Delay formula |
|---|---|
| `fixed` | `base_delay_s` every time |
| `linear` | `base_delay_s × attempt` |
| `exponential` | `base_delay_s × 2^(attempt-1)` |

All capped at `max_delay_s`.

---

### `GET /projects/{project_id}/retry-policies`
List retry policies defined for a project.

---

## Queues

### `POST /projects/{project_id}/queues`
Create a queue. **Owner only.**

**Body:**
```json
{
  "name": "email-delivery",
  "priority": 10,
  "concurrency_limit": 5,
  "rate_limit_per_minute": 100,
  "retry_policy_id": 3
}
```

| Field | Default | Description |
|---|---|---|
| `priority` | 0 | Higher-priority queues are polled first by workers |
| `concurrency_limit` | 10 | Max simultaneously running jobs across all workers |
| `rate_limit_per_minute` | 0 | Max new jobs accepted per 60 s (0 = unlimited) |
| `retry_policy_id` | null | Falls back to built-in exponential (5 s base, 300 s cap, 3 retries) |

---

### `GET /projects/{project_id}/queues`
List all queues in a project, ordered by priority descending.

---

### `PATCH /queues/{queue_id}`
Update queue configuration. **Owner only.**

All fields optional; only provided fields are updated.

---

### `POST /queues/{queue_id}/pause`
Stop workers from claiming jobs from this queue. Jobs already running continue.

---

### `POST /queues/{queue_id}/resume`
Resume a paused queue.

---

### `GET /queues/{queue_id}/stats`
Per-queue statistics.

**Response:**
```json
{
  "queue": { "...queue fields..." },
  "counts": {
    "queued": 12, "scheduled": 3, "claimed": 1,
    "running": 2, "completed": 847, "dead": 5, "cancelled": 0
  },
  "depth": 15,
  "active": 3,
  "completed_last_hour": 143,
  "avg_duration_s": 1.42
}
```

---

## Jobs

### `POST /queues/{queue_id}/jobs`
Enqueue a job.

**Body:**
```json
{
  "type": "send_email",
  "payload": { "to": "user@example.com", "subject": "Hello" },
  "priority": 5,
  "delay_s": 0,
  "run_at": null,
  "max_attempts": 4,
  "timeout_s": 30,
  "idempotency_key": "order-99-receipt"
}
```

| Field | Default | Description |
|---|---|---|
| `type` | required | Handler name registered on the worker |
| `payload` | `{}` | Arbitrary JSON passed to the handler |
| `priority` | 0 | Higher priority is claimed first within the queue |
| `delay_s` | null | Execute after N seconds (mutually exclusive with `run_at`) |
| `run_at` | null | Execute at this UTC datetime (mutually exclusive with `delay_s`) |
| `max_attempts` | 4 | 1 attempt + N-1 retries |
| `timeout_s` | null | Reserved for future enforcement |
| `idempotency_key` | null | Returns existing job if key already used in this queue |

**429 response** if the queue's `rate_limit_per_minute` is breached.

---

### `POST /queues/{queue_id}/jobs/batch`
Enqueue up to **500** jobs atomically. All share a `batch_id` for group tracking.

**Body:**
```json
{
  "jobs": [
    { "type": "send_email", "payload": { "to": "a@example.com" } },
    { "type": "send_email", "payload": { "to": "b@example.com" } }
  ]
}
```

**Response 201:**
```json
{ "batch_id": "a3f...", "count": 2, "items": [...] }
```

---

### `GET /queues/{queue_id}/jobs`
List jobs with filtering and pagination.

| Query param | Description |
|---|---|
| `status` | Filter by status value |
| `type` | Filter by job type string |
| `batch_id` | Filter to a specific batch |
| `limit` / `offset` | Pagination |

---

### `GET /jobs/{job_id}`
Full job detail including execution history and logs.

**Response:**
```json
{
  "id": "a3f...",
  "type": "send_email",
  "status": "completed",
  "attempts": 1,
  "max_attempts": 4,
  "payload": { "to": "user@example.com" },
  "result": { "delivered": true },
  "executions": [
    { "attempt": 1, "status": "completed", "started_at": "...", "finished_at": "..." }
  ],
  "logs": [
    { "ts": "...", "level": "info", "message": "rendering template for user@example.com" }
  ]
}
```

---

### `POST /jobs/{job_id}/cancel`
Cancel a job that hasn't started yet (`queued` or `scheduled`). Returns **409** for any other status.

---

### `POST /jobs/{job_id}/retry`
Requeue a `dead`, `cancelled`, or `completed` job with attempts reset to 0.

---

### `GET /jobs/{job_id}/analysis`
**AI-powered failure analysis** — returns root cause category, recommended action, and retry trend.

**Response:**
```json
{
  "job_id": "a3f...",
  "job_type": "http_request",
  "status": "dead",
  "total_attempts": 4,
  "analysis": {
    "category": "network_connectivity",
    "title": "Network / connectivity failure",
    "recommendation": "Check that the target service is reachable...",
    "retry_trend": "escalating",
    "retry_delays_s": [5.1, 10.3, 20.8],
    "unique_errors": ["ConnectionRefusedError: [Errno 111] Connection refused"],
    "confidence": "high"
  }
}
```

---

## Schedules (Cron)

### `POST /queues/{queue_id}/schedules`
Create a recurring job schedule. **Owner only.**

**Body:**
```json
{
  "name": "daily-report",
  "cron_expr": "0 9 * * 1-5",
  "job_type": "generate_report",
  "payload": { "name": "revenue" },
  "priority": 0
}
```

Cron expression is validated with `croniter`. Standard 5-field cron syntax.

---

### `GET /queues/{queue_id}/schedules`
List schedules for a queue.

---

### `PATCH /schedules/{schedule_id}`
Update a schedule. All fields optional.

```json
{ "enabled": false, "cron_expr": "*/5 * * * *", "payload": {}, "priority": 0 }
```

---

### `DELETE /schedules/{schedule_id}`
Delete a schedule. **Owner only.** Does not affect already-materialized jobs.

---

## Workers

### `GET /workers`
List all registered workers with live status derived from heartbeat age.

**Response item:**
```json
{
  "id": "b4a...",
  "hostname": "worker-1",
  "pid": 12345,
  "concurrency": 4,
  "status": "online",
  "running_jobs": 2,
  "heartbeat_age_s": 3.1
}
```

Workers with `heartbeat_age_s > 30` are shown as `offline`.

---

## Dead Letter Queue

### `GET /projects/{project_id}/dlq`
List dead-lettered jobs for a project, most recent first.

---

### `POST /dlq/{entry_id}/retry`
Requeue a dead-lettered job. Resets `attempts` to 0.  
Returns **409** if already requeued.

---

## Metrics

### `GET /projects/{project_id}/metrics/overview`
Dashboard overview — single call that powers all stat cards.

**Response:**
```json
{
  "counts": {
    "queued": 8, "scheduled": 2, "claimed": 1,
    "running": 3, "completed": 1204, "dead": 7, "cancelled": 2
  },
  "throughput_per_min": [0, 2, 5, 3, ...],
  "queues": [
    { "id": 1, "name": "emails", "paused": false, "depth": 8 }
  ],
  "online_workers": 2,
  "generated_at": "2026-07-04T10:32:01Z"
}
```

`throughput_per_min` is an array of 30 integers — completed jobs per minute for the last 30 minutes, oldest first.

---

## WebSocket Live Feed

### `WS /api/v1/ws/{project_id}?token=<jwt>`

Connect to receive a compact metrics snapshot every 2 seconds without polling. Authentication is via the `token` query parameter (browsers cannot set WebSocket headers).

**Message format:**
```json
{
  "type": "metrics",
  "counts": { "queued": 5, "running": 2, "completed": 1204, ... },
  "online_workers": 2,
  "ts": "2026-07-04T10:32:03Z"
}
```

Connection is rejected with close code `4001` if the token is missing or invalid.

---

## Health

### `GET /api/health`
Returns `{"status": "ok"}`. No auth required. Suitable for load balancer health checks.
