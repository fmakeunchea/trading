"""Start / stop the trading-bot container via docker CLI.

Two assumptions:
    1. The API container has docker.sock mounted and the docker CLI installed.
    2. The bot container is named after the docker-compose service and lives
       in the same compose project as the API.

If either assumption breaks (e.g. bare-metal systemd deploy), swap this for
a subprocess call to `systemctl start trading-bot-paper.service` — the API
contract doesn't change.
"""
from __future__ import annotations

import subprocess

from .config import settings
from . import engine_io


def _docker(*args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=30
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _container_name() -> str:
    # docker-compose default: <project>-<service>-1. Matching by label is
    # more robust but needs docker SDK; good enough for MVP.
    return f"autoflow-{settings.trading_bot_service}-1"


def is_running() -> bool:
    # We consider the bot "running" when both the container is up AND the
    # engine's own PID lock points to a live process. This catches the case
    # where the container is up but the engine crashed inside its restart
    # backoff window.
    code, out, _ = _docker(
        "inspect", "-f", "{{.State.Running}}", _container_name()
    )
    container_up = code == 0 and out.strip() == "true"
    return container_up and engine_io.bot_pid_alive()


def start() -> tuple[bool, str]:
    code, out, err = _docker("start", _container_name())
    return code == 0, out or err


def stop() -> tuple[bool, str]:
    code, out, err = _docker("stop", _container_name())
    return code == 0, out or err


def run_smoke_test() -> tuple[int, str, str]:
    """Run the existing scripts/paper_smoke.py inside a one-shot container.

    Blocking; typical runtime is a few seconds. The /run-smoke-test endpoint
    wraps this in a background task + DB row so the UI can poll.
    """
    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{settings.engine_repo_dir}:/engine",
            "-w",
            "/engine",
            "-e",
            "ALPACA_API_KEY",
            "-e",
            "ALPACA_API_SECRET",
            "autoflow-trading-bot",
            "python",
            "scripts/paper_smoke.py",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    return proc.returncode, proc.stdout, proc.stderr
