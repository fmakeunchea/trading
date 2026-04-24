from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import bot_control
from ..db import SessionLocal, get_db
from ..schemas import SmokeTestResult

router = APIRouter()


def _run_and_record(run_id: int) -> None:
    """Background task: invoke smoke test, update the row when it finishes."""
    db = SessionLocal()
    try:
        try:
            code, stdout, stderr = bot_control.run_smoke_test()
        except Exception as exc:  # noqa: BLE001
            db.execute(
                text(
                    "UPDATE smoke_test_runs SET status='error', finished_at=now(), "
                    "stderr=:err WHERE id=:id"
                ),
                {"id": run_id, "err": repr(exc)},
            )
            db.commit()
            return
        status = _classify(code, stdout)
        db.execute(
            text(
                """
                UPDATE smoke_test_runs SET
                  status = :status, finished_at = now(),
                  exit_code = :code, stdout = :stdout, stderr = :stderr
                WHERE id = :id
                """
            ),
            {"id": run_id, "status": status, "code": code, "stdout": stdout, "stderr": stderr},
        )
        db.commit()
    finally:
        db.close()


def _classify(code: int, stdout: str) -> str:
    if code == 0:
        return "pass"
    # paper_smoke.py exits non-zero on fail; some sections may self-report SKIP
    if "SKIP" in stdout and "FAIL" not in stdout:
        return "skip"
    return "fail"


@router.post("/run-smoke-test", response_model=SmokeTestResult)
def run_smoke_test(
    bg: BackgroundTasks, db: Session = Depends(get_db)
) -> SmokeTestResult:
    row = db.execute(
        text(
            "INSERT INTO smoke_test_runs (status) VALUES ('running') "
            "RETURNING id, status, started_at, finished_at, exit_code, stdout, stderr"
        )
    ).mappings().first()
    db.commit()
    bg.add_task(_run_and_record, row["id"])
    return SmokeTestResult(**dict(row))


@router.get("/run-smoke-test/{run_id}", response_model=SmokeTestResult)
def get_smoke_test(run_id: int, db: Session = Depends(get_db)) -> SmokeTestResult:
    row = db.execute(
        text(
            """
            SELECT id, status, started_at, finished_at, exit_code, stdout, stderr
            FROM smoke_test_runs WHERE id = :id
            """
        ),
        {"id": run_id},
    ).mappings().first()
    if not row:
        raise HTTPException(404, "run not found")
    return SmokeTestResult(**dict(row))
