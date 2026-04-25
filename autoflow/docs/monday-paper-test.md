# Monday market-open paper test — runbook

First end-to-end engine-connected paper test through the production stack
at https://autoflowtrader.com. Goal: verify the engine actually trades
through the deployed setup and we capture every artifact we'd need if
something goes wrong.

**This is paper-mode only. Live mode is gated until paper has run clean
for at least one full session.**

---

## Timeline (UTC)

The engine's session window in `config/config.paper.yaml` is
`13:35–19:45 UTC`. That's 09:35–15:45 ET — US regular market hours minus
the open/close minutes the engine deliberately skips.

| Time (UTC) | What you do |
|---|---|
| 13:00 | Start the pre-flight checklist below. Be at your desk. |
| 13:30 | Bot started. Heartbeat green. Monitor open. |
| 13:35 | Session opens. Engine starts looking for entries. |
| 13:35–14:00 | First-25-min observation window — most likely time to spot integration issues. Stay here. |
| 14:00–19:45 | Background watch. Check every 30–60 min. |
| 19:35 | `flat_before_close_minutes=10` triggers; engine flattens any open positions. |
| 19:45 | Session closes. Engine stops attempting entries. |
| 19:50 | Post-session checklist below. Stop bot. |

If you can't be at your desk for the open, **don't run this test that
day**. Wait for a day you can.

---

## Pre-flight (T-30 min)

1. UptimeRobot dashboard open in one tab — confirm `AutoFlow API health` is green.
2. https://autoflowtrader.com/dashboard open in a second tab — auth, confirm Stopped state.
3. SSH session into VPS open in a third tab. Run:

   ```bash
   docker compose -f /opt/trading-bot/autoflow/docker-compose.yml \
     -f /opt/trading-bot/autoflow/docker-compose.traefik.yml ps
   ```

   Expect web/api/postgres all `Up`, trading-bot in `Created` or `Exited` (not Up — we'll start it from the dashboard).

4. Confirm your Alpaca paper account is the one configured:

   ```bash
   grep ALPACA_API_KEY /opt/trading-bot/autoflow/.env | sed 's/=.*/=<redacted>/'
   ```

   Just confirms the key is present.

5. Tail the engine + sync logs in two more terminals (small panes):

   ```bash
   # Terminal A — engine logs (will be empty until you Start)
   docker logs autoflow-trading-bot-1 -f --tail=10
   ```

   ```bash
   # Terminal B — API + sync worker logs
   docker compose -f /opt/trading-bot/autoflow/docker-compose.yml \
     -f /opt/trading-bot/autoflow/docker-compose.traefik.yml logs -f api
   ```

6. Pre-create a scratch note (a markdown file or a piece of paper). Plan
   to log: timestamps of every interesting event, every WTF moment,
   every time you reach for a command not in this runbook.

---

## T-5 min: Start

In the dashboard, click **Start bot**. Watch:

- **Strategy** card: `Stopped (IDLE)` → `Running (OK)` within ~2s
- **Heartbeat** card: `Stale (BAD)` → `Fresh (OK)` within ~15s
- **Mode**: shows `paper`

In Terminal A you should see:

```
trading bot starting: pid=1 mode=paper config=config/config.paper.yaml ...
process lock acquired: var/trading_bot.lock
strategy built; entering main loop (session_window_utc=13:35–19:45, ...)
recovery complete: halted=False halt_reason=None open_trades=0 ...
```

If any of those don't appear, stop and debug — don't proceed into
session.

---

## T-0: Session opens (13:35 UTC)

Watch Terminal A. The engine logs an `alive:` line every 30 ticks (~5
min). Expect the first one around 13:40:

```
alive: ticks=30 open_trades=0 equity=<value> in_session=True halts=none kill_switch=False
```

`in_session` flipping to `True` is the signal that the engine entered
its trading window correctly.

---

## What success looks like

A successful session has any of the following — none are required, but
seeing **one of them** confirms the data path:

- **A trade taken**: an `INTENT` then `RESULT` record appears in
  `trades.jsonl`. The dashboard's "Recent trades" tab populates.
- **A signal evaluated but rejected**: visible in engine logs as `signal
  rejected: reason=<...>`. No trade, but proves the strategy logic ran.
- **An incident logged**: e.g. a reconcile event, a stale-quote skip.
  Surfaces in `/incidents` page.

If the session is dead quiet (no trades, no signals, no incidents),
that's NOT a failure of AutoFlow — it's the strategy not seeing setups
today. The infrastructure check is whether **state.json**,
**trades.jsonl**, and the **heartbeat** all keep getting written.

### Verify the data path is alive (run any time during session)

```bash
ls -la /opt/trading-bot/var/
```

Expect:
- `state.json` mtime within last minute
- `trading_bot.heartbeat` mtime within last 30s (heartbeat tick interval)
- `trades.jsonl` exists (created on first audit-log write)
- `trading_bot.lock` exists, points at the running container PID

```bash
wc -l /opt/trading-bot/var/trades.jsonl
tail -3 /opt/trading-bot/var/trades.jsonl | jq .
```

Expect: line count grows over the session even if no trades are taken
— at minimum, periodic `SEAL` records appear.

```bash
read -rsp 'AutoFlow password: ' AF_PASS && echo
curl -s -u "fifi:$AF_PASS" https://autoflowtrader.com/api/status | python3 -m json.tool
unset AF_PASS
```

Expect: `running=true`, `heartbeat_fresh=true`, `mode=paper`,
`broker_connected=true`. `incidents_today` may climb during the session.

---

## What failure looks like

Any of these is a **stop-the-test** signal. Engage the kill switch from
the dashboard or call `POST /api/kill-switch {"engaged": true}`, then
investigate.

| Symptom | What to look for | Where |
|---|---|---|
| Heartbeat stops advancing | Engine froze or crashed | Terminal A; `docker ps -a` |
| `incidents_today` jumps without obvious cause | Reconcile drift, orphan order | `/incidents` page |
| Container exited mid-session | Crash with traceback | `docker logs autoflow-trading-bot-1 --tail=50` |
| Repeated `signal rejected: reason=stale_data` | Broker data feed is stale | engine logs; check Alpaca status page |
| Repeated `RECONCILE` incidents | Broker state diverged from local | `/incidents`; verify Alpaca dashboard |
| UptimeRobot fires "DOWN" | Web/API/proxy issue | `docker compose ps`; `docker logs` |
| Position quantity doesn't match Alpaca | Reconcile broken | Compare `state.json.open_trades` to Alpaca dashboard |

### Emergency stop

```bash
# Via API (preferred):
curl -s -X POST -u "fifi:$AF_PASS" \
  -H 'Content-Type: application/json' -d '{"engaged": true}' \
  https://autoflowtrader.com/api/kill-switch

# Direct on disk (works even if API is down):
touch /opt/trading-bot/var/trading_bot.kill

# Stop the container outright:
docker stop autoflow-trading-bot-1
```

Kill switch is preferred — it lets the engine finish whatever broker
call it's mid-flight before halting on its next tick. Stopping the
container hard mid-call can leave broker state ahead of local state.

---

## Post-session (after 19:45 UTC)

1. Click **Stop bot** in the dashboard. Confirm `Strategy` flips to `Stopped`.
2. Capture a snapshot of artifacts for the post-mortem (whether it went
   well or not):

   ```bash
   mkdir -p ~/autoflow-test-runs/$(date -u +%Y%m%d)
   cd ~/autoflow-test-runs/$(date -u +%Y%m%d)
   cp /opt/trading-bot/var/state.json .
   cp /opt/trading-bot/var/trades.jsonl .
   docker logs autoflow-trading-bot-1 > engine.log 2>&1
   docker compose -f /opt/trading-bot/autoflow/docker-compose.yml \
     -f /opt/trading-bot/autoflow/docker-compose.traefik.yml \
     logs api > api.log 2>&1
   docker exec autoflow-postgres-1 psql -U autoflow -d autoflow \
     -c "SELECT * FROM incidents WHERE occurred_at::date = current_date" > incidents.txt
   docker exec autoflow-postgres-1 psql -U autoflow -d autoflow \
     -c "SELECT * FROM trades   WHERE occurred_at::date = current_date" > trades.txt
   ls -la
   ```

3. Take the scratch note and turn it into a markdown summary in the
   same directory: what worked, what surprised you, what you'd change.

4. Confirm tonight's backup will pick up the day's data:

   ```bash
   docker exec autoflow-postgres-1 psql -U autoflow -d autoflow \
     -c "SELECT count(*) FROM incidents WHERE occurred_at::date = current_date;
         SELECT count(*) FROM trades    WHERE occurred_at::date = current_date;"
   ```

   Whatever those counts are, they should appear in tomorrow's 03:00 UTC
   pg_dump and 03:30 UTC B2 sync. Tomorrow morning, verify:

   ```bash
   ls -lh /opt/backups/autoflow/ | tail -3
   rclone ls b2:autoflow-backups-fifi/ | tail -3
   ```

---

## Decision matrix after the test

| Outcome | Next step |
|---|---|
| Clean session, at least 1 alive log per 5 min, no crashes, no unexplained incidents | Repeat tomorrow. After 5 clean paper days, you can consider the live-mode gate. |
| Crashes / unexplained incidents | Don't repeat tomorrow. Open the artifacts directory, debug, push fixes. |
| No engine activity at all (no trades, no signals, no rejections) | Strategy didn't see setups — that's normal-ish. Confirm via engine logs that ticks ran and `in_session=True`. Re-test next session. |
| AutoFlow infra issue (heartbeat froze, container died, proxy 5xx) | This is the headline failure mode AutoFlow exists to catch. Captured in artifacts. Triage; don't blame the strategy. |

---

## Things NOT to do during the test

- Don't edit strategy config mid-session and click Apply. The
  restart-required UX is there for a reason — restarting the engine in
  the middle of an open position will fire reconcile alerts.
- Don't toggle paper ↔ live in the editor. You're in paper today.
- Don't deploy code changes to api/web mid-session. Anything that
  recreates containers will yank the docker socket the bot relies on.
- Don't restart Traefik or the VPS. Both interrupt the engine.
- Don't run the smoke test against the live engine. The smoke test
  spawns a separate one-shot container that contends for the same
  Alpaca paper account; orders can collide.

If you must do any of the above, kill switch first, stop the bot, then
do the thing, then restart.
