#!/usr/bin/env bash
# Restore an AutoFlow Postgres backup.
#
# Usage:  /opt/trading-bot/autoflow/scripts/restore.sh /opt/backups/autoflow/autoflow-20260424T030000Z.sql.gz
#
# Drops the existing autoflow DB and recreates from the dump. THIS DESTROYS
# the current DB — confirm twice before running in prod.
set -euo pipefail

CONTAINER="${CONTAINER:-autoflow-postgres-1}"
DB_USER="${DB_USER:-autoflow}"
DB_NAME="${DB_NAME:-autoflow}"

if [ "$#" -ne 1 ]; then
    echo "usage: $0 <path-to-backup.sql.gz>" >&2
    exit 64
fi

src="$1"
[ -f "$src" ] || { echo "no such file: $src" >&2; exit 66; }

read -r -p "Restore $src into $DB_NAME (DROPS EXISTING DATA)? type 'yes' to proceed: " confirm
[ "$confirm" = "yes" ] || { echo "aborted"; exit 1; }

# Stop the API + bot so nothing writes mid-restore
docker compose stop api trading-bot 2>/dev/null || true

gunzip -c "$src" | docker exec -i "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1

# Bring the API back up
docker compose start api

echo "restore complete from $src"
