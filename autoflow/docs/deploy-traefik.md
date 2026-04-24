# VPS deployment — behind existing Traefik

For VPS where Traefik is already running (e.g. alongside n8n). Use this
guide instead of `deploy.md` (which assumes nginx).

Assumptions, validated from your VPS:
- Traefik compose project name: `root` (config at `/root/docker-compose.yml`)
- Traefik container: `root-traefik-1`
- Traefik docker network: `root_default`
- Cert resolver: `mytlschallenge` (Let's Encrypt TLS challenge)
- Entrypoints: `web` (:80, redirects to websecure), `websecure` (:443)
- Existing n8n route: different host — no collision

The autoflow overlay file `docker-compose.traefik.yml` adds Traefik
labels and joins `root_default` as external. **n8n and the Traefik
container are never modified.**

---

## 1. DNS

Point `autoflowtrader.com` (and `www.autoflowtrader.com` if you want it)
A-records at the VPS public IP.

```bash
dig +short autoflowtrader.com   # should return the VPS IPv4
```

---

## 2. Clone repo + .env

```bash
mkdir -p /opt/trading-bot
git clone <your-trading-repo-url> /opt/trading-bot
cd /opt/trading-bot/autoflow
cp .env.example .env
```

Fill `.env` with this superset (keep the existing Postgres + Alpaca keys):

```
POSTGRES_USER=autoflow
POSTGRES_PASSWORD=<openssl rand -base64 32>
POSTGRES_DB=autoflow

ALPACA_API_KEY=<paper key>
ALPACA_API_SECRET=<paper secret>

# Traefik / public exposure
PUBLIC_HOST=autoflowtrader.com
NEXT_PUBLIC_API_URL=/api
CORS_ORIGINS=https://autoflowtrader.com

# Basic auth — generated in step 3
AUTOFLOW_BASIC_AUTH=
```

`chmod 600 .env`.

---

## 3. Generate basic-auth credentials

`htpasswd -nb` outputs `user:$apr1$...` which Traefik wants — but in a
docker-compose env var, every literal `$` must be doubled to `$$`.

```bash
# Pick a username + strong password
htpasswd -nbB youroperator 'your-strong-password' | sed -e 's/\$/\$\$/g'
```

Copy the output (one line) and paste as the `AUTOFLOW_BASIC_AUTH=` value
in `.env`. Example shape:

```
AUTOFLOW_BASIC_AUTH=youroperator:$$2y$$05$$abcdef...
```

To add a second user later, generate again with a different name and
join with a comma in the env var:

```
AUTOFLOW_BASIC_AUTH=op:$$2y$$05$$xxx,beta1:$$2y$$05$$yyy
```

---

## 4. Deploy

The `docker-compose.traefik.yml` overlay drops the host port bindings on
web/api and adds Traefik labels. Postgres and trading-bot stay internal.

```bash
cd /opt/trading-bot/autoflow

# Build + bring up web/api/postgres (no bot — profile-gated)
docker compose -f docker-compose.yml -f docker-compose.traefik.yml up -d --build

# Create the bot container in stopped state for the API to start later
docker compose -f docker-compose.yml -f docker-compose.traefik.yml \
    --profile bot up --no-start --build trading-bot
```

Traefik picks up the new labels automatically (it watches the docker
socket). Cert issuance happens on the first HTTPS hit (~10s).

---

## 5. Verify

```bash
# 1. HTTPS works + auth prompt fires
curl -I https://autoflowtrader.com/
# expect: HTTP/2 401, www-authenticate: Basic realm=...

# 2. With auth, dashboard loads
curl -I -u youroperator:your-strong-password https://autoflowtrader.com/
# expect: HTTP/2 200

# 3. /api/health through the proxy
curl -s -u youroperator:your-strong-password https://autoflowtrader.com/api/health \
    | python3 -m json.tool
# expect: {"status":"ok",...}

# 4. HTTP redirects to HTTPS
curl -I http://autoflowtrader.com/
# expect: HTTP/1.1 308 (or 301), location: https://...

# 5. n8n still works (sanity)
curl -I https://${SUBDOMAIN}.${DOMAIN_NAME}/   # whatever your n8n host is
# expect: 200/302/whatever it returned before
```

In a browser:
- visit `https://autoflowtrader.com/dashboard`
- basic-auth prompt → enter creds → dashboard renders
- Start bot → heartbeat green within 15s

---

## 6. Rollback

This stops AutoFlow only. Traefik and n8n keep running.

```bash
cd /opt/trading-bot/autoflow

# Stop the bot first (if running), then everything else
docker compose -f docker-compose.yml -f docker-compose.traefik.yml \
    --profile bot down

# Optional: also remove built images
docker compose -f docker-compose.yml -f docker-compose.traefik.yml down --rmi local
```

To bring it back without rebuilding:
```bash
docker compose -f docker-compose.yml -f docker-compose.traefik.yml up -d
```

---

## 7. Logs / troubleshooting

```bash
# Traefik routing decisions for autoflow
docker logs root-traefik-1 2>&1 | grep -i autoflow | tail -20

# AutoFlow services
cd /opt/trading-bot/autoflow
docker compose -f docker-compose.yml -f docker-compose.traefik.yml logs -f api
docker compose -f docker-compose.yml -f docker-compose.traefik.yml logs -f web

# Confirm autoflow joined the right network
docker inspect autoflow-web-1 --format '{{json .NetworkSettings.Networks}}' \
    | python3 -m json.tool | grep -E '"(root_default|autoflow)' 
```

Common issues:

| Symptom | Cause | Fix |
|---|---|---|
| 404 on `https://autoflowtrader.com/` | Traefik can't reach autoflow-web | confirm `root_default` is in the network list of autoflow-web; restart traefik isn't required, label changes are picked up live |
| 502 Bad Gateway on /api/ | api container down or wrong port | `docker compose ps`; check api logs |
| Cert pending / SSL error | Let's Encrypt rate-limited or DNS not propagated | `docker logs root-traefik-1 \| grep -i acme`; wait for DNS, then `docker restart root-traefik-1` |
| Auth prompt loops forever | `AUTOFLOW_BASIC_AUTH` wasn't expanded properly (single `$` in env) | regenerate with `sed 's/\$/\$\$/g'` and redeploy |

---

## 8. What this does NOT change

- `/root/docker-compose.yml` — never edited
- Traefik container — never restarted (label changes are picked up via the docker provider)
- n8n routing, n8n volumes, n8n env
- Existing TLS certs / `/letsencrypt/acme.json`
