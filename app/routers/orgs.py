from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access import get_org, get_project, org_ids_for, require_owner
from app.database import get_db
from app.models import Organization, OrgMember, Project, RetryPolicy, User
from app.schemas import OrgIn, ProjectIn, RetryPolicyIn
from app.security import get_current_user
from app.serialize import org_out, policy_out, project_out

router = APIRouter(tags=["orgs & projects"])


@router.post("/orgs", status_code=201, summary="Create an organization")
def create_org(body: OrgIn, user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    org = Organization(name=body.name, owner_id=user.id)
    db.add(org)
    db.flush()
    db.add(OrgMember(org_id=org.id, user_id=user.id, role="owner"))
    db.commit()
    return org_out(org)


@router.get("/orgs", summary="List my organizations")
def list_orgs(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    orgs = db.scalars(
        select(Organization).where(Organization.id.in_(org_ids_for(db, user)))
    ).all()
    return {"items": [org_out(o) for o in orgs]}


@router.post("/orgs/{org_id}/members", status_code=201,
             summary="Invite a user to an organization (owner only)")
def invite_member(org_id: int, body: dict,
                  user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Invite an existing user by email. Accepts: {"email": "...", "role": "member|owner"}"""
    get_org(db, user, org_id)
    require_owner(db, user, org_id)
    email = body.get("email", "").strip().lower()
    role = body.get("role", "member")
    if role not in ("owner", "member"):
        raise HTTPException(422, "role must be 'owner' or 'member'")
    if not email:
        raise HTTPException(422, "email is required")
    invitee = db.scalar(select(User).where(User.email == email))
    if invitee is None:
        raise HTTPException(404, f"No user with email '{email}' found")
    existing = db.scalar(
        select(OrgMember).where(OrgMember.org_id == org_id, OrgMember.user_id == invitee.id)
    )
    if existing:
        raise HTTPException(409, f"User '{email}' is already a member of this organization")
    db.add(OrgMember(org_id=org_id, user_id=invitee.id, role=role))
    db.commit()
    return {"org_id": org_id, "user_id": invitee.id, "email": invitee.email, "role": role}


@router.get("/orgs/{org_id}/members", summary="List organization members")
def list_members(org_id: int, user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    get_org(db, user, org_id)
    rows = db.execute(
        select(OrgMember, User)
        .join(User, User.id == OrgMember.user_id)
        .where(OrgMember.org_id == org_id)
    ).all()
    return {"items": [
        {"user_id": m.user_id, "email": u.email, "name": u.name, "role": m.role}
        for m, u in rows
    ]}


@router.delete("/orgs/{org_id}/members/{user_id}", status_code=204,
               summary="Remove a member from an organization (owner only)")
def remove_member(org_id: int, user_id: int,
                  user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_org(db, user, org_id)
    require_owner(db, user, org_id)
    if user_id == user.id:
        raise HTTPException(409, "Cannot remove yourself from an organization")
    member = db.scalar(
        select(OrgMember).where(OrgMember.org_id == org_id, OrgMember.user_id == user_id)
    )
    if member is None:
        raise HTTPException(404, "Member not found")
    db.delete(member)
    db.commit()


@router.post("/orgs/{org_id}/projects", status_code=201, summary="Create a project")
def create_project(org_id: int, body: ProjectIn,
                   user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_org(db, user, org_id)
    project = Project(org_id=org_id, name=body.name, description=body.description)
    db.add(project)
    db.commit()
    return project_out(project)


@router.get("/orgs/{org_id}/projects", summary="List projects in an organization")
def list_projects(org_id: int, user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    get_org(db, user, org_id)
    projects = db.scalars(select(Project).where(Project.org_id == org_id)).all()
    return {"items": [project_out(p) for p in projects]}


@router.post("/projects/{project_id}/retry-policies", status_code=201,
             summary="Create a retry policy")
def create_policy(project_id: int, body: RetryPolicyIn,
                  user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_project(db, user, project_id)
    policy = RetryPolicy(project_id=project_id, **body.model_dump())
    db.add(policy)
    db.commit()
    return policy_out(policy)


@router.get("/projects/{project_id}/retry-policies", summary="List retry policies")
def list_policies(project_id: int, user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    get_project(db, user, project_id)
    policies = db.scalars(
        select(RetryPolicy).where(RetryPolicy.project_id == project_id)
    ).all()
    return {"items": [policy_out(p) for p in policies]}
