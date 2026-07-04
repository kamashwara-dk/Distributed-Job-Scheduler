"""Full lifecycle: fail -> retry with backoff -> exhaust -> DLQ -> manual requeue."""

from sqlalchemy import select

from app.models import (
    DeadLetterJob, Job, JobStatus, Organization, Project, Queue, RetryPolicy,
    User, Worker, utcnow,
)
from app.services.claiming import claim_jobs
from app.services.lifecycle import (
    complete_execution, fail_execution, reap_stale_workers, start_execution,
)


def _seed_job(db, max_attempts=3, strategy="fixed", base_delay=60):
    db.add_all([Worker(id=w, hostname="test", pid=1) for w in ("w1", "w2")])
    user = User(email="a@b.c", name="a", password_hash="x")
    db.add(user); db.flush()
    org = Organization(name="o", owner_id=user.id)
    db.add(org); db.flush()
    project = Project(org_id=org.id, name="p")
    db.add(project); db.flush()
    policy = RetryPolicy(project_id=project.id, name="pol", strategy=strategy,
                         base_delay_s=base_delay, max_delay_s=3600, max_retries=max_attempts - 1)
    db.add(policy); db.flush()
    queue = Queue(project_id=project.id, name="q", retry_policy_id=policy.id)
    db.add(queue); db.flush()
    job = Job(queue_id=queue.id, type="flaky", payload={}, max_attempts=max_attempts)
    db.add(job)
    db.commit()
    return job


def test_success_path(db):
    job = _seed_job(db)
    [job] = claim_jobs(db, "w1", 1)
    execution = start_execution(db, job, "w1")
    assert job.status == JobStatus.RUNNING
    complete_execution(db, job, execution, {"ok": True})
    assert job.status == JobStatus.COMPLETED
    assert job.attempts == 1
    assert job.result == {"ok": True}


def test_failure_requeues_with_backoff_delay(db):
    job = _seed_job(db, max_attempts=3, base_delay=60)
    [job] = claim_jobs(db, "w1", 1)
    execution = start_execution(db, job, "w1")
    status = fail_execution(db, job, execution, "boom")
    assert status == JobStatus.QUEUED
    assert job.attempts == 1
    assert job.claimed_by is None
    # backoff pushed run_at into the future -> not immediately reclaimable
    assert (job.run_at - utcnow()).total_seconds() > 30
    assert claim_jobs(db, "w2", 1) == []


def test_exhausted_attempts_go_to_dlq(db):
    job = _seed_job(db, max_attempts=2, base_delay=0.01)
    for _ in range(2):
        job.run_at = utcnow()  # skip waiting out the backoff
        db.commit()
        [job] = claim_jobs(db, "w1", 1)
        execution = start_execution(db, job, "w1")
        fail_execution(db, job, execution, "boom")
    assert job.status == JobStatus.DEAD
    entry = db.scalar(select(DeadLetterJob).where(DeadLetterJob.job_id == job.id))
    assert entry is not None
    assert entry.attempts == 2
    assert entry.last_error == "boom"


def test_reaper_requeues_orphans_of_dead_worker(db):
    from datetime import timedelta
    from app.models import Worker

    job = _seed_job(db)
    worker = Worker(hostname="h", pid=1,
                    last_heartbeat_at=utcnow() - timedelta(seconds=999))
    db.add(worker)
    db.commit()
    [job] = claim_jobs(db, worker.id, 1)
    assert job.status == JobStatus.CLAIMED

    requeued = reap_stale_workers(db, stale_after_s=30)
    db.refresh(job)
    assert requeued == 1
    assert job.status == JobStatus.QUEUED
    assert job.claimed_by is None
    assert job.attempts == 0, "an infrastructure failure must not burn an attempt"
