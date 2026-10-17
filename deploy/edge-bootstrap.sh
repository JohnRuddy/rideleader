#!/usr/bin/env bash
# One-time, as root on the server. Prepares the shared "edge" for more than one site:
#   - a Docker network "edge" that Caddy and each app's web container join
#   - a directory /opt/edge/sites where each app drops its own Caddy site file
# Then the shared Caddy (in the checkmysitemap.com stack) must join "edge" and `import /etc/caddy/sites/*.caddy`;
# see DEPLOY.md. Safe to run again.
set -euo pipefail
docker network inspect edge >/dev/null 2>&1 || docker network create edge
install -d -m 755 -o deploy -g deploy /opt/edge /opt/edge/sites
echo "edge network and /opt/edge/sites are ready."
