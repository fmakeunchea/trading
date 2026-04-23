# VPS deployment (systemd, paper mode)

This runbook takes a fresh Linux VPS to a persistent paper-trading bot
that survives SSH disconnects, laptop shutdowns, and VPS reboots.

## Assumptions

- Debian 12 or Ubuntu 22.04+ (commands use `apt`; adapt for other
  distros).
- SSH access as `root` or via `sudo`.
- The code is at `https://github.com/fmakeunchea/trading`.
- Paper trading **only** at this stage. Live rollout requires meeting
  the gates in [paper_gates.md](paper_gates.md).

## Why a dedicated service user (not root)

Run the bot as a dedicated `trading` user, **not** as root. The bot
has internet access, holds real credentials (even for paper), and
processes market data. If it is ever compromised, a non-root service
user limits the blast radius to `/opt/trading-bot/var` and the one
user's crontab — root would expose the entire VPS. Systemd's
`User=trading` line enforces this.

## One-time VPS setup

### 0. Pre-flight

```bash
# As root
timedatectl                       # Confirm "NTP synchronized: yes".
# If not:
timedatectl set-ntp true
timedatectl set-timezone UTC      # Keep the host in UTC. Bot is UTC-internal.
```

### 1. Install system dependencies

```bash
apt update
apt install -y python3 python3-venv python3-pip git ca-certificates
python3 --version                 # must be 3.11+
```

### 2. Create the service user

```bash
useradd --system --shell /usr/sbin/nologin --home-dir /opt/trading-bot trading
```

### 3. Clone the repo into /opt/trading-bot

```bash
install -d -o trading -g trading -m 0755 /opt/trading-bot
sudo -u trading git clone https://github.com/fmakeunchea/trading.git /opt/trading-bot
```

### 4. Python virtualenv + install

```bash
sudo -u trading python3 -m venv /opt/trading-bot/.venv
sudo -u trading /opt/trading-bot/.venv/bin/pip install --upgrade pip
sudo -u trading /opt/trading-bot/.venv/bin/pip install -r /opt/trading-bot/requirements.txt
sudo -u trading /opt/trading-bot/.venv/bin/pip install -e /opt/trading-bot
```

### 5. Create the runtime var directory

```bash
install -d -o trading -g trading -m 0750 /opt/trading-bot/var
```

### 6. Install paper credentials (root-owned, group-readable)

```bash
install -d -o root -g trading -m 0750 /etc/trading-bot
cat > /etc/trading-bot/env <<'EOF'
ALPACA_API_KEY=PK...YOUR_PAPER_KEY
ALPACA_API_SECRET=YOUR_PAPER_SECRET
EOF
chown root:trading /etc/trading-bot/env
chmod 0640 /etc/trading-bot/env
```

Sanity check: the `trading` user must be able to read the file.

```bash
sudo -u trading cat /etc/trading-bot/env >/dev/null && echo "readable"
```

### 7. Smoke-test dry run (no orders placed)

```bash
sudo -u trading bash -c \
  'set -a; source /etc/trading-bot/env; set +a; \
   cd /opt/trading-bot && .venv/bin/python -m scripts.paper_smoke --dry-run'
```

Expected: one `[PASS] connectivity` line. If you see
"ALPACA_API_KEY / ALPACA_API_SECRET not present", the env file is
wrong.

### 8. Install the systemd unit

```bash
install -o root -g root -m 0644 \
  /opt/trading-bot/deploy/trading-bot-paper.service \
  /etc/systemd/system/trading-bot-paper.service
systemctl daemon-reload
systemctl enable trading-bot-paper
```

`enable` is the line that makes the bot come back after a VPS reboot.

### 9. Start and verify

```bash
systemctl start trading-bot-paper
systemctl status trading-bot-paper --no-pager
journalctl -u trading-bot-paper -n 50 --no-pager
```

You should see the bot acquire the lock, run `recover()`, and tick
every `tick_interval_s` (10s by default).

## Service management (day-to-day)

```bash
# Lifecycle
systemctl start trading-bot-paper
systemctl stop trading-bot-paper
systemctl restart trading-bot-paper
systemctl status trading-bot-paper

# Persistence across reboot
systemctl enable trading-bot-paper
systemctl disable trading-bot-paper

# Logs
journalctl -u trading-bot-paper -f                        # follow live
journalctl -u trading-bot-paper --since "1 hour ago"      # recent
journalctl -u trading-bot-paper --since today -p warning  # today, warnings+

# Heartbeat (updated once per tick)
stat -c '%y' /opt/trading-bot/var/trading_bot.heartbeat

# Heartbeat freshness check (exit 0 if updated in last 60s)
test $(($(date +%s) - $(date -r /opt/trading-bot/var/trading_bot.heartbeat +%s))) -lt 60 \
    && echo "fresh" || echo "STALE"

# Daily summary for today
sudo -u trading /opt/trading-bot/.venv/bin/python \
    -m scripts.daily_summary \
    --trade-log /opt/trading-bot/var/trades.jsonl \
    --date "$(date -u +%Y-%m-%d)"
```

## Kill switch (remote)

Engage from any SSH session:

```bash
sudo -u trading touch /opt/trading-bot/var/trading_bot.kill
```

The bot stops submitting new entries on the next tick. **Existing open
trades are still managed** (stops, targets, session-end flatten) — this
is intentional, tested in `test_kill_switch_does_not_block_exits`.

Release:

```bash
sudo -u trading rm /opt/trading-bot/var/trading_bot.kill
```

## Updating the bot

```bash
systemctl stop trading-bot-paper
sudo -u trading bash -c 'cd /opt/trading-bot && git pull --ff-only'
sudo -u trading /opt/trading-bot/.venv/bin/pip install -r /opt/trading-bot/requirements.txt
sudo -u trading /opt/trading-bot/.venv/bin/pip install -e /opt/trading-bot
# If deploy/trading-bot-paper.service changed:
install -o root -g root -m 0644 \
  /opt/trading-bot/deploy/trading-bot-paper.service \
  /etc/systemd/system/trading-bot-paper.service
systemctl daemon-reload
systemctl start trading-bot-paper
```

Never use `git pull` into a running bot's working tree — the stop
above is mandatory. State and trade log in `var/` are preserved across
the restart.

## Backups

The two files you cannot recreate:

- `/opt/trading-bot/var/trades.jsonl`  (hash-chained audit log)
- `/opt/trading-bot/var/state.json`    (current trading state)

A nightly cron backup to a separate host is the minimum acceptable
practice. Example (runs 00:15 UTC every night):

```cron
15 0 * * * root tar -czf /root/backups/trading-bot-$(date -u +\%Y\%m\%d).tgz /opt/trading-bot/var/
```

## Rolling back the deployment

```bash
systemctl stop trading-bot-paper
systemctl disable trading-bot-paper
rm /etc/systemd/system/trading-bot-paper.service
systemctl daemon-reload
# /opt/trading-bot is preserved; state/trade log are preserved.
# To remove everything:
# rm -rf /opt/trading-bot /etc/trading-bot
# userdel trading
```

## Log rotation (follow-up, not required on day 1)

`trades.jsonl` grows with each entry. On a busy day this is a few
hundred KB; it will not stress the VPS for months. The orchestrator
does **not** auto-rotate today — a Phase 3.5 follow-up would be to
call `TradeLog.append_seal()` at session rollover and rotate to
`trades-YYYY-MM-DD.jsonl`. Until then, manual annual rotation is
sufficient.
