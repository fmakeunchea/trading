from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas import Incident

router = APIRouter()


@router.get("/incidents", response_model=list[Incident])
def list_incidents(
    limit: int = Query(100, ge=1, le=500),
    kind: str | None = None,
    db: Session = Depends(get_db),
) -> list[Incident]:
    sql = """
        SELECT id, kind, severity, phase, symbols, reason, payload, occurred_at
        FROM incidents
        {where}
        ORDER BY occurred_at DESC LIMIT :limit
    """.format(where="WHERE kind = :kind" if kind else "")
    params = {"limit": limit}
    if kind:
        params["kind"] = kind
    rows = db.execute(text(sql), params).mappings().all()
    return [Incident(**dict(r)) for r in rows]
