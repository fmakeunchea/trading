"""Thin process entrypoint.

Responsibilities (and only these):
* Parse the config path from argv.
* Acquire a PID-based exclusive lock so only one instance runs per host.
* Install SIGTERM/SIGINT handlers for a graceful shutdown.
* Build the ``AlpacaBroker``, ``StateStore``, ``TradeLog``, and ``Strategy``.
* Run :meth:`Strategy.recover` once at startup.
* Loop: write heartbeat → read kill switch → call :meth:`Strategy.tick`
  → sleep ``tick_interval_s``.
* On shutdown: call :meth:`Strategy.graceful_shutdown` with ``flatten=True``
  during market hours, ``flatten=False`` otherwise.

No strategy logic here. No risk decisions. No broker calls that aren't
delegated through :class:`Strategy`. If something non-trivial needs
doing, it belongs in :mod:`strategy.strategy`, not here.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from strategy.broker import AlpacaBroker, RetryPolicy
from strategy.config import Config, load_config
from strategy.state import StateStore
from strategy.strategy import Strategy
from strategy.time_utils import now_utc
from strategy.trade_log import TradeLog


log = logging.getLogger("run_strategy")


# How often (in ticks) to emit an "alive" status line at INFO level so
# operators watching ``journalctl -f`` see proof of life during quiet
# periods (e.g. market closed, no signals). At a 10s tick interval
# 30 ticks = 5 minutes — enough to be reassuring without filling the
# journal. Set to 0 to disable.
STATUS_LOG_EVERY_N_TICKS = 30


# ---------------------------------------------------------------------------
# PID lock file — prevents two instances from racing the same account
# ---------------------------------------------------------------------------


class _ProcessLock:
    """A minimal PID lock.

    Uses ``O_CREAT | O_EXCL`` to acquire. If the file already exists and
    its PID is live, refuse to start. If the PID is stale (the previous
    instance died), replace and proceed.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._owned = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                pid = int(self.path.read_text().strip() or "-1")
            except (OSError, ValueError):
                pid = -1
            if pid > 0 and _pid_is_live(pid):
                raise RuntimeError(
                    f"another trading bot instance is running (pid={pid}); "
                    f"remove {self.path} if you're sure it isn't"
                )
            # Stale lock — take it over.
            log.warning("replacing stale lock %s (pid %s not live)", self.path, pid)
            self.path.unlink()
        fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        self._owned = True

    def release(self) -> None:
        if not self._owned:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self._owned = False


def _pid_is_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # PID exists but owned by someone else — still "live".
        return True
    return True


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def _write_heartbeat(path: Path, now: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(now.isoformat(), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_strategy(cfg: Config) -> Strategy:
    """Construct a :class:`Strategy` from a validated :class:`Config`.

    Factored out so tests can build a Strategy with a mock broker without
    calling ``main``.
    """
    broker = AlpacaBroker.from_credentials(
        api_key=cfg.broker.api_key.reveal(),
        api_secret=cfg.broker.api_secret.reveal(),
        paper=(cfg.mode == "paper"),
        data_feed=cfg.broker.data_feed,
        retry_policy=RetryPolicy(
            max_retries=cfg.broker.max_retries,
            base_s=cfg.broker.retry_backoff_base_s,
            cap_s=cfg.broker.retry_backoff_cap_s,
        ),
        poll_interval_s=cfg.broker.order_poll_interval_s,
        poll_timeout_s=cfg.broker.order_poll_timeout_s,
    )
    state_store = StateStore(cfg.persistence.state_path, fsync=cfg.persistence.state_fsync)
    trade_log = TradeLog(cfg.persistence.trade_log_path, fsync=cfg.persistence.state_fsync)
    return Strategy(cfg, broker, state_store, trade_log)


def run_loop(
    strategy: Strategy,
    cfg: Config,
    *,
    stop_flag: dict,
    now_fn=now_utc,
    sleep_fn=time.sleep,
) -> None:
    """Drive :meth:`Strategy.tick` until ``stop_flag['stop']`` is set.

    ``stop_flag`` is a single-key dict mutated by the signal handlers;
    the loop reads it each iteration. This keeps the loop free of
    global state while still responding to SIGTERM.
    """
    recovery = strategy.recover(now_fn())
    log.info(
        "recovery complete: halted=%s halt_reason=%s open_trades=%d "
        "repaired=%s dropped=%s unrecoverable=%s",
        recovery.halted,
        recovery.halt_reason,
        len(strategy.state.open_trades),
        recovery.repaired_extra_positions or [],
        recovery.dropped_missing_positions or [],
        recovery.unrecoverable or [],
    )

    tick_count = 0
    while not stop_flag.get("stop"):
        now = now_fn()
        try:
            _write_heartbeat(cfg.process.heartbeat_path, now)
        except OSError as exc:
            log.warning("heartbeat write failed: %s", exc)
        kill_present = cfg.process.kill_switch_path.exists()
        try:
            strategy.tick(now, kill_switch_present=kill_present)
        except Exception:  # noqa: BLE001 — tick must never crash the loop silently
            log.exception("tick raised; continuing next iteration")

        tick_count += 1
        if (
            STATUS_LOG_EVERY_N_TICKS > 0
            and tick_count % STATUS_LOG_EVERY_N_TICKS == 0
        ):
            active_halts = [
                name for name, rec in strategy.state.halts.items() if rec.active
            ]
            in_session = strategy.session_clock.is_within_session(now)
            log.info(
                "alive: ticks=%d open_trades=%d equity=%s "
                "in_session=%s halts=%s kill_switch=%s",
                tick_count,
                len(strategy.state.open_trades),
                strategy.state.last_reconciled_equity,
                in_session,
                active_halts or "none",
                kill_present,
            )
        sleep_fn(cfg.strategy.tick_interval_s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trading bot runner")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    cfg = load_config(args.config)
    log.info(
        "trading bot starting: pid=%d mode=%s config=%s data_feed=%s symbols=%s "
        "tick_interval_s=%s",
        os.getpid(),
        cfg.mode,
        args.config,
        cfg.broker.data_feed,
        list(cfg.strategy.symbols),
        cfg.strategy.tick_interval_s,
    )
    lock = _ProcessLock(cfg.process.lock_file)
    lock.acquire()
    log.info("process lock acquired: %s", cfg.process.lock_file)

    stop_flag: dict = {"stop": False, "flatten": True}
    strategy = build_strategy(cfg)
    log.info(
        "strategy built; entering main loop (session_window_utc=%s–%s, "
        "flat_before_close_min=%d)",
        cfg.risk.session_start_utc.strftime("%H:%M"),
        cfg.risk.session_end_utc.strftime("%H:%M"),
        cfg.execution.flat_before_close_minutes,
    )

    def _handle_signal(signum, frame):  # noqa: ARG001
        log.warning("received signal %s; requesting graceful shutdown", signum)
        stop_flag["stop"] = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        run_loop(strategy, cfg, stop_flag=stop_flag)
    finally:
        try:
            now = now_utc()
            in_session = strategy.session_clock.is_within_session(now)
            flatten = in_session and stop_flag.get("flatten", True)
            log.info(
                "graceful shutdown: in_session=%s flatten=%s open_trades=%d",
                in_session, flatten, len(strategy.state.open_trades),
            )
            strategy.graceful_shutdown(now, flatten=flatten)
        finally:
            lock.release()
            log.info("process lock released; exiting")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
