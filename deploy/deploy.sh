#!/usr/bin/env bash
# Deploy images on the server. Run by GitHub Actions as the 'deploy' user, from the app directory (/opt/rideleader):
#
#   bash deploy/deploy.sh <app-image> <hours-image>
#
# Steps: back up the database -> pull images -> start the stack -> wait for /healthz -> install the Caddy site and
# reload the shared Caddy. If anything fails the previous images are restored. Database changes are additive (the app
# only ever adds columns), so they are not reverted on rollback.
#
# SKIP_PULL=1 uses images already on the machine (for the very first deploy, built on the server).
set -euo pipefail

APP_IMAGE_NEW="${1:?usage: deploy.sh <app-image> <hours-image>}"
HOURS_IMAGE_NEW="${2:?usage: deploy.sh <app-image> <hours-image>}"
APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$APP_DIR"
[[ -f .env ]] || { echo "Missing $APP_DIR/.env" >&2; exit 1; }

COMPOSE=(docker compose -f docker-compose.prod.yml)
STATE_FILE=".deployed-images"
PREVIOUS="$(cat "$STATE_FILE" 2>/dev/null || true)"
EDGE_SITES_DIR="${EDGE_SITES_DIR:-/opt/edge/sites}"
EDGE_CADDY_CONTAINER="${EDGE_CADDY_CONTAINER:-sitemapchecker-caddy-1}"
env_value() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | tr -d "\"'\r"; }
log() { printf '\n==> %s\n' "$*"; }

start() {
  export APP_IMAGE="$1" HOURS_IMAGE="$2" APP_COMMIT="${3:-}"
  if [[ -z "${SKIP_PULL:-}" ]]; then
    log "Pulling images"
    "${COMPOSE[@]}" pull --quiet || return 1
  fi
  log "Starting the stack"
  "${COMPOSE[@]}" up -d --no-build --remove-orphans || return 1
}

healthy() {
  log "Waiting for the web app to report healthy"
  for _ in $(seq 1 40); do
    if "${COMPOSE[@]}" exec -T web python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8431/healthz', timeout=3)" >/dev/null 2>&1; then
      echo "Healthy."; return 0
    fi
    sleep 3
  done
  echo "Web app did not become healthy in time." >&2
  "${COMPOSE[@]}" logs --tail=60 web >&2 || true
  return 1
}

rollback() {
  if [[ -n "$PREVIOUS" && "$PREVIOUS" != "$APP_IMAGE_NEW $HOURS_IMAGE_NEW" ]]; then
    log "Deploy failed; rolling back to: $PREVIOUS"
    read -r prev_app prev_hours <<<"$PREVIOUS"
    if start "$prev_app" "$prev_hours" && healthy; then echo "Rolled back."; else echo "Rollback also failed; manual attention needed." >&2; fi
  else
    echo "Deploy failed and there is no previous release to roll back to." >&2
  fi
  exit 1
}

COMMIT_SHORT="${APP_IMAGE_NEW##*:}"; COMMIT_SHORT="${COMMIT_SHORT:0:7}"
log "Deploying $APP_IMAGE_NEW (previous: ${PREVIOUS:-none})"
mkdir -p backups
if "${COMPOSE[@]}" ps --status running --services 2>/dev/null | grep -qx db; then
  log "Backing up the database first"
  bash deploy/backup.sh >> backups/backup.log 2>&1 || echo "Pre-deploy backup failed; see backups/backup.log" >&2
fi
start "$APP_IMAGE_NEW" "$HOURS_IMAGE_NEW" "$COMMIT_SHORT" || rollback
healthy || rollback
echo "$APP_IMAGE_NEW $HOURS_IMAGE_NEW" > "$STATE_FILE"

log "Installing the nightly database backup (02:41 UTC)"
cron_line="41 2 * * * cd $APP_DIR && bash deploy/backup.sh >> backups/backup.log 2>&1"
{ crontab -l 2>/dev/null | grep -v 'rideleader\|deploy/backup.sh' || true; echo "$cron_line # rideleader"; } | crontab -

log "Publishing the site through the shared Caddy"
DOMAIN="$(env_value DOMAIN)"
if [[ -z "$DOMAIN" ]]; then echo "DOMAIN is not set in .env; skipping the Caddy site." >&2
elif docker inspect "$EDGE_CADDY_CONTAINER" >/dev/null 2>&1 && [[ -d "$EDGE_SITES_DIR" ]]; then
  new="$EDGE_SITES_DIR/rideleader.caddy"
  sed "s/__DOMAIN__/$DOMAIN/g" deploy/rideleader.caddy.tmpl > "$new.new"
  if ! cmp -s "$new.new" "$new" 2>/dev/null; then
    cp -f "$new" "$new.bak" 2>/dev/null || true
    mv -f "$new.new" "$new"
    if docker exec "$EDGE_CADDY_CONTAINER" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
      docker exec "$EDGE_CADDY_CONTAINER" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
      echo "Caddy reloaded with $DOMAIN."
    else
      echo "Caddy rejected the new site file; restoring the previous one and leaving Caddy untouched." >&2
      if [[ -f "$new.bak" ]]; then mv -f "$new.bak" "$new"; else rm -f "$new"; fi
    fi
  else
    rm -f "$new.new"; echo "Caddy site unchanged."
  fi
else
  echo "Shared Caddy ($EDGE_CADDY_CONTAINER) or $EDGE_SITES_DIR not found; see DEPLOY.md to set up the edge." >&2
fi

log "Cleaning up images older than 7 days"
docker image prune -af --filter "until=168h" >/dev/null || true
"${COMPOSE[@]}" ps
log "Deployed $APP_IMAGE_NEW"
