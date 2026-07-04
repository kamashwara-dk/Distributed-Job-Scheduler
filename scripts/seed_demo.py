"""Seed a demo workspace via the public API (so it exercises the real thing).

Usage: python scripts/seed_demo.py [http://localhost:8000]

Creates: demo user, org, project, retry policies, 3 queues, a mix of jobs
(instant, delayed, flaky, permanently failing, a batch), and a cron schedule.
Log in on the dashboard afterwards with demo@example.com / demo1234.
"""

import sys

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000") + "/api/v1"
EMAIL, PASSWORD = "demo@example.com", "demo1234"


def main():
    c = httpx.Client(base_url=BASE, timeout=15)

    r = c.post("/auth/register", json={"email": EMAIL, "name": "Demo User",
                                       "password": PASSWORD})
    if r.status_code == 409:
        print("demo user already exists — reusing it")
    token = c.post("/auth/login", json={"email": EMAIL, "password": PASSWORD}
                   ).json()["access_token"]
    c.headers["Authorization"] = f"Bearer {token}"

    orgs = c.get("/orgs").json()["items"]
    org = orgs[0] if orgs else c.post("/orgs", json={"name": "Acme Corp"}).json()
    projects = c.get(f"/orgs/{org['id']}/projects").json()["items"]
    project = projects[0] if projects else c.post(
        f"/orgs/{org['id']}/projects",
        json={"name": "Notifications", "description": "Demo project"}).json()
    pid = project["id"]

    aggressive = c.post(f"/projects/{pid}/retry-policies", json={
        "name": "fast-exponential", "strategy": "exponential",
        "base_delay_s": 2, "max_delay_s": 30, "max_retries": 3}).json()

    queues = {q["name"]: q for q in c.get(f"/projects/{pid}/queues").json()["items"]}

    def queue(name, **kw):
        if name in queues:
            return queues[name]
        return c.post(f"/projects/{pid}/queues", json={"name": name, **kw}).json()

    emails = queue("emails", priority=10, concurrency_limit=4,
                   retry_policy_id=aggressive["id"])
    reports = queue("reports", priority=5, concurrency_limit=2)
    webhooks = queue("webhooks", priority=0, concurrency_limit=3,
                     retry_policy_id=aggressive["id"])

    for i in range(8):
        c.post(f"/queues/{emails['id']}/jobs", json={
            "type": "send_email",
            "payload": {"to": f"user{i}@example.com", "subject": f"Welcome #{i}"}})
    for name in ("q2-revenue", "user-growth"):
        c.post(f"/queues/{reports['id']}/jobs", json={
            "type": "generate_report", "payload": {"name": name, "rows": 8000}})

    c.post(f"/queues/{webhooks['id']}/jobs", json={
        "type": "flaky", "payload": {"fail_times": 2},
        "max_attempts": 4})                        # succeeds on 3rd try
    c.post(f"/queues/{webhooks['id']}/jobs", json={
        "type": "always_fail", "payload": {"error": "endpoint returns 500"},
        "max_attempts": 3})                        # -> dead letter queue
    c.post(f"/queues/{emails['id']}/jobs", json={
        "type": "send_email", "payload": {"to": "later@example.com",
                                          "subject": "Delayed digest"},
        "delay_s": 120})                           # delayed job
    c.post(f"/queues/{emails['id']}/jobs/batch", json={"jobs": [
        {"type": "send_email", "payload": {"to": f"batch{i}@example.com",
                                           "subject": "Batch blast"}}
        for i in range(5)]})

    existing = c.get(f"/queues/{reports['id']}/schedules").json()["items"]
    if not any(s["name"] == "minutely-healthcheck" for s in existing):
        c.post(f"/queues/{reports['id']}/schedules", json={
            "name": "minutely-healthcheck", "cron_expr": "* * * * *",
            "job_type": "sleep", "payload": {"seconds": 1}})

    print(f"Seeded. Dashboard: http://localhost:8000  ({EMAIL} / {PASSWORD})")


if __name__ == "__main__":
    main()
