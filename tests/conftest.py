import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db, make_engine


@pytest.fixture()
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


@pytest.fixture()
def client(db_factory):
    from app.main import app

    def override_get_db():
        session = db_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def auth_client(client):
    """Client with a registered user, org, project, and queue ready to use."""
    client.post("/api/v1/auth/register", json={
        "email": "test@example.com", "name": "Tester", "password": "password123",
    })
    token = client.post("/api/v1/auth/login", json={
        "email": "test@example.com", "password": "password123",
    }).json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    org = client.post("/api/v1/orgs", json={"name": "TestOrg"}).json()
    project = client.post(f"/api/v1/orgs/{org['id']}/projects",
                          json={"name": "TestProject"}).json()
    queue = client.post(f"/api/v1/projects/{project['id']}/queues",
                        json={"name": "default", "concurrency_limit": 5}).json()
    client.org = org
    client.project = project
    client.queue = queue
    return client
