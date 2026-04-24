#!/usr/bin/env bash
# Nightly Postgres backup for the AutoFlow stack.
#
# - Dumps the autoflow DB via the running postgres container (no host psql needed).
# - Writes timestamped .sql.gz files to BACKUP_DIR.
# - Prunes anything older than RETENTION_DAYS.
# - Exits non-zero on failure so cron can mail/log the error.
#
# Usage:  /opt/trading-bot/autoflow/scripts/backup.sh
# Cron:   see docs/deploy.md
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/opt/backups/autoflow}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
CONTAINER="${CONTAINER:-autoflow-postgres-1}"
DB_USER="${DB_USER:-autoflow}"
DB_NAME="${DB_NAME:-autoflow}"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$BACKUP_DIR/autoflow-$stamp.sql.gz"

# pg_dump runs inside the container; gzip on the host so the file lands
# directly compressed without needing extra space in the container.
docker exec -i "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" --no-owner --clean --if-exists \
    | gzip -9 > "$out"

# Sanity: refuse to keep a 0-byte dump
if [ ! -s "$out" ]; then
    echo "backup empty: $out" >&2
    rm -f "$out"
    exit 2
fi

chmod 600 "$out"

# Prune old backups
find "$BACKUP_DIR" -maxdepth 1 -name 'autoflow-*.sql.gz' -mtime +"$RETENTION_DAYS" -delete

echo "backup ok: $out ($(stat -c %s "$out" 2>/dev/null || stat -f %z "$out") bytes)"
