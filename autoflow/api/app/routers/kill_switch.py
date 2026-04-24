from fastapi import APIRouter

from .. import engine_io
from ..schemas import ActionResult, KillSwitchState

router = APIRouter()


@router.get("/kill-switch", response_model=KillSwitchState)
def get_kill_switch() -> KillSwitchState:
    return KillSwitchState(engaged=engine_io.kill_switch_engaged())


@router.post("/kill-switch", response_model=ActionResult)
def set_kill_switch(body: KillSwitchState) -> ActionResult:
    if body.engaged:
        engine_io.engage_kill_switch()
        return ActionResult(ok=True, message="Kill switch engaged. Engine will halt on next tick.")
    engine_io.disengage_kill_switch()
    return ActionResult(ok=True, message="Kill switch released.")
