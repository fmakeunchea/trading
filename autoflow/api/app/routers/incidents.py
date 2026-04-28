from typing import Literal

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
    category: Literal["critical", "diagnostic", "all"] = "critical",
    db: Session = Depends(get_db),
) -> list[Incident]:
    """List incident records.

    ``category`` defaults to ``critical`` so the safety-incident view stays
    clean of observability noise (bars_fetched, no_signal, risk_denied).
    Set ``category=diagnostic`` for the engine's per-symbol decision
    stream, or ``category=all`` for both.
    """
    where: list[str] = []
    params: dict = {"limit": limit}
    if category == "critical":
        where.append("kind != 'DIAGNOSTIC'")
    elif category == "diagnostic":
        where.append("kind = 'DIAGNOSTIC'")
    if kind:
        where.append("kind = :kind")
        params["kind"] = kind
    sql = (
        "SELECT id, kind, severity, phase, symbols, reason, payload, occurred_at "
        "FROM incidents"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY occurred_at DESC LIMIT :limit"
    rows = db.execute(text(sql), params).mappings().all()
    return [Incident(**dict(r)) for r in rows]
