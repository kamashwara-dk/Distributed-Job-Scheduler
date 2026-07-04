"""API behavior: auth, tenancy isolation, validation, idempotency, pagination."""

API = "/api/v1"


def test_register_login_me(client):
    r = client.post(f"{API}/auth/register", json={
        "email": "u@example.com", "name": "U", "password": "password123"})
    assert r.status_code == 201
    r = client.post(f"{API}/auth/login", json={
        "email": "u@example.com", "password": "password123"})
    assert r.status_code == 200
    token = r.json()["access_token"]
    r = client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.json()["email"] == "u@example.com"


def test_wrong_password_and_missing_token(client):
    client.post(f"{API}/auth/register", json={
        "email": "u@example.com", "name": "U", "password": "password123"})
    assert client.post(f"{API}/auth/login", json={
        "email": "u@example.com", "password": "wrongpass1"}).status_code == 401
    r = client.get(f"{API}/orgs")
    assert r.status_code == 401
    assert "error" in r.json()  # structured error envelope


def test_duplicate_email_conflict(client):
    body = {"email": "u@example.com", "name": "U", "password": "password123"}
    client.post(f"{API}/auth/register", json=body)
    assert client.post(f"{API}/auth/register", json=body).status_code == 409


def test_validation_error_envelope(client):
    r = client.post(f"{API}/auth/register", json={
        "email": "not-an-email", "name": "U", "password": "short"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_job_create_list_filter_paginate(auth_client):
    qid = auth_client.queue["id"]
    for i in range(7):
        auth_client.post(f"{API}/queues/{qid}/jobs",
                         json={"type": "sleep", "payload": {"i": i}})
    auth_client.post(f"{API}/queues/{qid}/jobs",
                     json={"type": "sleep", "delay_s": 3600})  # -> scheduled
    r = auth_client.get(f"{API}/queues/{qid}/jobs?limit=5&offset=0").json()
    assert r["total"] == 8 and len(r["items"]) == 5
    r = auth_client.get(f"{API}/queues/{qid}/jobs?status=scheduled").json()
    assert r["total"] == 1
    assert r["items"][0]["status"] == "scheduled"


def test_idempotency_key_prevents_duplicates(auth_client):
    qid = auth_client.queue["id"]
    body = {"type": "send_email", "idempotency_key": "order-42-receipt"}
    first = auth_client.post(f"{API}/queues/{qid}/jobs", json=body).json()
    second = auth_client.post(f"{API}/queues/{qid}/jobs", json=body).json()
    assert first["id"] == second["id"]
    total = auth_client.get(f"{API}/queues/{qid}/jobs").json()["total"]
    assert total == 1


def test_batch_jobs_share_batch_id(auth_client):
    qid = auth_client.queue["id"]
    r = auth_client.post(f"{API}/queues/{qid}/jobs/batch", json={
        "jobs": [{"type": "sleep"} for _ in range(3)]}).json()
    assert r["count"] == 3
    listed = auth_client.get(
        f"{API}/queues/{qid}/jobs?batch_id={r['batch_id']}").json()
    assert listed["total"] == 3


def test_cancel_only_pending_jobs(auth_client):
    qid = auth_client.queue["id"]
    job = auth_client.post(f"{API}/queues/{qid}/jobs", json={"type": "sleep"}).json()
    assert auth_client.post(f"{API}/jobs/{job['id']}/cancel").json()["status"] == "cancelled"
    # cancelling again -> 409
    assert auth_client.post(f"{API}/jobs/{job['id']}/cancel").status_code == 409


def test_cross_tenant_isolation(auth_client, client):
    """User B must not see or touch user A's resources — and gets 404, not 403."""
    qid = auth_client.queue["id"]
    job = auth_client.post(f"{API}/queues/{qid}/jobs", json={"type": "sleep"}).json()

    client.post(f"{API}/auth/register", json={
        "email": "intruder@example.com", "name": "B", "password": "password123"})
    token_b = client.post(f"{API}/auth/login", json={
        "email": "intruder@example.com", "password": "password123"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token_b}"}

    assert client.get(f"{API}/queues/{qid}/jobs", headers=headers).status_code == 404
    assert client.get(f"{API}/jobs/{job['id']}", headers=headers).status_code == 404
    assert client.post(f"{API}/queues/{qid}/pause", headers=headers).status_code == 404


def test_invalid_cron_rejected(auth_client):
    qid = auth_client.queue["id"]
    r = auth_client.post(f"{API}/queues/{qid}/schedules", json={
        "name": "bad", "cron_expr": "not a cron", "job_type": "sleep"})
    assert r.status_code == 422


def test_queue_pause_resume_and_stats(auth_client):
    qid = auth_client.queue["id"]
    assert auth_client.post(f"{API}/queues/{qid}/pause").json()["paused"] is True
    assert auth_client.post(f"{API}/queues/{qid}/resume").json()["paused"] is False
    auth_client.post(f"{API}/queues/{qid}/jobs", json={"type": "sleep"})
    stats = auth_client.get(f"{API}/queues/{qid}/stats").json()
    assert stats["depth"] == 1
    assert stats["counts"]["queued"] == 1
