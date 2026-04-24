from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import bot_control as ctl
from .. import engine_io
from ..db import get_db
from ..schemas import ActionResult, BotStatus

router = APIRouter()


@router.get("/status", response_model=BotStatus)
def status(db: Session = Depends(get_db)) -> BotStatus:
    row = db.execute(
        text(
            """
            SELECT running, mode, kill_switch_engaged, heartbeat_at,
                   reconcile_ok, broker_connected, open_positions_count,
                   incidents_today
            FROM bot_state WHERE id = 1
            """
        )
    ).mappings().first()
    return BotStatus(
        running=bool(row["running"]),
        mode=row["mode"],
        kill_switch_engaged=bool(row["kill_switch_engaged"]),
        heartbeat_at=row["heartbeat_at"],
        heartbeat_fresh=engine_io.heartbeat_fresh(),
        reconcile_ok=row["reconcile_ok"],
        broker_connected=row["broker_connected"],
        open_positions_count=int(row["open_positions_count"]),
        incidents_today=int(row["incidents_today"]),
    )


@router.post("/start-bot", response_model=ActionResult)
def start_bot(db: Session = Depends(get_db)) -> ActionResult:
    # Refuse to start while kill-switch is engaged — surfaces the invariant
    # rather than having the engine halt on its first tick.
    if engine_io.kill_switch_engaged():
        return ActionResult(ok=False, message="Kill switch is engaged; release it first.")
    ok, msg = ctl.start()
    if ok:
        db.execute(
            text("UPDATE strategies SET last_restart_at = now() WHERE is_active")
        )
        db.commit()
    return ActionResult(ok=ok, message=msg)


@router.post("/stop-bot", response_model=ActionResult)
def stop_bot() -> ActionResult:
    ok, msg = ctl.stop()
    return ActionResult(ok=ok, message=msg)
