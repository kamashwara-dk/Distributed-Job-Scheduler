# Response serializers kept as plain functions (not response_model classes)
# so list endpoints stay cheap and the shapes live in one place.

from app.models import (
    DeadLetterJob,
    Job,
    JobExecution,
    JobLog,
    Organization,
    Project,
    Queue,
    RetryPolicy,
    ScheduledJob,
    Worker,
)


def _dt(v):
    return v.isoformat() + "Z" if v else None


def org_out(o: Organization):
    return {"id": o.id, "name": o.name, "owner_id": o.owner_id, "created_at": _dt(o.created_at)}


def project_out(p: Project):
    return {
        "id": p.id, "org_id": p.org_id, "name": p.name,
        "description": p.description, "created_at": _dt(p.created_at),
    }


def policy_out(rp: RetryPolicy):
    return {
        "id": rp.id, "project_id": rp.project_id, "name": rp.name,
        "strategy": rp.strategy, "base_delay_s": rp.base_delay_s,
        "max_delay_s": rp.max_delay_s, "max_retries": rp.max_retries,
    }


def queue_out(q: Queue):
    return {
        "id": q.id, "project_id": q.project_id, "name": q.name,
        "priority": q.priority, "concurrency_limit": q.concurrency_limit,
        "rate_limit_per_minute": q.rate_limit_per_minute,
        "paused": q.paused, "retry_policy_id": q.retry_policy_id,
        "created_at": _dt(q.created_at),
    }


def job_out(j: Job):
    return {
        "id": j.id, "queue_id": j.queue_id, "type": j.type, "status": j.status,
        "priority": j.priority, "payload": j.payload,
        "attempts": j.attempts, "max_attempts": j.max_attempts,
        "run_at": _dt(j.run_at), "claimed_by": j.claimed_by,
        "started_at": _dt(j.started_at), "finished_at": _dt(j.finished_at),
        "last_error": j.last_error, "result": j.result,
        "batch_id": j.batch_id, "idempotency_key": j.idempotency_key,
        "created_at": _dt(j.created_at), "updated_at": _dt(j.updated_at),
    }


def execution_out(e: JobExecution):
    return {
        "id": e.id, "job_id": e.job_id, "worker_id": e.worker_id,
        "attempt": e.attempt, "status": e.status, "error": e.error,
        "result": e.result, "started_at": _dt(e.started_at),
        "finished_at": _dt(e.finished_at),
    }


def log_out(l: JobLog):
    return {"ts": _dt(l.ts), "level": l.level, "message": l.message,
            "execution_id": l.execution_id}


def worker_out(w: Worker, online_threshold_s: int, age_s: float):
    # Derive effective status: a worker in "stopping" state that recently
    # heartbeated is still considered "stopping" (not "online") in the UI.
    if w.status == "offline":
        effective_status = "offline"
    elif w.status == "stopping":
        effective_status = "stopping"
    elif age_s < online_threshold_s:
        effective_status = "online"
    else:
        effective_status = "offline"
    return {
        "id": w.id, "hostname": w.hostname, "pid": w.pid,
        "concurrency": w.concurrency,
        "status": effective_status,
        "started_at": _dt(w.started_at), "last_heartbeat_at": _dt(w.last_heartbeat_at),
        "heartbeat_age_s": round(age_s, 1),
    }


def schedule_out(s: ScheduledJob):
    return {
        "id": s.id, "queue_id": s.queue_id, "name": s.name,
        "cron_expr": s.cron_expr, "job_type": s.job_type, "payload": s.payload,
        "priority": s.priority, "enabled": s.enabled,
        "next_run_at": _dt(s.next_run_at), "last_enqueued_at": _dt(s.last_enqueued_at),
    }


def dlq_out(d: DeadLetterJob):
    return {
        "id": d.id, "job_id": d.job_id, "queue_id": d.queue_id,
        "job_type": d.job_type, "payload": d.payload, "attempts": d.attempts,
        "last_error": d.last_error, "failed_at": _dt(d.failed_at),
        "requeued_at": _dt(d.requeued_at),
    }
