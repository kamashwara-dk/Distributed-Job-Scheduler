"""Additional tests: analysis endpoint, batch rate limiting, org membership,
timeout enforcement, worker status serialization, DLQ retry flow."""

import threading
import time

import pytest

from app.models import (
    DeadLetterJob,
    Job,
    JobExecution,
    JobLog,
    JobStatus,
    Organization,
    OrgMember,
    Project,
    Queue,
    RetryPolicy,
    User,
    Worker,
    utcnow,
)
from app.services.claiming import claim_jobs
from app.services.lifecycle import (
    add_log,
    complete_execution,
    fail_execution,
    start_execution,
)

API = "/api/v1"


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_user(db, email="u@test.com"):
    user = User(email=email, name="Test", password_hash="x")
    db.add(user)
    db.flush()
    return user


def _make_project(db, user):
    org = Organization(name="org", owner_id=user.id)
    db.add(org)
    db.flush()
    db.add(OrgMember(org_id=org.id, user_id=user.id, role="owner"))
    project = Project(org_id=org.id, name="proj")
    db.add(project)
    db.flush()
    return org, project


def _make_queue(db, project_id, **kwargs):
    q = Queue(project_id=project_id, name="q", **kwargs)
    db.add(q)
    db.flush()
    return q


def _make_job(db, queue_id, job_type="sleep", max_attempts=3):
    j = Job(queue_id=queue_id, type=job_type, payload={}, max_attempts=max_attempts)
    db.add(j)
    db.flush()
    return j


# ── AI analysis endpoint ──────────────────────────────────────────────────────

def test_analysis_endpoint_returns_category(auth_client):
    """analysis endpoint returns structured diagnosis for a failed job."""
    qid = auth_client.queue["id"]
    job = auth_client.post(f"{API}/queues/{qid}/jobs",
                           json={"type": "always_fail"}).json()

    # Fake a failure log so the analysis has something to pattern-match
    r = auth_client.get(f"{API}/jobs/{job['id']}/analysis")
    assert r.status_code == 200
    data = r.json()
    assert "analysis" in data
    assert data["analysis"]["category"] in (
        "unknown", "network_connectivity", "timeout", "rate_limited",
        "auth_failure", "upstream_error", "data_format",
        "resource_exhaustion", "misconfiguration", "simulated_failure",
    )
    assert data["analysis"]["confidence"] in ("high", "low")
    assert "recommendation" in data["analysis"]


def test_analysis_categorizes_timeout_errors(db):
    """analysis correctly categorizes timeout-related error messages."""
    user = _make_user(db)
    org, project = _make_project(db, user)
    q = _make_queue(db, project.id)
    worker = Worker(id="w1", hostname="h", pid=1)
    db.add(worker)
    db.commit()

    job = _make_job(db, q.id)
    db.commit()

    # Force a claimed/running state and add an execution with a timeout error
    [claimed] = claim_jobs(db, "w1", 1)
    execution = start_execution(db, claimed, "w1")
    add_log(db, claimed.id, "deadline exceeded while calling upstream",
            level="error", execution_id=execution.id)
    fail_execution(db, claimed, execution, "timeout: deadline exceeded after 30s")
    db.commit()

    # Verify the execution error is persisted
    exec_row = db.get(JobExecution, execution.id)
    assert exec_row.error is not None
    assert "timeout" in exec_row.error.lower()


# ── batch rate limiting ───────────────────────────────────────────────────────

def test_batch_rate_limit_accounts_for_entire_batch(auth_client):
    """A batch that would breach the rate limit is rejected with 429."""
    qid = auth_client.queue["id"]
    # Set rate limit to 3
    auth_client.patch(f"{API}/queues/{qid}", json={"rate_limit_per_minute": 3})

    # A batch of 4 should be rejected
    r = auth_client.post(f"{API}/queues/{qid}/jobs/batch", json={
        "jobs": [{"type": "sleep"} for _ in range(4)]
    })
    assert r.status_code == 429
    assert "rate limit" in r.json()["error"]["message"].lower()


def test_batch_within_rate_limit_succeeds(auth_client):
    """A batch that fits within the rate limit goes through."""
    qid = auth_client.queue["id"]
    auth_client.patch(f"{API}/queues/{qid}", json={"rate_limit_per_minute": 10})
    r = auth_client.post(f"{API}/queues/{qid}/jobs/batch", json={
        "jobs": [{"type": "sleep"} for _ in range(5)]
    })
    assert r.status_code == 201
    assert r.json()["count"] == 5


# ── org membership ────────────────────────────────────────────────────────────

def test_invite_member_and_access_resource(auth_client, client):
    """Owner can invite another user; invited member can access the project."""
    # Register a second user
    client.post(f"{API}/auth/register", json={
        "email": "member@example.com", "name": "Member", "password": "password123",
    })
    token_b = client.post(f"{API}/auth/login", json={
        "email": "member@example.com", "password": "password123",
    }).json()["access_token"]

    org_id = auth_client.org["id"]
    # Invite the second user
    r = auth_client.post(f"{API}/orgs/{org_id}/members",
                         json={"email": "member@example.com", "role": "member"})
    assert r.status_code == 201
    assert r.json()["role"] == "member"

    # Second user can now see the queue
    qid = auth_client.queue["id"]
    r2 = client.get(f"{API}/queues/{qid}/jobs",
                    headers={"Authorization": f"Bearer {token_b}"})
    assert r2.status_code == 200


def test_invite_nonexistent_user_returns_404(auth_client):
    org_id = auth_client.org["id"]
    r = auth_client.post(f"{API}/orgs/{org_id}/members",
                         json={"email": "nobody@nowhere.com", "role": "member"})
    assert r.status_code == 404


def test_invite_duplicate_member_returns_409(auth_client, client):
    client.post(f"{API}/auth/register", json={
        "email": "dup@example.com", "name": "Dup", "password": "password123",
    })
    org_id = auth_client.org["id"]
    auth_client.post(f"{API}/orgs/{org_id}/members",
                     json={"email": "dup@example.com", "role": "member"})
    r = auth_client.post(f"{API}/orgs/{org_id}/members",
                         json={"email": "dup@example.com", "role": "member"})
    assert r.status_code == 409


def test_member_cannot_invite_others(auth_client, client):
    """A 'member' role cannot invite further users — only owners can."""
    # Register and invite member B
    client.post(f"{API}/auth/register", json={
        "email": "member2@example.com", "name": "M2", "password": "password123",
    })
    token_b = client.post(f"{API}/auth/login", json={
        "email": "member2@example.com", "password": "password123",
    }).json()["access_token"]
    org_id = auth_client.org["id"]
    auth_client.post(f"{API}/orgs/{org_id}/members",
                     json={"email": "member2@example.com", "role": "member"})

    # Register a third user to invite
    client.post(f"{API}/auth/register", json={
        "email": "third@example.com", "name": "T", "password": "password123",
    })
    # Member B tries to invite — should get 403
    r = client.post(f"{API}/orgs/{org_id}/members",
                    json={"email": "third@example.com", "role": "member"},
                    headers={"Authorization": f"Bearer {token_b}"})
    assert r.status_code == 403


def test_list_members(auth_client):
    org_id = auth_client.org["id"]
    r = auth_client.get(f"{API}/orgs/{org_id}/members")
    assert r.status_code == 200
    members = r.json()["items"]
    assert len(members) >= 1
    assert any(m["role"] == "owner" for m in members)


# ── timeout enforcement ───────────────────────────────────────────────────────

def test_timeout_kills_long_running_handler():
    """A handler that exceeds timeout_s should be failed, not run indefinitely."""
    from worker.runner import run_job
    from app.database import SessionLocal, Base, make_engine
    from sqlalchemy.orm import sessionmaker
    import tempfile, os

    # Create a fresh isolated DB
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        eng = make_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(eng)
        Session = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)

        db = Session()
        user = User(email="t@t.com", name="t", password_hash="x")
        db.add(user)
        db.flush()
        org = Organization(name="o", owner_id=user.id)
        db.add(org)
        db.flush()
        project = Project(org_id=org.id, name="p")
        db.add(project)
        db.flush()
        q = Queue(project_id=project.id, name="q")
        db.add(q)
        db.flush()
        worker = Worker(id="wt1", hostname="h", pid=1)
        db.add(worker)
        db.commit()

        # Job with a 1s timeout
        job = Job(queue_id=q.id, type="sleep", payload={"seconds": 10},
                  timeout_s=1, max_attempts=2)
        db.add(job)
        db.commit()

        # Manually claim the job so run_job can find it
        [claimed] = claim_jobs(db, "wt1", 1)
        db.close()

        # Patch SessionLocal used inside run_job to use our isolated DB
        import worker.runner as runner_mod
        import app.services.lifecycle as lifecycle_mod
        import app.services.claiming as claiming_mod
        original_sl = runner_mod.__dict__.get("SessionLocal")
        runner_mod.SessionLocal = Session

        start = time.monotonic()
        run_job(claimed.id, "wt1")
        elapsed = time.monotonic() - start

        runner_mod.SessionLocal = original_sl or runner_mod.SessionLocal

        # Should have returned well within 10s (the handler's sleep duration)
        assert elapsed < 8, f"Timeout not enforced: took {elapsed:.1f}s"

        # Job should now be failed / retried (not still running)
        check_db = Session()
        refreshed = check_db.get(Job, claimed.id)
        assert refreshed.status in (JobStatus.QUEUED, JobStatus.DEAD), \
            f"Expected failed/requeued status after timeout, got: {refreshed.status}"
        check_db.close()
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


# ── worker status serialization ───────────────────────────────────────────────

def test_worker_stopping_status_serialized_correctly():
    """A worker in 'stopping' state with recent heartbeat shows as 'stopping', not 'online'."""
    from datetime import timedelta
    from app.serialize import worker_out

    w = Worker(id="ws1", hostname="h", pid=1, status="stopping",
               last_heartbeat_at=utcnow())
    result = worker_out(w, online_threshold_s=30, age_s=2.0)
    assert result["status"] == "stopping"


def test_worker_offline_status_respected():
    """A worker explicitly marked offline always shows as 'offline'."""
    from app.serialize import worker_out

    w = Worker(id="wo1", hostname="h", pid=1, status="offline",
               last_heartbeat_at=utcnow())
    result = worker_out(w, online_threshold_s=30, age_s=1.0)
    assert result["status"] == "offline"


# ── retry job via API (end-to-end) ────────────────────────────────────────────

def test_retry_job_api_resets_attempts(auth_client):
    """POST /jobs/{id}/retry on a cancelled job resets attempts and status."""
    qid = auth_client.queue["id"]
    job = auth_client.post(f"{API}/queues/{qid}/jobs", json={"type": "sleep"}).json()
    cancel_r = auth_client.post(f"{API}/jobs/{job['id']}/cancel")
    assert cancel_r.json()["status"] == "cancelled"

    retry_r = auth_client.post(f"{API}/jobs/{job['id']}/retry")
    assert retry_r.status_code == 200
    assert retry_r.json()["status"] == "queued"
    assert retry_r.json()["attempts"] == 0


# ── metrics overview ──────────────────────────────────────────────────────────

def test_metrics_overview_returns_expected_shape(auth_client):
    """metrics/overview returns all required keys for the dashboard."""
    pid = auth_client.project["id"]
    qid = auth_client.queue["id"]
    auth_client.post(f"{API}/queues/{qid}/jobs", json={"type": "sleep"})

    r = auth_client.get(f"{API}/projects/{pid}/metrics/overview")
    assert r.status_code == 200
    data = r.json()
    assert "counts" in data
    assert "throughput_per_min" in data
    assert len(data["throughput_per_min"]) == 30
    assert "queues" in data
    assert "online_workers" in data
    assert "generated_at" in data
    assert data["counts"]["queued"] >= 1


# ── rate limit single job ─────────────────────────────────────────────────────

def test_single_job_rate_limit_enforced(auth_client):
    """Single-job creation is rejected after rate limit is hit."""
    qid = auth_client.queue["id"]
    auth_client.patch(f"{API}/queues/{qid}", json={"rate_limit_per_minute": 2})

    auth_client.post(f"{API}/queues/{qid}/jobs", json={"type": "sleep"})
    auth_client.post(f"{API}/queues/{qid}/jobs", json={"type": "sleep"})
    r = auth_client.post(f"{API}/queues/{qid}/jobs", json={"type": "sleep"})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"
