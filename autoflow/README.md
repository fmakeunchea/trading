# AutoFlow Trader

**Broker-safe automated strategy deployment platform.**
Thin SaaS veneer on top of the existing Python trading engine in the parent
repo (`/Users/fifi/Trading/`). The engine itself is unchanged — AutoFlow
adds dashboard, controls, audit, backups, monitoring, and HTTPS-fronted
deployment.

This README is the single canonical doc. Deeper runbooks for specific
tasks live in `docs/`.

---

## What this is — and what it deliberately is NOT

**Is:**
- Operations layer for one trader's automated strategy
- Single-tenant per VPS
- Paper-mode-first; live mode gated behind a passing smoke test + clean paper week
- Read-only over the engine's existing append-only audit log

**Is NOT:**
- A profitable bot ("we don't make money for you, we make sure your bot doesn't lose money silently")
- Multi-tenant SaaS (no users, orgs, billing yet)
- A strategy authoring tool (the strategy code lives in the parent engine)
- A broker switch (Alpaca only in MVP)
- An always-on managed service — you run it on your VPS

If a feature would push us toward any of the "is NOT" list, it's
out-of-scope for the MVP.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Browser (you / beta user)                                        │
│  https://autoflowtrader.com  — basic auth + TLS                   │
└────────────────────────────────┬─────────────────────────────────┘
                                 │
┌────────────────────────────────▼─────────────────────────────────┐
│  Traefik  (existing on the VPS, alongside n8n)                    │
│  - HTTPS via Let's Encrypt (resolver: mytlschallenge)             │
│  - Basic auth middleware (autoflow-auth@docker)                   │
│  - Routes: / → web,  /api → api (prefix stripped)                 │
└──────────────┬──────────────────────────┬────────────────────────┘
               │                          │
┌──────────────▼─────────┐  ┌─────────────▼────────────────────────┐
│  web  (Next.js 15)     │  │  api  (FastAPI)                       │
│  - dark-mode SaaS UI   │  │  - reads engine artifacts             │
│  - shadcn/ui           │  │  - writes kill-switch file            │
│  - SSR + RSC           │  │  - controls bot via docker socket     │
└────────────────────────┘  │  - mirrors trades.jsonl → Postgres    │
                            └────┬───────────────────────┬─────────┘
                                 │ SQL                   │ shared volume
                       ┌─────────▼──────┐      ┌─────────▼──────────┐
                       │  postgres 16    │      │  trading-bot       │
                       │  - strategies   │      │  (the existing     │
                       │  - incidents    │      │   run_strategy.py) │
                       │  - trades       │      │  paper mode        │
                       │  - bot_state    │      │  writes:           │
                       │  - smoke_runs   │      │   var/state.json   │
                       └─────────────────┘      │   var/trades.jsonl │
                                                │   var/heartbeat    │
                                                │   var/lock         │
                                                └────────────────────┘
                                                profile-gated;
                                                started/stopped via API
```

### Core integration insight

The engine's `var/` directory is the contract. AutoFlow:

| Concern               | Mechanism                                                       |
|-----------------------|-----------------------------------------------------------------|
| Strategy status       | Read `var/state.json` + check `var/trading_bot.lock` PID alive  |
| Heartbeat health      | `mtime` of `var/trading_bot.heartbeat` vs. now (≤30s = fresh)   |
| Reconcile status      | Last RECONCILE record in `var/trades.jsonl`                     |
| Kill switch           | `touch` / `rm` `var/trading_bot.kill` (engine checks each tick) |
| Open positions        | `state.json.open_trades`                                        |
| Incidents             | Tail `var/trades.jsonl`, `kind == "INCIDENT"`                   |
| Recent trades         | Tail `var/trades.jsonl`, `kind == "RESULT"`                     |
| Broker connection     | Engine state's last successful broker call timestamp            |
| Start / stop bot      | `docker start|stop autoflow-trading-bot-1`                      |
| Smoke test            | Spawn one-shot container running `scripts/paper_smoke.py`       |

**Postgres is for SaaS-side state only.** The engine's append-only
hash-chained `trades.jsonl` is the source of truth; the DB is a
queryable mirror + UI cache.

---

## Repository layout

```
autoflow/
├── README.md                           ← you are here
├── docker-compose.yml                  base stack (postgres, api, web, bot)
├── docker-compose.traefik.yml          overlay: routes through existing Traefik
├── .env.example                        env template
├── .gitignore
│
├── api/                                FastAPI backend
│   ├── Dockerfile
│   ├── trading-bot.Dockerfile          containerises the existing engine
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   ├── pytest.ini
│   ├── app/
│   │   ├── main.py                     FastAPI app + lifespan
│   │   ├── config.py                   pydantic-settings
│   │   ├── db.py                       SQLAlchemy session factory
│   │   ├── schemas.py                  request/response DTOs
│   │   ├── engine_io.py                reads var/ artifacts
│   │   ├── bot_control.py              docker CLI wrapper for start/stop
│   │   ├── incident_sync.py            background tail of trades.jsonl → DB
│   │   ├── yaml_writer.py              DB → engine YAML, atomic rename
│   │   └── routers/
│   │       ├── health.py               /health
│   │       ├── positions.py            /positions
│   │       ├── orders.py               /orders
│   │       ├── incidents.py            /incidents
│   │       ├── strategies.py           /strategies (+ /apply)
│   │       ├── smoke_test.py           /run-smoke-test
│   │       ├── kill_switch.py          /kill-switch
│   │       └── bot_control.py          /status, /start-bot, /stop-bot
│   └── tests/                          pytest suite (10 tests)
│       ├── conftest.py
│       ├── test_health.py
│       ├── test_status.py
│       ├── test_kill_switch.py
│       └── test_bot_control.py
│
├── web/                                Next.js 15 frontend
│   ├── Dockerfile                      builder bakes NEXT_PUBLIC_API_URL
│   ├── package.json
│   ├── tsconfig.json
│   ├── tailwind.config.ts
│   ├── components.json                 shadcn config
│   ├── app/
│   │   ├── globals.css
│   │   ├── layout.tsx                  root, dark mode forced
│   │   ├── page.tsx                    landing
│   │   └── (app)/
│   │       ├── layout.tsx              sidebar shell
│   │       ├── dashboard/page.tsx      live status + controls
│   │       ├── strategy/page.tsx       config editor
│   │       ├── strategy/editor.tsx     DB save + Apply YAML
│   │       ├── incidents/page.tsx      audit log viewer
│   │       └── smoke-test/page.tsx     smoke test runner
│   ├── components/
│   │   ├── ui/                         shadcn primitives (button, card, …)
│   │   ├── landing/                    hero, benefits, features, pricing, CTA
│   │   └── app/                        sidebar, status-card, kill-switch, bot-controls
│   ├── lib/
│   │   ├── api.ts                      typed API client
│   │   └── utils.ts
│   └── public/                         (kept tracked via .gitkeep)
│
├── db/
│   └── migrations/
│       ├── 001_initial.sql             strategies, incidents, trades, bot_state, smoke_test_runs
│       └── 002_apply_state.sql         applied_at, last_restart_at on strategies
│
├── scripts/
│   ├── backup.sh                       nightly pg_dump → /opt/backups/autoflow/
│   └── restore.sh                      interactive 'yes'-confirmed restore
│
└── docs/
    ├── deploy.md                       nginx + HTTPS deploy (alternative to Traefik)
    ├── deploy-traefik.md               PROD: deploy behind existing Traefik
    ├── demo.md                         beta-user demo walkthrough
    └── monday-paper-test.md            first market-open paper test runbook
```

---

## API endpoints

All endpoints live under `/api/` on production (Traefik strips the prefix).

| Endpoint                      | Method | Auth | Description                                         |
|-------------------------------|--------|------|-----------------------------------------------------|
| `/health`                     | GET    | yes  | DB + engine var/ accessibility check                |
| `/status`                     | GET    | yes  | Composite status snapshot (consumed by dashboard)   |
| `/positions`                  | GET    | yes  | Open positions from `state.json.open_trades`        |
| `/orders`                     | GET    | yes  | Last N RESULT records from `trades` table           |
| `/incidents`                  | GET    | yes  | Audit log INCIDENTs (filterable by kind)            |
| `/strategies`                 | GET    | yes  | All strategy presets                                |
| `/strategies/{id}`            | GET    | yes  | One strategy                                        |
| `/strategies/{id}`            | PUT    | yes  | Save (DB only — engine doesn't pick up yet)         |
| `/strategies/{id}/apply`      | POST   | yes  | Render DB → YAML; sets `applied_at`                 |
| `/run-smoke-test`             | POST   | yes  | Kick off background smoke test container            |
| `/run-smoke-test/{id}`        | GET    | yes  | Poll smoke test status                              |
| `/kill-switch`                | GET    | yes  | Read kill-switch file presence                      |
| `/kill-switch`                | POST   | yes  | Engage / release (writes `var/trading_bot.kill`)    |
| `/start-bot`                  | POST   | yes  | `docker start autoflow-trading-bot-1`               |
| `/stop-bot`                   | POST   | yes  | `docker stop  autoflow-trading-bot-1`               |

"Auth" = Traefik basic-auth in production (none in local dev).

---

## Configuration

All config is via the autoflow/.env file. None checked into git.

| Variable               | Local dev default      | Production value                  | Notes |
|------------------------|------------------------|-----------------------------------|-------|
| `POSTGRES_USER`        | `autoflow`             | `autoflow`                        |       |
| `POSTGRES_PASSWORD`    | (any)                  | `openssl rand -base64 32`         | mode 0600 |
| `POSTGRES_DB`          | `autoflow`             | `autoflow`                        |       |
| `ALPACA_API_KEY`       | paper key              | paper key (rotate for prod)       | from app.alpaca.markets/paper |
| `ALPACA_API_SECRET`    | paper secret           | paper secret                      |       |
| `PUBLIC_HOST`          | (unused)               | `autoflowtrader.com`              | Traefik routing host |
| `NEXT_PUBLIC_API_URL`  | `http://localhost:8000`| `/api`                            | **baked into client bundle at build time** |
| `CORS_ORIGINS`         | `http://localhost:3000`| `https://autoflowtrader.com`      |       |
| `AUTOFLOW_BASIC_AUTH`  | (empty)                | `user:$$2y$$05$$...`              | `htpasswd -nbB` output, `$` doubled |

`NEXT_PUBLIC_API_URL` ⚠️ — Next.js inlines `NEXT_PUBLIC_*` env vars into
the client bundle during `npm run build`. If you change this value, you
**must** rebuild the web image (`docker compose build --no-cache web`).
A runtime change does nothing.

`AUTOFLOW_BASIC_AUTH` ⚠️ — docker-compose env vars use `$` for variable
interpolation. The htpasswd hash literal must double every `$` to `$$`.
Use `htpasswd -nbB user 'pass' | sed 's/\$/\$\$/g'`. Multi-user format is
comma-separated.

---

## Local development

```bash
cd autoflow
cp .env.example .env
# Fill ALPACA_API_KEY / ALPACA_API_SECRET (paper keys)

docker compose up -d --build
# - postgres healthy in ~10s
# - api at  http://localhost:8000  (docs at /docs)
# - web at  http://localhost:3000

# Build the bot image and create the container in stopped state
docker compose --profile bot up --no-start --build trading-bot

# Verify
curl -s http://localhost:8000/health | python3 -m json.tool
open http://localhost:3000
```

Then on the dashboard click **Start bot**. Heartbeat goes green within
~15s.

### Run the test suite

The api container doesn't ship with tests — run from your host venv:

```bash
cd autoflow/api
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest tests/ -v
# Expect: 10 passing
```

---

## Production deployment

This VPS already runs Traefik (alongside n8n at
`n8n.srv1167844.hstgr.cloud`). AutoFlow joins the existing Traefik
network without modifying Traefik or n8n.

**Step-by-step deployment**: [docs/deploy-traefik.md](docs/deploy-traefik.md)

Summary:
1. DNS A-record for `autoflowtrader.com` → VPS IP (`72.61.72.192`)
2. Repo at `/opt/trading-bot` on VPS, branch `autoflow-mvp` (or `main` after merge)
3. `.env` filled per the table above; basic-auth hash with doubled `$`
4. ```bash
   docker compose -f docker-compose.yml -f docker-compose.traefik.yml up -d --build
   docker compose -f docker-compose.yml -f docker-compose.traefik.yml --profile bot up --no-start --build trading-bot
   ```
5. Verify: HTTPS works, basic auth fires 401, authed `/api/health` returns
   `{"status":"ok"}`, n8n still 200

### Rollback (n8n + Traefik untouched)

```bash
cd /opt/trading-bot/autoflow
docker compose -f docker-compose.yml -f docker-compose.traefik.yml --profile bot down
```

To bring it back without rebuilding:
```bash
docker compose -f docker-compose.yml -f docker-compose.traefik.yml up -d
```

---

## Backups

### Local nightly pg_dump
- Cron line: `0 3 * * * /opt/trading-bot/autoflow/scripts/backup.sh >> /var/log/autoflow-backup.log 2>&1`
- Output dir: `/opt/backups/autoflow/`, mode 0700
- Retention: 14 days (env: `RETENTION_DAYS`)
- Safety: refuses to keep dumps with fewer than 5 non-comment SQL lines (catches empty pg_dump masked by gzip header)

### Off-site to Backblaze B2
- Bucket: `autoflow-backups-fifi` (Private, default encryption on)
- Cron line: `30 3 * * * rclone sync /opt/backups/autoflow/ b2:autoflow-backups-fifi/ --transfers 2 --quiet >> /var/log/autoflow-backup.log 2>&1`
- App key scoped read+write to that bucket only

### Restore

```bash
ls -lh /opt/backups/autoflow/
/opt/trading-bot/autoflow/scripts/restore.sh /opt/backups/autoflow/autoflow-<timestamp>.sql.gz
# type 'yes' at the prompt
docker exec autoflow-postgres-1 psql -U autoflow -d autoflow \
  -c 'SELECT count(*) FROM strategies; SELECT count(*) FROM incidents;'
```

To restore from B2 if the VPS is dead:
```bash
apt install -y rclone           # or: brew install rclone
rclone config                   # add b2 remote with same keyID/applicationKey
rclone copy b2:autoflow-backups-fifi/<file> /tmp/
/opt/trading-bot/autoflow/scripts/restore.sh /tmp/<file>
```

The local restore was practiced once (1 strategy + 0 incidents back).
A B2-restore practice is recommended within the first week of beta.

---

## Monitoring

UptimeRobot Keyword monitor:
- URL: `https://autoflowtrader.com/api/health`
- Keyword: `"status":"ok"` — alerts on **absence**
- HTTP basic auth attached
- Interval: 5 minutes
- Email alerts to the operator

Test-notification email confirmed working. Real outage simulation
recommended once before relying on it.

---

## Operational levers

### Kill switch
Three ways, in priority order:

1. **Dashboard toggle** — preferred; engine completes mid-flight broker call before halting on next tick
2. **API**: `curl -u user:pass -X POST -d '{"engaged":true}' -H 'Content-Type: application/json' https://autoflowtrader.com/api/kill-switch`
3. **Filesystem**: `touch /opt/trading-bot/var/trading_bot.kill` — works even if API is down

Release: same paths, with `engaged: false` or `rm` the file.

### Start / stop bot
From dashboard, or:
```bash
curl -u user:pass -X POST https://autoflowtrader.com/api/start-bot
curl -u user:pass -X POST https://autoflowtrader.com/api/stop-bot
```

Refuses to start when kill switch is engaged (surfaces the invariant
rather than silently halting on first tick).

### Strategy edit flow

1. Dashboard `/strategy` → edit fields → **Save changes**. DB row updated.
2. Badge **Unsaved → YAML** appears.
3. Click **Apply to YAML**. `yaml_writer.py` patches the engine's
   `config.paper.yaml` (atomic write to `.next`, fsync, rename). Sets
   `strategies.applied_at`.
4. Badge changes to **Restart bot to take effect**.
5. Click **Stop bot** then **Start bot**. The start endpoint stamps
   `last_restart_at`, clearing the badge.

The engine never silently restarts — restart is always operator-driven
to keep mid-tick safety guarantees intact.

### Smoke test
Dashboard `/smoke-test` → **Run smoke test**. Spawns a one-shot
container that exercises OTO placement, cancel, flatten, partial-fill
against the paper broker. PASS / FAIL surfaced in UI; rows persist in
`smoke_test_runs` table.

**Don't run mid-session** — it shares the paper account with the live
engine and orders can collide.

### Logs

```bash
docker compose -f docker-compose.yml -f docker-compose.traefik.yml logs -f api
docker compose -f docker-compose.yml -f docker-compose.traefik.yml logs -f web
docker logs autoflow-trading-bot-1 -f
docker logs root-traefik-1 --since=10m | grep -i autoflow
```

All services use docker's `json-file` driver with 10MB × 5 file rotation.

---

## Postgres schema

```
strategies        editable presets — name, mode, symbols, daily_loss_cap_pct,
                  max_concurrent_positions, position_notional_pct, is_active,
                  applied_at, last_restart_at, updated_at
                  (one row UNIQUE WHERE is_active)

incidents         mirror of trades.jsonl INCIDENT records — kind, severity,
                  phase, symbols, reason, payload jsonb, occurred_at
                  (UNIQUE source_id from engine hash chain)

trades            mirror of trades.jsonl RESULT records — symbol, side, qty,
                  avg_fill_price, status, pnl, payload jsonb, occurred_at

bot_state         single-row snapshot — running, mode, kill_switch_engaged,
                  heartbeat_at, reconcile_ok, broker_connected, open_positions
                  (refreshed every 3s by incident_sync worker)

smoke_test_runs   history — status, started_at, finished_at, exit_code,
                  stdout, stderr
```

Migrations in `db/migrations/`. Postgres only auto-runs them on first
init; for migrations against an existing DB:

```bash
docker compose exec -T postgres psql -U autoflow -d autoflow < db/migrations/00X_name.sql
```

---

## What's NOT in scope yet

These are deliberate non-goals for the MVP. Each has a "when":

| Feature                      | When to consider                                          |
|------------------------------|-----------------------------------------------------------|
| User accounts / multi-tenant | After 5+ paying beta users on separate VPSes              |
| Billing                      | After 3 paid betas (manual invoicing first)               |
| Live mode safety gates       | After 5 clean paper-mode days; require recent passing smoke |
| Email/Slack alerts           | When manual ops via UptimeRobot become tedious            |
| Broker expansion (IBKR, …)   | After product-market fit on Alpaca                        |
| Custom strategy plug-in      | Far future — not the value prop                           |
| AI signal generation         | Out of scope; this is an ops platform, not alpha          |

---

## Pointers

- **Local boot + 7 validation checks**: covered above + during conversation; reproduce by following "Local development"
- **Production deployment runbook**: [docs/deploy-traefik.md](docs/deploy-traefik.md)
- **Beta-user demo walkthrough**: [docs/demo.md](docs/demo.md)
- **First market-open paper test**: [docs/monday-paper-test.md](docs/monday-paper-test.md)
- **Pre-beta security checklist**: [docs/deploy.md §H](docs/deploy.md)
- **Engine itself**: parent repo at `/opt/trading-bot/` (or `/Users/fifi/Trading/` locally)

---

## Current state (2026-04-26)

- ✅ MVP scaffold (60 files, 10 tests, all 7 validation checks passing)
- ✅ Week-2 hardening (DB → YAML, restart-required UX, deploy + demo docs)
- ✅ Production deploy via Traefik at https://autoflowtrader.com
- ✅ UptimeRobot keyword monitoring + alert channel verified
- ✅ Local nightly pg_dump cron (03:00 UTC)
- ✅ B2 off-site sync cron (03:30 UTC), restore practice done locally
- ⏳ First market-open engine-connected paper test — Monday next session
- ⏳ B2 restore practice — recommended within first beta week
