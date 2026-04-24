from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas import Trade

router = APIRouter()


@router.get("/orders", response_model=list[Trade])
def list_recent_trades(
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[Trade]:
    rows = db.execute(
        text(
            """
            SELECT id, symbol, side, qty, avg_fill_price, status, pnl, occurred_at
            FROM trades ORDER BY occurred_at DESC LIMIT :limit
            """
        ),
        {"limit": limit},
    ).mappings().all()
    return [Trade(**dict(r)) for r in rows]
