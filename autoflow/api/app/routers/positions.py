from fastapi import APIRouter

from .. import engine_io
from ..schemas import Position

router = APIRouter()


@router.get("/positions", response_model=list[Position])
def list_positions() -> list[Position]:
    return [Position(**p) for p in engine_io.open_positions()]
