#!/usr/bin/env bash
# Nightly and pre-deploy database backup: gzip'd pg_dump in ./backups, 30 days kept.
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p backups
file="backups/rideleader-$(date -u +%Y%m%d-%H%M%S).sql.gz"
docker compose -f docker-compose.prod.yml exec -T db pg_dump -U rlk -d rlk --no-owner | gzip > "$file"
test -s "$file" || { echo "backup is empty" >&2; rm -f "$file"; exit 1; }
find backups -name 'rideleader-*.sql.gz' -mtime +30 -delete
echo "backup written: $file ($(du -h "$file" | cut -f1))"
