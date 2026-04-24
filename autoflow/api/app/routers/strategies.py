from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import yaml_writer
from ..db import get_db
from ..schemas import ActionResult, Strategy, StrategyUpsert

router = APIRouter()


SELECT_COLS = """
    id, name, mode, symbols, daily_loss_cap_pct,
    max_concurrent_positions, position_notional_pct, is_active,
    applied_at, last_restart_at, updated_at
"""


def _row_to_strategy(row: dict) -> Strategy:
    applied = row.get("applied_at")
    updated = row.get("updated_at")
    last_restart = row.get("last_restart_at")
    dirty = applied is None or (updated is not None and applied < updated)
    restart_required = applied is not None and (
        last_restart is None or last_restart < applied
    )
    return Strategy(**dict(row), dirty=dirty, restart_required=restart_required)


@router.get("/strategies", response_model=list[Strategy])
def list_strategies(db: Session = Depends(get_db)) -> list[Strategy]:
    rows = db.execute(
        text(f"SELECT {SELECT_COLS} FROM strategies ORDER BY id")
    ).mappings().all()
    return [_row_to_strategy(dict(r)) for r in rows]


@router.get("/strategies/{strategy_id}", response_model=Strategy)
def get_strategy(strategy_id: int, db: Session = Depends(get_db)) -> Strategy:
    row = db.execute(
        text(f"SELECT {SELECT_COLS} FROM strategies WHERE id = :id"),
        {"id": strategy_id},
    ).mappings().first()
    if not row:
        raise HTTPException(404, "strategy not found")
    return _row_to_strategy(dict(row))


@router.put("/strategies/{strategy_id}", response_model=Strategy)
def update_strategy(
    strategy_id: int, body: StrategyUpsert, db: Session = Depends(get_db)
) -> Strategy:
    row = db.execute(
        text(
            f"""
            UPDATE strategies SET
              name = :name, mode = :mode, symbols = :symbols,
              daily_loss_cap_pct = :dlc, max_concurrent_positions = :mcp,
              position_notional_pct = :pnp, updated_at = now()
            WHERE id = :id
            RETURNING {SELECT_COLS}
            """
        ),
        {
            "id": strategy_id,
            "name": body.name,
            "mode": body.mode,
            "symbols": body.symbols,
            "dlc": body.daily_loss_cap_pct,
            "mcp": body.max_concurrent_positions,
            "pnp": body.position_notional_pct,
        },
    ).mappings().first()
    db.commit()
    if not row:
        raise HTTPException(404, "strategy not found")
    return _row_to_strategy(dict(row))


@router.post("/strategies/{strategy_id}/apply", response_model=ActionResult)
def apply_strategy(strategy_id: int, db: Session = Depends(get_db)) -> ActionResult:
    """Render the strategy DB row into the engine's YAML config.

    Does NOT restart the bot. The UI surfaces a 'restart required' flag once
    applied; the user must explicitly stop+start the bot to pick up changes.
    This keeps mid-tick safety guarantees intact (no silent restart).
    """
    row = db.execute(
        text(f"SELECT {SELECT_COLS} FROM strategies WHERE id = :id"),
        {"id": strategy_id},
    ).mappings().first()
    if not row:
        raise HTTPException(404, "strategy not found")

    try:
        target = yaml_writer.render_yaml(dict(row))
    except FileNotFoundError as e:
        raise HTTPException(500, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"yaml write failed: {e}")

    db.execute(
        text("UPDATE strategies SET applied_at = now() WHERE id = :id"),
        {"id": strategy_id},
    )
    db.commit()
    return ActionResult(
        ok=True,
        message=f"Wrote {target.name}. Restart the bot to apply.",
    )
