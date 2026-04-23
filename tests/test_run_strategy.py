"""Focused tests for the thin ``run_strategy`` driver.

What we pin down:
* The PID lock refuses a second live instance but reclaims stale locks.
* The loop calls ``tick`` with the kill-switch status derived from the
  filesystem and honours ``stop_flag['stop']``.
* The loop never lets a ``tick`` exception kill the process — it logs
  and continues.
* Heartbeat is written every iteration.

We do *not* exercise ``main`` end-to-end because that constructs the
real Alpaca SDK clients. The unit tests cover every risky branch of
the driver without requiring network access.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from run_strategy import _pid_is_live, _ProcessLock, run_loop


UTC = timezone.utc
NOW = datetime(2026, 4, 23, 14, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Process lock
# ---------------------------------------------------------------------------


def test_lock_acquires_and_releases(tmp_path: Path) -> None:
    lock = _ProcessLock(tmp_path / "bot.lock")
    lock.acquire()
    assert (tmp_path / "bot.lock").exists()
    lock.release()
    assert not (tmp_path / "bot.lock").exists()


def test_lock_refuses_concurrent_live_instance(tmp_path: Path) -> None:
    path = tmp_path / "bot.lock"
    path.write_text(str(os.getpid()), encoding="utf-8")  # our pid = live
    lock = _ProcessLock(path)
    with pytest.raises(RuntimeError, match="another trading bot"):
        lock.acquire()


def test_lock_reclaims_stale(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "bot.lock"
    path.write_text("999999", encoding="utf-8")

    import run_strategy
    monkeypatch.setattr(run_strategy, "_pid_is_live", lambda pid: False)

    lock = run_strategy._ProcessLock(path)
    lock.acquire()
    # New pid should now be written.
    assert int(path.read_text()) == os.getpid()
    lock.release()


def test_pid_is_live_self() -> None:
    # Our own pid must register as live.
    assert _pid_is_live(os.getpid()) is True


# ---------------------------------------------------------------------------
# run_loop
# ---------------------------------------------------------------------------


def _fake_strategy(kill_switch_seen: list[bool] | None = None,
                   tick_side_effect=None) -> MagicMock:
    s = MagicMock()
    def _tick(now, *, kill_switch_present):
        if kill_switch_seen is not None:
            kill_switch_seen.append(kill_switch_present)
        if tick_side_effect is not None:
            tick_side_effect(now, kill_switch_present)
        return MagicMock()
    s.tick.side_effect = _tick
    return s


def _fake_cfg(tmp_path: Path) -> MagicMock:
    cfg = MagicMock()
    cfg.process.heartbeat_path = tmp_path / "hb"
    cfg.process.kill_switch_path = tmp_path / "kill"
    cfg.strategy.tick_interval_s = 0.0
    return cfg


def test_run_loop_stops_when_flag_set(tmp_path: Path) -> None:
    strat = _fake_strategy()
    cfg = _fake_cfg(tmp_path)
    stop_flag = {"stop": False}
    times = iter([NOW] * 3)
    sleeps: list[float] = []
    def _sleep(s):
        sleeps.append(s)
        # Stop the loop after the first sleep call.
        stop_flag["stop"] = True
    run_loop(strat, cfg, stop_flag=stop_flag, now_fn=lambda: next(times), sleep_fn=_sleep)
    strat.recover.assert_called_once()
    # At least one tick called.
    assert strat.tick.call_count >= 1


def test_run_loop_passes_kill_switch_flag(tmp_path: Path) -> None:
    seen: list[bool] = []
    strat = _fake_strategy(kill_switch_seen=seen)
    cfg = _fake_cfg(tmp_path)
    kill_path = cfg.process.kill_switch_path

    # Create the kill-switch file between the first and second tick via
    # the sleep hook (sleep runs after each tick).
    stop_flag = {"stop": False}
    ticks_done = [0]
    def _sleep(_):
        ticks_done[0] += 1
        if ticks_done[0] == 1:
            kill_path.write_text("x")
        if ticks_done[0] >= 2:
            stop_flag["stop"] = True
    run_loop(strat, cfg, stop_flag=stop_flag, now_fn=lambda: NOW, sleep_fn=_sleep)
    assert seen == [False, True]


def test_run_loop_survives_tick_exception(tmp_path: Path, caplog) -> None:
    cfg = _fake_cfg(tmp_path)

    def _boom(now, kill_switch_present):
        raise RuntimeError("simulated tick failure")

    strat = _fake_strategy(tick_side_effect=_boom)
    stop_flag = {"stop": False}
    ticks = [0]
    def _sleep(_):
        ticks[0] += 1
        if ticks[0] >= 2:
            stop_flag["stop"] = True
    # Should complete without raising.
    run_loop(strat, cfg, stop_flag=stop_flag, now_fn=lambda: NOW, sleep_fn=_sleep)
    assert strat.tick.call_count >= 2


def test_run_loop_writes_heartbeat(tmp_path: Path) -> None:
    cfg = _fake_cfg(tmp_path)
    strat = _fake_strategy()
    stop_flag = {"stop": False}
    def _sleep(_):
        stop_flag["stop"] = True
    run_loop(strat, cfg, stop_flag=stop_flag, now_fn=lambda: NOW, sleep_fn=_sleep)
    assert (cfg.process.heartbeat_path).exists()
    assert NOW.isoformat() in cfg.process.heartbeat_path.read_text(encoding="utf-8")
