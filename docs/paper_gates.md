# Paper-test pass/fail gates before tiny live rollout

Paper validation is not a vibes check. A tier-0 live rollout with real
money only starts when every gate below has been met in writing and an
operator has signed off on the summary.

Each gate is a **hard** pass/fail. One failure means "do not advance"
— not "investigate and maybe". If a gate is failing for a reason we
understand and intend to accept, we must amend this document before
advancing, not hand-wave it away.

## Summary

| Gate | Threshold | Source of truth |
|---|---|---|
| G1 — Smoke test | all stages pass, cleanup clean | `scripts/paper_smoke.py --yes` exit 0 |
| G2 — Duration | ≥ 4 continuous weeks of paper runs | daily summaries |
| G3 — Trade count | ≥ 50 closed trades | `daily_summary.n_trades` aggregated |
| G4 — Audit integrity | 100% of days: `audit_integrity_ok` | each daily summary |
| G5 — Reconcile incidents | 0 unresolved on any session | INCIDENT log |
| G6 — Orphan incidents | 0 on any session | INCIDENT log |
| G7 — Halts | 0 unexplained halts | INCIDENT log + operator notes |
| G8 — Error RESULTs | < 1% of submissions | daily summaries |
| G9 — Profit factor | ≥ 1.3 over the window | aggregated closes |
| G10 — Expectancy | > 0 after costs | aggregated closes |
| G11 — Max drawdown | ≤ 2 × backtest-predicted DD | daily summaries |
| G12 — Worst week | no week below −5% of starting equity | weekly rollup |
| G13 — Slippage | realized ≤ 1.5 × backtest assumption | per-trade fill vs limit |
| G14 — Flat-by-close | 100% of sessions end flat | `get_positions()` at +5 min after close |
| G15 — Restart recovery | every restart with an open position results in either a clean rebuild or a halt | operator log |

## Detailed gates

### G1 — Smoke test

**Criteria:** `python -m scripts.paper_smoke --yes` exits 0 with all
stages PASS and `cleanup: no residual orders or position`. Must be
re-run:
- Before the first paper-run day.
- On any version bump of `alpaca-py`.
- After any change to `strategy/broker.py`.
- After any change to the orchestrator's flatten / recover paths.

### G2 — Duration

**Criteria:** At least 20 trading days (≈ 4 calendar weeks) of
continuous paper running. A day counts only if the bot was running for
≥ 90% of the session window.

### G3 — Trade count

**Criteria:** Summed `n_trades` (closed round trips) across the window
≥ 50. If signal quality is producing < 50 closes in 4 weeks, the
sample is too small to draw conclusions from — extend the window,
don't lower the bar.

### G4 — Audit integrity

**Criteria:** Every daily summary reports `audit integrity: OK`. One
broken chain is a P0 — the audit trail is the only thing protecting
the operator from misunderstanding what happened.

### G5 / G6 / G7 — Safety incidents

**Criteria:** Zero unresolved `reconcile_mismatch`, zero
`orphan_protective_orders`, and zero `partial_fill_with_orphan_child`
across the window. Zero unexplained halts.

"Unresolved" means the incident was recorded and the operator cannot
point at a clear root cause + fix in the runbook.

### G8 — Error RESULTs

**Criteria:** `error_results / n_trades < 0.01`. Error RESULTs are
submissions that reached the broker but terminalised with an error we
couldn't classify. A handful is tolerable; a pattern is not.

### G9 — Profit factor

**Criteria:** Over the full window, `sum(wins) / abs(sum(losses)) ≥
1.3`. Profit factor is a cleaner signal than win rate for trend
strategies (which often have < 50% win rate but larger wins).

### G10 — Expectancy

**Criteria:** `expectancy_per_trade > 0` after including simulated
commissions and realistic slippage. The bot runs on Alpaca's
commission-free equities, but we still count a conservative per-share
slippage allowance (default 1 cent per share, configurable).

### G11 — Max drawdown

**Criteria:** Observed `intraday_drawdown_pct` from the summary ≤ 2 ×
the drawdown the backtest predicted. If the backtest had no
drawdown-prediction, treat this gate as "≤ 6% intraday DD" for v1.

### G12 — Worst week

**Criteria:** No Monday–Friday calendar week shows a net realized PnL
worse than −5% of that week's starting equity. A single bad week
above the threshold means one more full week of observation before
advancing.

### G13 — Slippage

**Criteria:** For each filled entry, compare `avg_fill_price` against
the limit price. The mean absolute slippage across the window must be
≤ 1.5 × the slippage assumption used in the backtest. If no backtest
slippage number exists, treat this as "mean fill is within 8 bps of
limit".

### G14 — Flat-by-close

**Criteria:** 100% of sessions end with zero open positions at
`session_end + 5 min`. The bot's `session_end_flatten` is tested, but
this gate verifies it fires in the wild on every real session.

### G15 — Restart recovery

**Criteria:** Every operator-initiated restart (SIGTERM, crash,
machine reboot) either:
- Rebuilds the OpenTrade cleanly from the trade log and resumes, or
- Halts with a clear `RecoveryReport.halt_reason` that the operator
  documents.

No restart may result in an open broker position with no local state,
and no restart may silently drop a position.

## Advancement procedure

When every gate above is green for a full window:

1. Operator writes a one-page "paper validation summary" covering
   each gate with the supporting number / file path.
2. Operator files the summary alongside the runbook (recommended:
   `docs/paper_run_<YYYY-MM-DD>.md`).
3. Switch to `config/config.live.yaml` with **tier-0 caps**:
   - `sizing.starting_equity`: real account equity
   - `sizing.position_notional_pct`: 1–2% (not the paper value)
   - `sizing.max_trade_notional`: $500
   - `sizing.max_total_exposure_pct`: 0.10
   - `risk.max_concurrent_positions`: 1
   - `risk.daily_loss_cap_pct`: 0.01
   - everything else as paper-validated
4. Re-run `scripts/paper_smoke.py` against a live-paper endpoint a
   final time.
5. First live run is **observed** by the operator for its full first
   session. No "start and walk away" for tier 0.

## Demotion triggers

Any of these during a live window demotes one tier (or halts, if at
tier 0):
- Any G4, G5, G6, G7 violation.
- Realized slippage exceeding 2 × backtest assumption.
- Drawdown halt triggered.
- Daily loss halt triggered.
- Reconcile mismatch that blocked entries for a full session.
