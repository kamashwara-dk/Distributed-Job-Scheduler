from sqlalchemy import create_engine, event, pool
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None):
    url = url or settings.database_url
    kwargs = {"pool_pre_ping": True}

    if url.startswith("sqlite"):
        # check_same_thread=False: worker threads each open their own session.
        # timeout=15: busy-wait up to 15 s before raising OperationalError.
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15}

        if url == "sqlite:///:memory:":
            # In-memory SQLite creates a brand-new empty DB per connection by
            # default.  Use StaticPool so ALL sessions share the same single
            # in-process connection — the schema created by create_all() is
            # therefore visible to every request handler.
            kwargs["connect_args"]["check_same_thread"] = False
            kwargs["poolclass"] = pool.StaticPool

    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):
            import os
            cur = dbapi_conn.cursor()
            # WAL requires a real file; skip for in-memory / Vercel.
            if not os.environ.get("VERCEL") and url != "sqlite:///:memory:":
                cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=15000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
