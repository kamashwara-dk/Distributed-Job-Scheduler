"""The core reliability guarantee: a job is claimed by exactly one worker,
even when many workers race for it concurrently."""

import threading

from sqlalchemy import select

from app.models import Job, JobStatus, Organization, Project, Queue, User, Worker, utcnow
from app.services.claiming import claim_jobs, promote_due_scheduled


def _seed(db, n_jobs=20, concurrency_limit=100, workers=("w0", "w1", "w2", "w3")):
    # claimed_by is a real FK — workers must exist before they can claim
    db.add_all([Worker(id=w, hostname="test", pid=1) for w in workers])
    user = User(email="a@b.c", name="a", password_hash="x")
    db.add(user)
    db.flush()
    org = Organization(name="o", owner_id=user.id)
    db.add(org)
    db.flush()
    project = Project(org_id=org.id, name="p")
    db.add(project)
    db.flush()
    queue = Queue(project_id=project.id, name="q", concurrency_limit=concurrency_limit)
    db.add(queue)
    db.flush()
    jobs = [Job(queue_id=queue.id, type="sleep", payload={}) for _ in range(n_jobs)]
    db.add_all(jobs)
    db.commit()
    return queue


def test_racing_workers_never_claim_the_same_job(db_factory):
    setup = db_factory()
    _seed(setup, n_jobs=30)
    setup.close()

    results: dict[str, list[str]] = {}

    def worker(worker_id: str):
        session = db_factory()
        claimed: list[str] = []
        while True:
            got = claim_jobs(session, worker_id, 3)
            if not got:
                break
            claimed += [j.id for j in got]
        results[worker_id] = claimed
        session.close()

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    all_claims = [jid for claims in results.values() for jid in claims]
    assert len(all_claims) == 30, "every job claimed"
    assert len(set(all_claims)) == 30, "no job claimed twice"


def test_concurrency_limit_caps_claims(db_factory):
    session = db_factory()
    _seed(session, n_jobs=10, concurrency_limit=3)
    claimed = claim_jobs(session, "w1", 10)
    assert len(claimed) == 3, "claims capped at the queue's concurrency limit"
    # nothing more claimable until those finish
    assert claim_jobs(session, "w2", 10) == []
    session.close()


def test_paused_queue_is_not_claimed_from(db_factory):
    session = db_factory()
    queue = _seed(session, n_jobs=5)
    queue.paused = True
    session.commit()
    assert claim_jobs(session, "w1", 5) == []
    session.close()


def test_future_jobs_are_not_claimed_and_promote_flips_due_ones(db_factory):
    from datetime import timedelta

    session = db_factory()
    queue = _seed(session, n_jobs=0)
    future = Job(queue_id=queue.id, type="sleep", status=JobStatus.SCHEDULED,
                 run_at=utcnow() + timedelta(hours=1))
    due = Job(queue_id=queue.id, type="sleep", status=JobStatus.SCHEDULED,
              run_at=utcnow() - timedelta(seconds=1))
    session.add_all([future, due])
    session.commit()

    promote_due_scheduled(session)
    statuses = dict(session.execute(select(Job.id, Job.status)).all())
    assert statuses[due.id] == JobStatus.QUEUED
    assert statuses[future.id] == JobStatus.SCHEDULED

    claimed = claim_jobs(session, "w1", 10)
    assert [j.id for j in claimed] == [due.id]
    session.close()
