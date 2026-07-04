# Ownership-chain authorization: every resource resolves up to an org, and
# the caller must be a member. Non-members get 404 (not 403) so resource IDs
# don't leak across tenants.
#
# RBAC roles:
#   owner  — full access: create/update/delete orgs, projects, queues, schedules
#   member — operational access: create jobs, pause/resume queues, retry DLQ
#
# Owner-only operations raise 403 for members (not 404) because the resource
# existence is already established at that point.

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    DeadLetterJob,
    Job,
    Organization,
    OrgMember,
    Project,
    Queue,
    ScheduledJob,
    User,
)


def org_ids_for(db: Session, user: User) -> list[int]:
    return list(db.scalars(select(OrgMember.org_id).where(OrgMember.user_id == user.id)))


def _membership(db: Session, user: User, org_id: int) -> OrgMember | None:
    return db.scalar(
        select(OrgMember).where(
            OrgMember.org_id == org_id, OrgMember.user_id == user.id
        )
    )


def require_owner(db: Session, user: User, org_id: int) -> None:
    """Raise 403 if the user is not an owner of the given org."""
    m = _membership(db, user, org_id)
    if m is None or m.role != "owner":
        raise HTTPException(403, "Owner permission required for this operation")


def get_org(db: Session, user: User, org_id: int) -> Organization:
    org = db.get(Organization, org_id)
    if org is None or org_id not in org_ids_for(db, user):
        raise HTTPException(404, "Organization not found")
    return org


def get_project(db: Session, user: User, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None or project.org_id not in org_ids_for(db, user):
        raise HTTPException(404, "Project not found")
    return project


def get_project_owner(db: Session, user: User, project_id: int) -> Project:
    """Like get_project but additionally requires owner role."""
    project = get_project(db, user, project_id)
    require_owner(db, user, project.org_id)
    return project


def get_queue(db: Session, user: User, queue_id: int) -> Queue:
    queue = db.get(Queue, queue_id)
    if queue is None:
        raise HTTPException(404, "Queue not found")
    get_project(db, user, queue.project_id)  # raises 404 if not a member
    return queue


def get_queue_owner(db: Session, user: User, queue_id: int) -> Queue:
    """Like get_queue but additionally requires owner role."""
    queue = get_queue(db, user, queue_id)
    project = db.get(Project, queue.project_id)
    require_owner(db, user, project.org_id)
    return queue


def get_job(db: Session, user: User, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    get_queue(db, user, job.queue_id)
    return job


def get_schedule(db: Session, user: User, schedule_id: int) -> ScheduledJob:
    sched = db.get(ScheduledJob, schedule_id)
    if sched is None:
        raise HTTPException(404, "Schedule not found")
    get_queue(db, user, sched.queue_id)
    return sched


def get_schedule_owner(db: Session, user: User, schedule_id: int) -> ScheduledJob:
    sched = get_schedule(db, user, schedule_id)
    queue = db.get(Queue, sched.queue_id)
    project = db.get(Project, queue.project_id)
    require_owner(db, user, project.org_id)
    return sched


def get_dlq_entry(db: Session, user: User, entry_id: int) -> DeadLetterJob:
    entry = db.get(DeadLetterJob, entry_id)
    if entry is None:
        raise HTTPException(404, "Dead letter entry not found")
    get_queue(db, user, entry.queue_id)
    return entry
