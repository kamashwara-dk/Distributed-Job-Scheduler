from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access import get_org, get_project, org_ids_for
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
