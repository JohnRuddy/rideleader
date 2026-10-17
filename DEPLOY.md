# Deployment runbook

Ride Leader Kit runs on the same server as checkmysitemap.com (Hostinger VPS, Ubuntu 24.04), as its own Docker
Compose project in `/opt/rideleader`. The two share one Caddy, which owns ports 80 and 443 and the HTTPS certificates.

```
internet -> Caddy (checkmysitemap stack) --edge network--> rideleader-web:8431
                                          \--> web:8000  (checkmysitemap)
```

## What lives where

| Thing | Where |
|---|---|
| App stack | `/opt/rideleader` (compose project `rideleader`: db, redis, hours, web, worker, scheduler) |
| Production settings | `/opt/rideleader/.env` (mode 600, written by the pipeline from the `PROD_ENV` secret) |
| Site file for Caddy | `/opt/edge/sites/rideleader.caddy` (written by `deploy/deploy.sh`) |
| Backups | `/opt/rideleader/backups`, nightly 02:41 UTC and before every deploy, 30 days kept |
| Images | `ghcr.io/johnruddy/rideleader` and `ghcr.io/johnruddy/rideleader-hours`, tagged by commit |

## One-time setup (already done on the current server)

1. `bash deploy/edge-bootstrap.sh` as root: creates the Docker network `edge` and `/opt/edge/sites`.
2. The checkmysitemap.com stack's Caddy joins `edge`, mounts `/opt/edge/sites` at `/etc/caddy/sites` and has
   `import /etc/caddy/sites/*.caddy` in its Caddyfile (change made in the SitemapChecker repo).
3. DNS: an `A` record for the app's hostname pointing at the server's IPv4 address.
4. Make the `.env`: `bash deploy/make-env.sh <domain> > .env && chmod 600 .env`, then add `SMTP_*` and, when
   ready, `STRAVA_*`.
5. GitHub: environment `production` with secrets `DEPLOY_HOST`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS`, `PROD_ENV`;
   repository variables `DEPLOY_ENABLED=true` and `PUBLIC_URL=https://<domain>`.
6. Protect `main` (Settings -> Branches): require a pull request and the status checks `test`, `lint`, `secrets`.

## Day to day

Merge to `main`. That is the deploy. Check the Actions run, or `curl https://<domain>/healthz`.

## First sign-in (before email is set up)

Sign-in links are emailed. Until `SMTP_*` is configured, print one on the server:

```
cd /opt/rideleader
docker compose -f docker-compose.prod.yml exec web flask --app app login-link you@example.com
```

The first person to sign in becomes the admin. In production the link is never shown on the web page.

## Operations

```
cd /opt/rideleader
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f web
bash deploy/backup.sh                       # extra backup now
gunzip -c backups/<file>.sql.gz | docker compose -f docker-compose.prod.yml exec -T db psql -U rlk -d rlk   # restore
```

## Things to know

- **Strava**: leave `STRAVA_MODE=off` until the Strava application's Authorization Callback Domain is changed from
  `localhost` to the production hostname, then set `STRAVA_MODE=live` with the client id and secret in `PROD_ENV`.
- **Map data**: production uses the public Overpass and BRouter servers (fair-use throttled). The `maps` profile in
  `docker-compose.yml` runs your own, which needs several GB of disk and a long first import, so it is not on this
  shared server by default.
- **Map tiles**: the public OpenStreetMap tile server is for light use. Set `TILE_URL` to a tile provider if traffic grows.
- **Shared Caddy**: if the checkmysitemap.com deploy ever replaces its Caddy files, keep the `edge` network, the
  `/opt/edge/sites` mount and the `import` line, or this site goes dark.
