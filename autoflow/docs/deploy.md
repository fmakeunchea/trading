# VPS deployment — nginx basic auth + HTTPS + backups

Single-tenant beta deployment. Ubuntu 22.04 / 24.04 (works on Hostinger
KVM, DigitalOcean droplets, Hetzner, etc).

Assumed paths: parent Trading repo at `/opt/trading-bot/`,
this MVP at `/opt/trading-bot/autoflow/`. Replace `autoflow.example.com`
with your domain everywhere below.

---

## A. nginx + HTTPS setup

### A.1  Install everything

```bash
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx \
                    apache2-utils ufw fail2ban
```

`docker.io` + `docker-compose-plugin` should already be installed; if not:
```bash
sudo apt install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER   # log out + back in once
```

### A.2  Repo on the box

```bash
sudo mkdir -p /opt/trading-bot
sudo chown $USER:$USER /opt/trading-bot
git clone <your-trading-repo> /opt/trading-bot
cd /opt/trading-bot/autoflow
cp .env.example .env
```

Fill `.env`:

```bash
$EDITOR .env
# - Set strong POSTGRES_PASSWORD (e.g. `openssl rand -base64 32`)
# - Set ALPACA_API_KEY / ALPACA_API_SECRET (paper)
# - Add: NEXT_PUBLIC_API_URL=/api
# - Add: CORS_ORIGINS=https://autoflow.example.com
```

### A.3  Boot the stack

```bash
docker compose up -d --build
docker compose --profile bot up --no-start --build trading-bot
curl -s http://127.0.0.1:8000/health   # expect status:ok
curl -s -I http://127.0.0.1:3000        # expect HTTP/1.1 200
```

All three ports are bound to `127.0.0.1` only (verified in
`docker-compose.yml`), so they're invisible from the public internet —
nginx will be the only thing facing outside.

---

## B. Full nginx config

Save as `/etc/nginx/sites-available/autoflow`. Certbot will rewrite the
`listen 80` block to redirect to 443 in step D.

```nginx
# Hardening defaults — apply once in /etc/nginx/conf.d/security.conf
# (or inline here). server_tokens off keeps the nginx version out of
# error pages and headers.

server {
    listen 80;
    listen [::]:80;
    server_name autoflow.example.com;

    # Allow Let's Encrypt HTTP-01 renewals through without auth
    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    # Everything else: certbot will replace this with a 301→https
    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    # Listening on 443 — certbot fills in ssl_certificate paths in step D.
    # Until certbot runs, this block is harmless because nginx will only
    # listen on 80. Don't enable this block manually before certbot.
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name autoflow.example.com;

    # ── TLS (managed by certbot after step D) ──
    # ssl_certificate     /etc/letsencrypt/live/autoflow.example.com/fullchain.pem;
    # ssl_certificate_key /etc/letsencrypt/live/autoflow.example.com/privkey.pem;
    # include /etc/letsencrypt/options-ssl-nginx.conf;
    # ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    # ── Security headers ──
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Frame-Options DENY always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;
    server_tokens off;
    client_max_body_size 4m;

    # ── HTTP basic auth on everything ──
    auth_basic "AutoFlow";
    auth_basic_user_file /etc/nginx/.autoflow.htpasswd;

    # FastAPI under /api/  →  api container on 127.0.0.1:8000
    location /api/ {
        proxy_pass         http://127.0.0.1:8000/;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }

    # Next.js everything else  →  web container on 127.0.0.1:3000
    location / {
        proxy_pass         http://127.0.0.1:3000;
        proxy_http_version 1.1;
        # Next.js dev/prod uses websockets for HMR + RSC streaming
        proxy_set_header   Upgrade           $http_upgrade;
        proxy_set_header   Connection        "upgrade";
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
```

Enable + test:

```bash
sudo ln -sf /etc/nginx/sites-available/autoflow /etc/nginx/sites-enabled/autoflow
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

---

## C. Basic auth

```bash
sudo htpasswd -c /etc/nginx/.autoflow.htpasswd youroperator
# follow prompts for password

sudo chmod 640 /etc/nginx/.autoflow.htpasswd
sudo chown root:www-data /etc/nginx/.autoflow.htpasswd
```

Add more users later (omit `-c` so the file isn't recreated):

```bash
sudo htpasswd /etc/nginx/.autoflow.htpasswd betauser1
```

Rotate a password:
```bash
sudo htpasswd /etc/nginx/.autoflow.htpasswd youroperator
```

Remove a user:
```bash
sudo htpasswd -D /etc/nginx/.autoflow.htpasswd alumnus
```

---

## D. Firewall + TLS

### D.1 UFW — only 22/80/443 public

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp     comment 'ssh'
sudo ufw allow 80/tcp     comment 'http (redirects to 443)'
sudo ufw allow 443/tcp    comment 'https'
sudo ufw enable
sudo ufw status verbose
```

`api:8000`, `web:3000`, `postgres:5432` are bound to `127.0.0.1` in the
compose file — they're loopback-only and UFW makes that explicit at the
kernel level too.

### D.2 TLS via Let's Encrypt

DNS for `autoflow.example.com` must point at the VPS first. Verify:
```bash
dig +short autoflow.example.com    # should return your VPS IPv4
```

Then:
```bash
sudo certbot --nginx -d autoflow.example.com
```

Certbot will:
- prove ownership over HTTP-01
- write the cert to `/etc/letsencrypt/live/autoflow.example.com/`
- uncomment + populate the `ssl_certificate*` lines in your nginx config
- rewrite the port-80 server block to 301→443
- install a systemd timer (`certbot.timer`) for auto-renewal

Verify renewal:
```bash
sudo certbot renew --dry-run
sudo systemctl list-timers | grep certbot
```

### D.3 fail2ban for SSH (recommended)

Default Ubuntu install has fail2ban with the `sshd` jail enabled out of the
box once installed. Confirm:
```bash
sudo systemctl enable --now fail2ban
sudo fail2ban-client status sshd
```

---

## Verify

```bash
# 1. HTTPS works + cert is valid
curl -I https://autoflow.example.com/        # 401 (auth required) — correct
curl -u youroperator:yourpass https://autoflow.example.com/api/health
# expect: {"status":"ok",...}

# 2. HTTP redirects to HTTPS
curl -I http://autoflow.example.com/         # 301 to https://...

# 3. Browser: https://autoflow.example.com/dashboard
#    - basic-auth prompt
#    - dashboard renders
#    - clicking Start bot → heartbeat green
```

---

## E. pg_dump backup script

The script is at [`autoflow/scripts/backup.sh`](../scripts/backup.sh) and
already exists in the repo. It:

- runs `pg_dump` inside the postgres container (no host psql needed)
- writes to `/opt/backups/autoflow/autoflow-<UTC-timestamp>.sql.gz`
- gzip-9 compressed
- 14-day retention (override with `RETENTION_DAYS=N`)
- exits non-zero on failure
- refuses to keep 0-byte dumps

Set up the backup directory on the VPS (one-time):

```bash
sudo mkdir -p /opt/backups/autoflow
sudo chown $USER:$USER /opt/backups/autoflow
sudo chmod 700 /opt/backups/autoflow
```

Run a manual backup to verify:

```bash
/opt/trading-bot/autoflow/scripts/backup.sh
ls -lh /opt/backups/autoflow/
```

**Expect:** one `autoflow-YYYYMMDDTHHMMSSZ.sql.gz` file, mode `-rw-------`,
non-zero size, command exit code 0.

---

## F. Cron entry

Edit your user's crontab:

```bash
crontab -e
```

Add (3:00 AM UTC nightly; uses absolute paths because cron has no PATH):

```cron
SHELL=/bin/bash
MAILTO=""
0 3 * * * /opt/trading-bot/autoflow/scripts/backup.sh >> /var/log/autoflow-backup.log 2>&1
```

Then verify:

```bash
sudo touch /var/log/autoflow-backup.log
sudo chown $USER:$USER /var/log/autoflow-backup.log
crontab -l
```

For the cron user to invoke `docker exec`, it must be in the `docker`
group (already done in step A.1).

---

## G. Restore procedure

The script is at [`autoflow/scripts/restore.sh`](../scripts/restore.sh).
It stops the api + bot, drops + recreates the autoflow DB from a backup,
then restarts the api. Two-step confirmation prompt protects against
accidents.

```bash
# Pick a backup
ls -lh /opt/backups/autoflow/

# Restore (interactive 'yes' confirmation required)
/opt/trading-bot/autoflow/scripts/restore.sh \
    /opt/backups/autoflow/autoflow-20260424T030000Z.sql.gz
```

**Verify the restore worked:**
```bash
docker compose exec postgres psql -U autoflow -d autoflow \
    -c 'SELECT count(*) FROM strategies; SELECT count(*) FROM incidents;'
```

**Test the backup → restore round-trip in staging before you trust it.**
A backup you've never restored is theoretical. Suggested cadence: do a
practice restore once after setup, then quarterly.

### Off-site copies (recommended)

Backups on the same VPS as the DB protect against software corruption,
not against the VPS being lost. For beta scale, the simplest off-site
is a second cron line that rsyncs to your laptop or to S3:

```cron
30 3 * * * rsync -az /opt/backups/autoflow/ user@your-laptop:autoflow-backups/ >> /var/log/autoflow-backup.log 2>&1
```

(Set up SSH key auth from the VPS to the destination first.)

---

## H. Pre-beta security checklist

Run this list before you send a beta user the URL. Tick each box.

### Access
- [ ] SSH key-only auth (disable password login: `PasswordAuthentication no` in `/etc/ssh/sshd_config`, then `sudo systemctl reload ssh`)
- [ ] Root SSH disabled (`PermitRootLogin no`)
- [ ] fail2ban running, sshd jail active
- [ ] UFW enabled, only 22/80/443 inbound
- [ ] All docker ports bound to `127.0.0.1` (re-verify with `ss -tlnp | grep -E '3000|8000|5432'`)

### TLS + auth
- [ ] HTTPS works; cert valid; HSTS header present (`curl -I https://… | grep -i strict`)
- [ ] HTTP → HTTPS redirect works (`curl -I http://…` returns 301)
- [ ] Basic auth prompt appears in a fresh browser
- [ ] htpasswd file is `0640 root:www-data`
- [ ] Auto-renewal verified: `sudo certbot renew --dry-run`

### Secrets
- [ ] `.env` file is `0600` and owned by your deploy user (`chmod 600 .env`)
- [ ] `POSTGRES_PASSWORD` is not the default and not weak
- [ ] Alpaca keys are **paper** for the beta period (no live keys yet)
- [ ] `.env` is in `.gitignore` (already is) — verify nothing was committed: `git log --all -- autoflow/.env` returns empty
- [ ] No keys hard-coded in repo: `git grep -E 'ALPACA_API_(KEY|SECRET)\s*=\s*[A-Z0-9]{8}' || echo clean`
- [ ] htpasswd password is strong + stored in your password manager (not just on the VPS)

### Reliability
- [ ] All compose services have `restart: unless-stopped` (✓ in repo)
- [ ] Log rotation: docker json-file `max-size=10m max-file=5` (✓ in repo)
- [ ] Nightly backup cron installed and one manual run succeeded
- [ ] Practice restore done in staging or against a throwaway DB
- [ ] Off-site backup copy configured (or accepted risk in writing)

### Operations
- [ ] Smoke test PASSes end-to-end (don't hand a beta user a stack the smoke test fails on)
- [ ] Kill switch round-trip tested via the production URL
- [ ] You know how to `docker compose logs -f api` over SSH
- [ ] `docker compose --profile bot down` is the documented "shut everything off" command
- [ ] You have an out-of-band channel to reach the beta user (Slack, email) if you need to take the system down

### Beta-user-specific
- [ ] Beta user has their own htpasswd entry (not sharing your operator account)
- [ ] You've walked them through `docs/demo.md` once before giving them the URL
- [ ] Documented to them: paper-only, single-tenant, no SLA, expect downtime during fixes

### Things you DON'T need before beta
- billing
- multi-tenant
- email/Slack alerts (manual ops is fine for 1–5 betas)
- broker expansion
- automated tests beyond what's already in `api/tests/`
