#!/usr/bin/env bash
# Nightly and pre-deploy database backup: gzip'd pg_dump in ./backups, 30 days kept.
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p backups
file="backups/rideleader-$(date -u +%Y%m%d-%H%M%S).sql.gz"
# Talk to the database container directly: going through `docker compose` needs the image settings the deploy
# exports, which cron (and a plain shell) don't have.
DB_CONTAINER="${DB_CONTAINER:-rideleader-db-1}"
set -o pipefail
docker exec "$DB_CONTAINER" pg_dump -U rlk -d rlk --no-owner | gzip > "$file" || { echo "pg_dump failed" >&2; rm -f "$file"; exit 1; }
# a real dump is more than a gzip header, and mentions our tables
[[ "$(zcat "$file" | grep -c 'CREATE TABLE public.route ')" -ge 1 ]] || { echo "backup does not look like a dump" >&2; rm -f "$file"; exit 1; }
find backups -name 'rideleader-*.sql.gz' -mtime +30 -delete
echo "backup written: $file ($(du -h "$file" | cut -f1))"
