"""Phase 1 sub-task 1.4 — parity vs FakeBroker.

The keystone parity claim: ``SimulatedBroker`` is **functionally
equivalent** to the ``FakeBroker`` the production unit tests already
trust. Same Strategy class + same setup, run once through each, must
produce the same observable decisions (entries_submitted, denies, intent
reasons, open_trades state). If this passes, the simulator is
**anchored to a known-good reference**, not just to itself — and any
future drift in either side breaks here loudly.

What this PROVES
----------------
* No logic drift in the broker surface — same duck-typed contract,
  equivalent fill semantics on the perfect-fill path, equivalent
  anti-leak bar semantics for the inputs the strategy queries.
* The same Strategy code yields the same denies/entries when fed
  scenarios from the in-repo unit-test fixtures.

What this DOES NOT prove
------------------------
* Signal QUALITY (synthetic bars; says nothing about real-data edge).
* Realism of perfect-fill (Phase 1 limitation, still loudly labeled
  elsewhere; Phase 2 adds slippage/commissions).
* Byte-exact prices. ``SimulatedBroker`` stores DataFrame floats and
  recovers via ``Decimal(str(float))``; a price like ``Decimal("180.10")``
  may round-trip as ``Decimal("180.10000000000005")`` at the ~1e-13
  level. Strategy DECISIONS are unaffected (everything quantizes to
  cents downstream), but we compare prices at 0.01 tolerance.

The verdict-gate caveat (0.6 anti-leak hardening) is still in force —
build/run/inspect, not a verdict.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

_HAS_PMC = importlib.util.find_spec("pandas_market_calendars") is not None
requires_pmc = pytest.mark.skipif(
    not _HAS_PMC, reason="pandas_market_calendars not installed (research-only dep)",
)

pd = pytest.importorskip("pandas")

# Reach into the production tests for the canonical FakeBroker + its
# scenario helpers — this binds the parity test to the SAME fixtures
# the existing unit-test suite trusts.
_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))
from tests.test_strategy import (  # noqa: E402
    FakeBroker, _synth_trend, NOW, TEST_ENV, FIXTURE,
)

from strategy.config import load_config  # noqa: E402
from strategy.dto import Bar, Quote  # noqa: E402
from strategy.state import StateStore  # noqa: E402
from strategy.strategy import Strategy  # noqa: E402
from strategy.trade_log import RecordKind, TradeLog  # noqa: E402

from research.engine.sim_broker import SimulatedBroker  # noqa: E402
from research.engine.sim_trade_log import SimulatedTradeLog  # noqa: E402

UTC = timezone.utc


# --- shared scenario builders ---------------------------------------------

# Cents quantization — production engine treats prices as 2dp. We compare
# parity at this resolution so float-vs-Decimal noise can't masquerade as
# behavioral drift.
CENTS = Decimal("0.01")


def _bars_to_df(bars: list[Bar]):
    """Convert FakeBroker-style ``list[Bar]`` → SimulatedBroker DataFrame.

    Float round-trip is the precision-loss point; see module docstring.
    """
    idx = pd.DatetimeIndex([b.ts for b in bars], tz="UTC", name="ts")
    return pd.DataFrame({
        "open":   [float(b.open)   for b in bars],
        "high":   [float(b.high)   for b in bars],
        "low":    [float(b.low)    for b in bars],
        "close":  [float(b.close)  for b in bars],
        "volume": [b.volume        for b in bars],
    }, index=idx)


_TF_MIN_TO_NAME = {5: "5Min", 15: "15Min", 60: "1Hour"}


def _canonical_bars_clean_entry() -> dict[tuple[str, int], list[Bar]]:
    """Replicates tests/test_strategy.py's `broker` fixture bar setup —
    forces an entry-fireable trend on both AAPL and MSFT."""
    out: dict[tuple[str, int], list[Bar]] = {}
    for sym in ("AAPL", "MSFT"):
        out[(sym, 5)] = _synth_trend(
            sym, 120, start_price=Decimal("180"), step=Decimal("0.10"),
            start_ts=datetime(2026, 4, 23, 13, 35, tzinfo=UTC),
            tf_minutes=5, force_breakout=True,
        )
        out[(sym, 15)] = _synth_trend(
            sym, 80, start_price=Decimal("170"), step=Decimal("0.15"),
            start_ts=datetime(2026, 4, 23, 8, 0, tzinfo=UTC),
            tf_minutes=15,
        )
        out[(sym, 60)] = _synth_trend(
            sym, 250, start_price=Decimal("100"), step=Decimal("0.30"),
            start_ts=datetime(2026, 4, 15, 13, 35, tzinfo=UTC),
            tf_minutes=60,
        )
    return out


def _canonical_bars_stale() -> dict[tuple[str, int], list[Bar]]:
    """Same trend as the clean-entry scenario but 5Min bars stop 20
    minutes before NOW so the stale_bar risk gate fires."""
    out = _canonical_bars_clean_entry()
    for sym in ("AAPL", "MSFT"):
        # 5Min bars end 20 min before NOW → latest close_ts = NOW - 15min
        # → 900s old vs 330s freshness threshold → deny.
        out[(sym, 5)] = _synth_trend(
            sym, 120, start_price=Decimal("180"), step=Decimal("0.10"),
            start_ts=NOW - timedelta(minutes=5 * 120 + 20),
            tf_minutes=5, force_breakout=True,
        )
    return out


def _seed_state(s: Strategy) -> None:
    s.state.peak_equity = Decimal("25000")
    s.state.last_reconciled_equity = Decimal("25000")
    s.state.intraday_low_equity = Decimal("25000")


# --- runners --------------------------------------------------------------

def _make_cfg(tmp_path: Path):
    """Load the fixture config with paths redirected into tmp_path."""
    import yaml
    data = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    data["persistence"]["state_path"] = str(tmp_path / "state.json")
    data["persistence"]["trade_log_path"] = str(tmp_path / "trades.jsonl")
    out = tmp_path / "cfg.yaml"
    out.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_config(out, env=TEST_ENV)


def _run_via_fake(bars: dict[tuple[str, int], list[Bar]], tmp_path: Path) -> dict:
    cfg = _make_cfg(tmp_path)
    broker = FakeBroker()
    for sym in ("AAPL", "MSFT"):
        broker.latest_quotes[sym] = Quote(
            symbol=sym, ts=NOW,
            bid_price=Decimal("199.98"), ask_price=Decimal("200.02"),
            bid_size=100, ask_size=100,
        )
    for (sym, tf_min), bs in bars.items():
        broker.bars[(sym, _TF_MIN_TO_NAME[tf_min])] = bs

    state = StateStore(tmp_path / "state.json", fsync=False)
    log = TradeLog(tmp_path / "trades.jsonl", fsync=False)
    s = Strategy(cfg, broker, state, log)
    _seed_state(s)
    s.recover(NOW)
    rep = s.tick(NOW, kill_switch_present=False)

    records = list(log.read_all())
    return {
        "intents":   [r for r in records if r.kind is RecordKind.INTENT],
        "results":   [r for r in records if r.kind is RecordKind.RESULT],
        "incidents": [r for r in records if r.kind is RecordKind.INCIDENT],
        "entries":   sorted(rep.entries_submitted),
        "denies":    sorted((sym, reason) for sym, reason in rep.denies),
        "open":      dict(s.state.open_trades),
    }


def _run_via_sim(bars: dict[tuple[str, int], list[Bar]], tmp_path: Path) -> dict:
    cfg = _make_cfg(tmp_path)
    bar_dfs = {(sym, tf): _bars_to_df(bs) for (sym, tf), bs in bars.items()}
    broker = SimulatedBroker(bars=bar_dfs, starting_cash=Decimal("12500"), now=NOW)
    log = SimulatedTradeLog(clock=lambda: broker._require_now())

    state = StateStore(tmp_path / "sim_state.json", fsync=False)
    s = Strategy(cfg, broker, state, log)
    _seed_state(s)
    s.recover(NOW)
    rep = s.tick(NOW, kill_switch_present=False)

    records = list(log.read_all())
    return {
        "intents":   [r for r in records if r.kind is RecordKind.INTENT],
        "results":   [r for r in records if r.kind is RecordKind.RESULT],
        "incidents": [r for r in records if r.kind is RecordKind.INCIDENT],
        "entries":   sorted(rep.entries_submitted),
        "denies":    sorted((sym, reason) for sym, reason in rep.denies),
        "open":      dict(s.state.open_trades),
    }


# --- parity comparators ---------------------------------------------------

# Fields that legitimately differ between the two impls and must be
# ignored when comparing INTENT payloads.
_NON_STRUCTURAL_INTENT_KEYS = {
    "intent_id",           # contains ISO timestamp; clocks differ
    "client_order_id",     # depends on intent_id
}

_NON_STRUCTURAL_RESULT_KEYS = {
    "intent_id", "client_order_id",
    "broker_order_id",
    "protective_child_client_order_id", "protective_child_broker_id",
    # status enum string casing already pinned by test_integration_one_entry
}

_PRICE_KEYS = {
    "limit_price", "stop_price", "disaster_stop_price", "target_price",
    "equity_snapshot", "peak_equity", "drawdown_pct", "throttle_multiplier",
    "spread_bps", "avg_fill_price",
}


def _normalize_payload(p: dict, drop_keys: set[str]) -> dict:
    """Return a payload comparable across the two implementations.

    Drops non-structural keys (timestamps, coids); quantizes price-like
    Decimal strings to cents so float-vs-Decimal noise can't masquerade
    as behavioral drift.
    """
    out = {}
    for k, v in p.items():
        if k in drop_keys:
            continue
        if k in _PRICE_KEYS and isinstance(v, str):
            try:
                out[k] = str(Decimal(v).quantize(CENTS))
                continue
            except Exception:
                pass
        out[k] = v
    return out


def _by_symbol(records: list, key: str = "symbol") -> dict[str, list]:
    out: dict[str, list] = {}
    for r in records:
        sym = r.payload.get(key)
        out.setdefault(sym, []).append(r)
    return out


# --- parity tests ---------------------------------------------------------

@requires_pmc
def test_parity_clean_entry(tmp_path) -> None:
    """Same trending bars + same Strategy → same entries/intents/state
    through FakeBroker and SimulatedBroker."""
    bars = _canonical_bars_clean_entry()
    # Use disjoint tmp_paths so the two runs' state files don't collide.
    fake = _run_via_fake(bars, tmp_path / "fake")
    sim  = _run_via_sim(bars,  tmp_path / "sim")
    (tmp_path / "fake").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sim").mkdir(parents=True, exist_ok=True)

    # Behavior (the strongest claim).
    assert fake["entries"] == sim["entries"], (
        f"entries_submitted parity drift: fake={fake['entries']} sim={sim['entries']}"
    )
    assert fake["denies"] == sim["denies"], (
        f"denies parity drift: fake={fake['denies']} sim={sim['denies']}"
    )

    # open_trades structural equality.
    assert set(fake["open"].keys()) == set(sim["open"].keys())
    for sym in fake["open"]:
        ft, st = fake["open"][sym], sim["open"][sym]
        assert ft.qty == st.qty
        assert ft.entry_price.quantize(CENTS) == st.entry_price.quantize(CENTS)
        assert ft.stop_price.quantize(CENTS) == st.stop_price.quantize(CENTS)
        assert ft.target_price.quantize(CENTS) == st.target_price.quantize(CENTS)

    # Intent payload parity — per symbol, structural only.
    fake_intents_by_sym = _by_symbol(fake["intents"])
    sim_intents_by_sym  = _by_symbol(sim["intents"])
    assert set(fake_intents_by_sym) == set(sim_intents_by_sym)
    for sym in fake_intents_by_sym:
        # Pick the "submitting" intent for the entry — the only one we
        # care about for behavior parity on a clean path.
        fake_submit = [r for r in fake_intents_by_sym[sym]
                       if r.payload.get("result") == "submitting"]
        sim_submit  = [r for r in sim_intents_by_sym[sym]
                       if r.payload.get("result") == "submitting"]
        assert len(fake_submit) == len(sim_submit) == 1, sym
        fp = _normalize_payload(fake_submit[0].payload, _NON_STRUCTURAL_INTENT_KEYS)
        sp = _normalize_payload(sim_submit[0].payload,  _NON_STRUCTURAL_INTENT_KEYS)
        assert fp == sp, f"intent parity drift on {sym}: fake={fp} sim={sp}"

    # Result payload parity.
    assert len(fake["results"]) == len(sim["results"])
    for fr, sr in zip(
        sorted(fake["results"], key=lambda r: r.payload.get("intent_id") or ""),
        sorted(sim["results"],  key=lambda r: r.payload.get("intent_id") or ""),
    ):
        fp = _normalize_payload(fr.payload, _NON_STRUCTURAL_RESULT_KEYS)
        sp = _normalize_payload(sr.payload, _NON_STRUCTURAL_RESULT_KEYS)
        assert fp == sp, f"result parity drift: fake={fp} sim={sp}"


@requires_pmc
def test_parity_stale_bar_deny(tmp_path) -> None:
    """Same stale bars → both brokers cause the risk gate to fire
    ``stale_bar`` on every symbol; no entries on either side."""
    bars = _canonical_bars_stale()
    fake = _run_via_fake(bars, tmp_path / "fake")
    sim  = _run_via_sim(bars,  tmp_path / "sim")

    assert fake["entries"] == sim["entries"] == []
    # Both should deny every symbol with stale_bar.
    assert fake["denies"] == sim["denies"]
    assert all(reason == "stale_bar" for _, reason in fake["denies"])
    assert len(fake["denies"]) == 2  # AAPL + MSFT

    # No positions on either side.
    assert fake["open"] == {}
    assert sim["open"] == {}

    # DENY intents have matching deny_reasons (per symbol).
    fake_intents_by_sym = _by_symbol(fake["intents"])
    sim_intents_by_sym  = _by_symbol(sim["intents"])
    assert set(fake_intents_by_sym) == set(sim_intents_by_sym)
    for sym in fake_intents_by_sym:
        fake_denials = [r for r in fake_intents_by_sym[sym]
                        if r.payload.get("result") == "denied"]
        sim_denials  = [r for r in sim_intents_by_sym[sym]
                        if r.payload.get("result") == "denied"]
        assert len(fake_denials) == 1
        assert len(sim_denials) == 1
        assert fake_denials[0].payload["deny_reason"] == "stale_bar"
        assert sim_denials[0].payload["deny_reason"] == "stale_bar"
