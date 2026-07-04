API Summary

Base path: /api/v1. Auth: Authorization: Bearer <JWT> (from /auth/login).
Live interactive spec: http://localhost:8000/docs (Swagger UI, generated
from the code — always current).

Errors are uniform:
{"error": {"code": "not_found", "message": "...", "details": null}}
Cross-tenant access returns 404 (not 403) so resource IDs don't leak.

List endpoints paginate with ?limit=&offset= and return
{"items": [...], "total": N, "limit": L, "offset": O}.


Auth
| Method | Path | Purpose |
|---|---|---|
| POST | /auth/register | create account |
| POST | /auth/login | get JWT |
| GET | /auth/me | current user |

Organizations and Projects
| POST/GET | /orgs | create / list my orgs |
| POST/GET | /orgs/{id}/projects | create / list projects |
| POST/GET | /projects/{id}/retry-policies | create / list retry policies |

Queues
| POST/GET | /projects/{id}/queues | create / list queues |
| PATCH | /queues/{id} | update priority / concurrency / policy |
| POST | /queues/{id}/pause, /queues/{id}/resume | pause / resume |
| GET | /queues/{id}/stats | counts, depth, active, avg duration, hourly throughput |

Jobs
| POST | /queues/{id}/jobs | create (immediate; delay_s or run_at to defer; idempotency_key optional) |
| POST | /queues/{id}/jobs/batch | create up to 500 jobs sharing a batch_id |
| GET | /queues/{id}/jobs | list; filter by status, type, batch_id |
| GET | /jobs/{id} | detail + executions + logs |
| POST | /jobs/{id}/cancel | cancel if not yet started (409 otherwise) |
| POST | /jobs/{id}/retry | requeue a dead/cancelled/completed job |

Recurring (cron)
| POST/GET | /queues/{id}/schedules | create / list (cron validated) |
| PATCH | /schedules/{id} | edit / enable / disable |
| DELETE | /schedules/{id} | remove |

Workers, DLQ, Metrics
| GET | /workers | workers with liveness derived from heartbeat age |
| GET | /projects/{id}/dlq | dead-lettered jobs |
| POST | /dlq/{id}/retry | requeue from DLQ |
| GET | /projects/{id}/metrics/overview | status counts, per-queue depth, per-minute throughput, online workers |
