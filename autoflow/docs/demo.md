# Beta-user demo checklist

15-minute walkthrough. Goal: prospect leaves convinced AutoFlow is the
operations layer they wish they had before deploying their bot.

## Before you start

- [ ] Bot is **stopped** (clean slate)
- [ ] Kill switch is **off**
- [ ] At least one prior smoke-test run is in the DB (so the screen has content)
- [ ] Browser already authed past basic auth so you're not fumbling at credentials

## Demo flow

### 1. Dashboard status (1 min)
Open `/dashboard`. Walk through each card:

- [ ] **Strategy: Stopped (IDLE)** — "we haven't started anything yet"
- [ ] **Heartbeat: Stale (BAD)** — "this turns green when the engine is ticking. If it doesn't, you find out in seconds"
- [ ] **Reconcile: —** — "shows IN SYNC vs DRIFT. Drift is the silent killer; we surface it"
- [ ] **Broker: Unknown (WARN)** — "broker connectivity is its own card"

Point at the empty positions/trades tables. "Will populate live once the engine runs."

### 2. Start bot → heartbeat green (1 min)

- [ ] Click **Start bot** (top right)
- [ ] Heartbeat card flips to **Fresh (OK)** within 15 seconds
- [ ] Strategy card flips to **Running**

"That's the engine ticking. The card is bound to a heartbeat file the
engine writes every tick — if the engine froze, you'd see this go stale."

### 3. Kill switch (2 min) — the headline feature

- [ ] Toggle the kill-switch card on
- [ ] Open a terminal. Show the file appears: `ls /opt/trading-bot/var/trading_bot.kill`
- [ ] Show engine logs: `docker compose logs --tail=5 trading-bot` — `kill_switch=True`
- [ ] Toggle off. File disappears.

"This isn't an API call to the broker — it's a filesystem signal the engine
checks every tick. No network, no race. If our API is down, you can still
`touch trading_bot.kill` over SSH."

### 4. Strategy config (2 min)

- [ ] Navigate to `/strategy`
- [ ] Edit a value (e.g. lower `daily_loss_cap_pct`). Click Save.
- [ ] Note the **Unsaved → YAML** badge appears
- [ ] Click **Apply to YAML**. Note the **Restart bot to take effect** badge

"DB is the source of truth for what you intend; YAML is what the engine reads.
We never silently restart your bot — that would be a footgun. You apply, then
explicitly restart when you're ready."

### 5. Smoke test (3 min) — the trust-builder

- [ ] Navigate to `/smoke-test`
- [ ] Click **Run smoke test**
- [ ] Card transitions RUNNING → PASS (~30–60s)
- [ ] Show the stdout block: OTO placement, cancel, flatten, partial-fill verified

"This is what we run on every paper deploy before we'll let you flip to
live. Most platforms ship live mode and hope. We make you prove the
broker round-trips work first."

### 6. Incident log (2 min)

- [ ] Navigate to `/incidents`
- [ ] Show the table: kind, severity, symbols, reason, time
- [ ] If empty, that's also the story: "nothing has gone wrong"
- [ ] If you want to populate live, toggle the kill switch during a market
      session — the engine writes a HALT incident and it appears here

"Every reconcile event, halt, orphan-order alert, and restart recovery is
here. Source of truth is a hash-chained append-only log on disk; this view
is the queryable index. Auditable, not just observable."

### 7. Stop bot → close (1 min)

- [ ] Click **Stop bot**
- [ ] Strategy card flips to **Stopped**
- [ ] Heartbeat goes to **Stale**

"That's the loop. You start, the system tells you it's healthy, you trust
it. Something looks off — kill switch in one click, audit trail in another."

## Things to be ready to answer

| Question | Answer |
|---|---|
| "Does it work with brokers other than Alpaca?" | "Alpaca only in MVP. Architecture is broker-agnostic; IBKR is on the roadmap." |
| "Can I run my own strategy code?" | "MVP is the deployment + safety layer for the included engine. Custom strategy plug-in is post-MVP." |
| "Where does my data live?" | "Single VPS you control. Postgres + filesystem on your box. We don't host." |
| "What's the kill-switch latency?" | "One tick interval — default 10s. Configurable down to 1s." |
| "Is the engine open source?" | [your call — set expectations here] |

## Don't demo (yet)

- AI signal generation (not built)
- Multi-tenant / team accounts (not built)
- Billing flow (not built — handle manually for first 5–10 betas)
- Live mode (only after a beta has run paper for a week and smoke test passes consistently)
