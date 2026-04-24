import time
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..schemas import Health

router = APIRouter()
_started = time.monotonic()


@router.get("/health", response_model=Health)
def health(db: Session = Depends(get_db)) -> Health:
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    var_ok = settings.engine_var_dir.exists()
    status = "ok" if (db_ok and var_ok) else "degraded"
    return Health(
        status=status,
        api_uptime_s=round(time.monotonic() - _started, 1),
        db_ok=db_ok,
        engine_var_dir_ok=var_ok,
    )
