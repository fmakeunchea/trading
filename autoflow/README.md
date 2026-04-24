# AutoFlow Trader — MVP

Broker-safe automated strategy deployment platform. Thin SaaS veneer on top of the existing Python trading engine at `../` (see repo root).

## What this MVP does

- Dashboard for strategy status, heartbeat, reconcile state, kill switch, open positions, incidents, recent trades, broker connection
- Strategy configuration (symbols, risk, paper/live, daily loss cap, max positions)
- Incident log (reconcile events, halts, orphan-order alerts, restart recoveries)
- Smoke test runner (invokes `scripts/paper_smoke.py`)
- Kill switch + start/stop bot controls

## What this MVP deliberately does NOT do

- No auth (deploy behind VPN / reverse-proxy basic auth). Add week 2.
- No billing, no multi-tenant. Single-tenant per VPS.
- No email / notifications / webhooks.
- No websockets — 5s polling is fine for MVP.
- No rewrite of the trading engine. FastAPI reads the engine's existing `./var/` artifacts.

## Architecture

```
web (Next.js)  →  api (FastAPI)  →  trading-bot (existing run_strategy.py)
                      ↓                   ↓
                  postgres           shared volume: ./var/
                                     (state.json, trades.jsonl,
                                      heartbeat, kill-switch)
```

**Integration surface (zero engine changes):**

| MVP concern          | How it works                                                    |
|----------------------|-----------------------------------------------------------------|
| Strategy status      | Read `var/state.json` + check `var/trading_bot.lock` PID alive  |
| Heartbeat            | `mtime` of `var/trading_bot.heartbeat` vs. now                  |
| Reconcile status     | Last RECONCILE INCIDENT record in `var/trades.jsonl`            |
| Kill switch          | `touch`/`rm` `var/trading_bot.kill`                             |
| Open positions       | `state.json.open_trades`                                        |
| Incidents            | Tail `var/trades.jsonl`, filter `kind == "INCIDENT"`            |
| Recent trades        | Tail `var/trades.jsonl`, filter `kind == "RESULT"`              |
| Broker connection    | Last successful broker call timestamp from engine state         |
| Start/stop bot       | `docker compose start|stop trading-bot`                         |
| Smoke test           | `docker compose run --rm trading-bot python scripts/paper_smoke.py` |

**Postgres is for SaaS state only:**
- `strategies` — editable config presets (the engine still reads YAML; we render YAML from DB on apply)
- `incidents` — mirrored from trades.jsonl for fast querying/filtering
- `trades` — mirrored RESULT records
- `bot_state` — last-known status snapshot (one row, upserted)
- `smoke_test_runs` — history of smoke test invocations + output

## Local dev

```bash
cp .env.example .env
# Fill in ALPACA_API_KEY / ALPACA_API_SECRET (paper keys)
docker compose up --build
# web → http://localhost:3000
# api → http://localhost:8000/docs
```

## VPS deployment

One `docker compose up -d` on a single VPS. The `trading-bot` service mounts the existing repo's `./var/` so it keeps writing to the same files systemd would use. Put nginx in front with basic auth + TLS. That's the deploy.

See `docs/deploy.md` (TODO week 2) for the nginx config.

## Folder layout

```
autoflow/
├── README.md                 this file
├── docker-compose.yml
├── .env.example
├── api/                      FastAPI service
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py
│       ├── config.py
│       ├── db.py
│       ├── schemas.py
│       ├── engine_io.py      reads var/ artifacts
│       ├── bot_control.py    docker-compose wrapper
│       ├── incident_sync.py  background tailer for trades.jsonl
│       └── routers/          one file per endpoint group
├── db/
│   └── migrations/001_initial.sql
└── web/                      Next.js (App Router)
    ├── app/
    │   ├── page.tsx          landing
    │   └── (app)/            authenticated shell (later)
    │       ├── dashboard/
    │       ├── strategy/
    │       ├── incidents/
    │       └── smoke-test/
    ├── components/ui/        shadcn primitives
    ├── components/landing/   hero/features/pricing/cta
    ├── components/app/       dashboard widgets
    └── lib/                  api client, utils
```
