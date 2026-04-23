# Paper-run runbook

This runbook covers the operational checks an operator must perform
during paper validation. It deliberately assumes nothing about prior
context: each section is a self-contained procedure.

The priority ordering is the same as the architecture plan: (1) prevent
catastrophic losses, (2) broker correctness, (3) audit integrity, (4)
deterministic restart, (5) risk controls. When in doubt, halt the bot
and investigate — **never keep trading on uncertain state.**

## 0. Prerequisites

- Paper credentials present in env or in `~/.alpaca_paper.env`:
  ```
  ALPACA_API_KEY=PK...
  ALPACA_API_SECRET=sk...
  ```
- `config/config.paper.yaml` points at paths the operator can write
  (`persistence.state_path`, `persistence.trade_log_path`,
  `process.lock_file`, `process.heartbeat_path`,
  `process.kill_switch_path`). The shipped defaults use `./var/`.
- Python venv active. `pytest` clean (all tests pass).

## 1. Startup checks

Run these before starting the bot each trading day.

1. **Kill switch clear:**
   ```
   test ! -e ./var/trading_bot.kill && echo "kill switch clear"
   ```
2. **No stale lock:**
   ```
   ls -la ./var/trading_bot.lock 2>/dev/null || echo "no lock present"
   ```
   If a lock file exists and its PID is not running, `run_strategy.py`
   will reclaim it automatically on next start. Do **not** manually
   delete a lock whose PID is still live.
3. **Trade log integrity:**
   ```
   python -m scripts.daily_summary --trade-log ./var/trades.jsonl | head -3
   ```
   Look for `audit integrity: OK`. If broken, do not start the bot;
   preserve the log for forensics before any further action.
4. **Previous day's summary reviewed:**
   ```
   python -m scripts.daily_summary --trade-log ./var/trades.jsonl \
       --date $(date -v-1d '+%Y-%m-%d')
   ```
   Ignore if this is the first run.
5. **Paper endpoint sanity check (connectivity only):**
   ```
   python -m scripts.paper_smoke --dry-run
   ```
   Expect one `[PASS] connectivity` line and exit code 0.
6. **Start the bot:**
   ```
   python run_strategy.py --config config/config.paper.yaml --log-level INFO \
       2>&1 | tee -a ./var/bot.log
   ```

## 2. Kill-switch test

Verify the kill switch blocks new entries but does **not** prevent
exit management.

1. With the bot running in-session, create the kill switch:
   ```
   touch ./var/trading_bot.kill
   ```
2. Watch `./var/bot.log` — within one `tick_interval_s` you should see
   a tick whose `TickReport.kill_switch_blocked=True`. No new INTENT
   records for entries.
3. If a position was already open, exits still fire on stop/target/time
   (tested in `test_kill_switch_does_not_block_exits`).
4. Remove the kill switch:
   ```
   rm ./var/trading_bot.kill
   ```
5. Confirm next tick resumes evaluating new entries.

## 3. Restart with an open position

Exercises atomic state persistence + reconciliation rebuild.

1. With one open trade, send `SIGTERM` to the bot process:
   ```
   kill -TERM $(cat ./var/trading_bot.lock)
   ```
2. Bot logs `requesting graceful shutdown`, flattens if in-session,
   releases the lock.
3. Inspect state:
   ```
   jq . < ./var/state.json
   ```
   Confirm `open_trades` reflects the broker's truth.
4. Restart:
   ```
   python run_strategy.py --config config/config.paper.yaml --log-level INFO \
       2>&1 | tee -a ./var/bot.log
   ```
5. `Strategy.recover` runs once: expect a clean `RecoveryReport` or a
   halt reason you can explain. An open trade at the broker that had a
   matching INTENT+RESULT in the log should be rebuilt. An open trade
   with no history triggers a halt — **this is correct behaviour;** do
   not silence it.

## 4. Session-end flatten test

Confirms the bot never holds overnight.

1. Close to the session end (default `19:45 UTC` with a 10-minute
   flatten buffer → flatten at `19:35 UTC`), hold at least one open
   trade.
2. Watch for a tick at or after `19:35 UTC` — bot emits
   `session_end_flatten` INTENT records and calls `flatten_symbol` on
   every open trade.
3. At `19:45 UTC`, `get_positions()` must return `[]`. Confirm with a
   final `daily_summary` run:
   ```
   python -m scripts.daily_summary --trade-log ./var/trades.jsonl --date today
   ```

## 5. Reconcile-mismatch response

This is the most important operational drill. Any reconcile mismatch
means **do not keep trading** until the mismatch is resolved.

### 5.1 How mismatches surface

- **Bot-side:** `TickReport.halted=True` with a `halt_reason` value, or
  `TickReport.reconcile_report.is_clean() == False`. An `INCIDENT`
  record of kind `reconcile_mismatch` is appended to the trade log.
- **Report-side:**
  ```
  python -m scripts.daily_summary --trade-log ./var/trades.jsonl
  ```
  Look at the `Incidents` section.

### 5.2 Categories and operator response

| Category | Meaning | What the bot does | What you do |
|---|---|---|---|
| `position_side_mismatch` | Account holds a short | Halts immediately | Close the short manually, investigate who/what created it |
| `orphan_protective_orders` | Protective stop with no matching local position | Blocks entries, halts on recovery | Cancel the orphan in Alpaca's web UI after confirming no matching position |
| `partial_fill_with_orphan_child` | Child qty doesn't match position qty | Blocks entries | Cancel child, verify position, optionally resubmit a correctly-sized stop manually |
| `missing_positions` | Local says open, broker says flat | Drops local trade on next recover; blocks entries that tick | Usually benign (e.g. broker-side disaster stop fired). Verify and resume. |
| `extra_positions` (no history) | Broker has a position with no matching INTENT+RESULT in the log | Halts on recovery | Either manually close via broker, or accept and rebuild state file with a known entry price and stop |
| `qty_mismatches` | Local qty ≠ broker qty | Blocks entries | Usually partial-fill echo; investigate and re-align state |
| `broker_order_with_unknown_coid` | Resting order without our `TBv1-` prefix | Surfaces warning, does **not** by itself block entries | Confirm whether another system is sharing the account; if so, stop the bot |
| `stale_local_orders` | Local pending, broker terminal | Blocks entries | Clear the stale pending entry from state and restart |

### 5.3 Incident commands

Inspect every INCIDENT in the last day:
```
python -m scripts.daily_summary --trade-log ./var/trades.jsonl --json \
    | jq '.incidents_by_kind'
```

Show the raw INCIDENT records:
```
jq -r 'select(.kind=="INCIDENT") | "\(.ts) \(.payload.kind) \(.payload)"' \
    ./var/trades.jsonl
```

## 6. What logs to inspect after each trade

For any trade (win, loss, flat):

1. Locate the intent id in stdout log: `grep "intent-<SYMBOL>-<ts>" ./var/bot.log`.
2. In `./var/trades.jsonl`, confirm the pair is present:
   ```
   jq -r 'select(.payload.intent_id=="<ID>")' ./var/trades.jsonl
   ```
   You should see exactly one INTENT + one RESULT per submission, and
   one INTENT + one RESULT for the close.
3. Confirm `status` on the RESULT matches what you expected. `filled`
   is the only happy case. `expired`, `canceled`, `rejected` are
   post-mortem events — investigate why.
4. For the close's RESULT, confirm `realized_pnl` is present and
   matches `(close_price - entry_price) × qty`.
5. Confirm there are no unmatched protective children remaining:
   ```
   jq -r 'select(.payload.symbol=="<SYMBOL>" and .payload.protective_child_broker_id != null)' \
       ./var/trades.jsonl
   ```

## 7. Daily wrap-up

At end-of-session:

1. Verify flat:
   ```
   python -c "from scripts.paper_smoke import build_broker, load_env; \
     from pathlib import Path; b=build_broker(load_env(Path.home()/'.alpaca_paper.env')); \
     print(b.get_positions())"
   ```
   Expect `[]`.
2. Generate summary:
   ```
   python -m scripts.daily_summary --trade-log ./var/trades.jsonl \
       --date $(date '+%Y-%m-%d')
   ```
3. Record the summary in a daily log (spreadsheet or text file) and
   track: trade count, win rate, expectancy, intraday DD, incident
   counts, error RESULT counts.
4. If any incident fired, do **not** clear the halt until root cause
   is identified and documented.

## 8. Running the paper smoke test

The smoke test places real orders on the paper account. Do this at
least once before your first paper-run day, and any time you change
`broker.py` or bump the `alpaca-py` pin:

```
python -m scripts.paper_smoke --yes
```

Exit codes:
- `0` — all stages pass, cleanup clean
- `1` — one or more stages failed
- `2` — precondition error (bad env, no `--yes`)
- `3` — stages passed but cleanup left residual state (operator must
  intervene manually before trading)

Run the smoke test with an open market for best coverage — several
stages only provide useful signal when the parent order can actually
fill.
