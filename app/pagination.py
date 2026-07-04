from fastapi import Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session


class PageParams:
    def __init__(
        self,
        limit: int = Query(25, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        self.limit = limit
        self.offset = offset


def paginate(db: Session, stmt, page: PageParams, serializer):
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.limit(page.limit).offset(page.offset)).all()
    return {
        "items": [serializer(r) for r in rows],
        "total": total,
        "limit": page.limit,
        "offset": page.offset,
    }
