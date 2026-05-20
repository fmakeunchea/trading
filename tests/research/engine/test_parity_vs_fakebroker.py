"""Phase 1 sub-task 1.4 — parity vs FakeBroker.

The keystone parity claim: ``SimulatedBroker`` is **functionally
equivalent** to the ``FakeBroker`` the production unit tests already
trust, *on the shared semantic space*. Same Strategy class + same
setup, run once through each, must produce the same observable
decisions (entries_submitted, denies, open_trades state).

The "shared semantic space" caveat matters — see the finding below.

Finding (anti-leak gap)
-----------------------
The production ``FakeBroker`` does NOT enforce anti-leak: its
``get_bars`` returns everything in its dict regardless of the current
clock. The in-repo unit-test fixtures exploit this — they install bars
whose ``ts`` extends **after** ``NOW`` and rely on the strategy
seeing them anyway. The ``SimulatedBroker`` (correctly) clips at
``ts <= now - tf``; with the same fixture bars, it returns ~11 bars,
far below ``min_bars_entry_tf=100``, and the entry path is silently
short-circuited.

This is not a bug in either side. It is a meaningful semantic
divergence: the Phase-0 anti-leak invariant ("future-bar access
impossible by construction") is a stricter guarantee than the
unit-test FakeBroker provides. Reusing FakeBroker fixture bars
verbatim through SimulatedBroker is incorrect.

Resolution for this test: build bars that are entirely **causal**
relative to ``NOW`` (last bar's ``ts`` < ``NOW``) so both brokers
see the same bar set. The parity claim narrows to the shared space
both implementations correctly model, which is exactly what we need
to prove "no logic drift in the broker surface."

This finding will be revisited and pinned by 0.6 (anti-leak hardening
tests) — see [[project_backtester_design]].

What this PROVES
----------------
* On causally-correct inputs, same Strategy + same bars + equivalent
  quote semantics → same entries_submitted, same denies, same
  open_trades structure across both broker implementations.
* The SimulatedBroker's perfect-fill semantics match the FakeBroker's
  perfect-fill semantics on the shared path.

What this DOES NOT prove
------------------------
* Signal QUALITY (synthetic bars; says nothing about real-data edge).
* Realism of perfect-fill (Phase 1 limitation, loudly labeled).
* Byte-equal payload fields. Float-vs-Decimal precision (DataFrame
  floats round-trip via ``Decimal(str(float))`` at ~1e-13). Strategy
  DECISIONS are unaffected; price-like fields are compared at cents
  resolution.
* Equivalence under FakeBroker's looser anti-leak semantics —
  intentionally out of scope (see Finding above).

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


def _uptrend(symbol: str, last_ts: datetime, n_bars: int, tf_min: int,
             *, base: Decimal = Decimal("100.00"),
             step: Decimal = Decimal("0.15"),
             wick: Decimal = Decimal("0.10")) -> list[Bar]:
    """Monotonic uptrend bars ending at ``last_ts`` (causal — all in the
    past of any future ``now``).

    step > wick => close[i] > high[i-1], so the breakout condition
    fires on every bar after indicator warmup (same pattern as
    ``test_integration_one_entry.py``). ATR/close stays in
    [0.0005, 0.03] for typical price ranges.
    """
    bars: list[Bar] = []
    for i in range(n_bars):
        ts = last_ts - timedelta(minutes=tf_min * (n_bars - 1 - i))
        close = base + step * i
        prev_close = base if i == 0 else (base + step * (i - 1))
        bars.append(Bar(
            symbol=symbol, ts=ts,
            open=prev_close,
            high=close + wick,
            low=close - wick,
            close=close,
            volume=1000,
        ))
    return bars


def _flat(symbol: str, last_ts: datetime, n_bars: int, tf_min: int,
          *, price: Decimal = Decimal("100.00")) -> list[Bar]:
    """Flat-price bars — no breakout possible."""
    bars: list[Bar] = []
    for i in range(n_bars):
        ts = last_ts - timedelta(minutes=tf_min * (n_bars - 1 - i))
        bars.append(Bar(
            symbol=symbol, ts=ts,
            open=price, high=price, low=price, close=price, volume=1000,
        ))
    return bars


# Per-TF bar counts comfortably above min_bars_*_tf (100 / 60 / 210).
_N_BARS = {5: 130, 15: 80, 60: 250}


def _canonical_bars_clean_entry() -> dict[tuple[str, int], list[Bar]]:
    """Causal bars (all ts < NOW): AAPL rises, MSFT flat.

    AAPL's monotonic uptrend triggers a breakout on every bar after
    warmup. MSFT's flat series cannot break out → signal-side denial,
    only DIAGNOSTIC incidents.

    Each timeframe's last ``ts = NOW - tf_min`` so the latest bar's
    ``close_ts == NOW`` (fresh under both brokers). The SimulatedBroker
    clip at ``ts <= now - tf`` keeps the full set in scope; the
    FakeBroker returns the full set unconditionally. Both brokers thus
    expose the strategy to the same bar set.
    """
    out: dict[tuple[str, int], list[Bar]] = {}
    for tf_min in (5, 15, 60):
        last_ts = NOW - timedelta(minutes=tf_min)
        out[("AAPL", tf_min)] = _uptrend("AAPL", last_ts, _N_BARS[tf_min], tf_min)
        out[("MSFT", tf_min)] = _flat("MSFT", last_ts, _N_BARS[tf_min], tf_min)
    return out


def _canonical_bars_stale() -> dict[tuple[str, int], list[Bar]]:
    """All-uptrend bars where 5Min stops 20 minutes before NOW.

    Latest 5Min close_ts = NOW - 15min = 900s old; the gate threshold
    is 5min + 30s grace = 330s. 900 > 330 → stale_bar deny.

    BOTH symbols need uptrend bars (not just AAPL) so the signal
    passes first and the risk gate is actually exercised — otherwise
    MSFT would short-circuit on `no_breakout` (signal-side denial)
    and never reach the stale check.
    """
    out: dict[tuple[str, int], list[Bar]] = {}
    last_5m = NOW - timedelta(minutes=20)
    for sym in ("AAPL", "MSFT"):
        # 15Min / 1Hour bars stay fresh (latest close_ts == NOW); only
        # 5Min is stale.
        out[(sym, 5)]  = _uptrend(sym, last_5m,
                                  _N_BARS[5], 5)
        out[(sym, 15)] = _uptrend(sym, NOW - timedelta(minutes=15),
                                  _N_BARS[15], 15)
        out[(sym, 60)] = _uptrend(sym, NOW - timedelta(minutes=60),
                                  _N_BARS[60], 60)
    return out


def _latest_visible_close(bars: dict[tuple[str, int], list[Bar]],
                          symbol: str, now: datetime) -> Decimal:
    """Match SimulatedBroker's quote synthesis: prefer the smallest tf
    whose latest bar has ``close_ts <= now``, return its close."""
    available = sorted(tf for (s, tf) in bars if s == symbol)
    for tf_min in available:
        usable = [b for b in bars[(symbol, tf_min)]
                  if b.ts <= now - timedelta(minutes=tf_min)]
        if usable:
            return usable[-1].close
    raise AssertionError(f"no visible bar for {symbol} at {now}")


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
    # Override the FakeBroker default quote so spread semantics match
    # SimulatedBroker's Phase-1 synthesis (bid==ask==latest-visible
    # close, spread_bps==0). Without this, the FakeBroker would
    # advertise a hardcoded 4-bps spread and the intent payload's
    # `spread_bps` field would diverge between runs even though the
    # downstream decision is identical.
    for sym in ("AAPL", "MSFT"):
        mid = _latest_visible_close(bars, sym, NOW)
        broker.latest_quotes[sym] = Quote(
            symbol=sym, ts=NOW,
            bid_price=mid, ask_price=mid,
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
    # SimulatedBroker computes equity = cash + position_value. The
    # FakeBroker default hardcodes equity=25000 (cash=12500). Strategy
    # uses equity for sizing — to make both runs see the same equity
    # at decision time, give the sim equivalent cash so equity matches.
    broker = SimulatedBroker(bars=bar_dfs, starting_cash=Decimal("25000"), now=NOW)
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
    # Disjoint tmp subdirs so the two runs' state/log files don't collide.
    # MUST exist before _run_via_* (their _make_cfg writes cfg.yaml into them).
    (tmp_path / "fake").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sim").mkdir(parents=True, exist_ok=True)
    fake = _run_via_fake(bars, tmp_path / "fake")
    sim  = _run_via_sim(bars,  tmp_path / "sim")

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
    (tmp_path / "fake").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sim").mkdir(parents=True, exist_ok=True)
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
