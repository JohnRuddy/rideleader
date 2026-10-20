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
| App stack | `/opt/rideleader` (compose project `rideleader`: db, redis, hours, rl-web, worker, scheduler) |
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

## Clubs, invite codes and first sign-in

People sign in with an emailed link, and the first time they also enter an **invite code** from a club admin.
Roles: the **site admin** (you; the first person ever to sign in) creates clubs; a **club admin** invites and removes
people in their club; **members** share routes, stops and closures inside their club. A sample club called "Velo Club"
exists from the start.

Make a code from the site (Club name in the top bar, then Invite people), or on the server:

```
cd /opt/rideleader
docker compose -f docker-compose.prod.yml exec rl-web flask --app app club-invite "Velo Club" --role admin
```

The code is shown once; only a hash is stored. Removing someone from their last club signs them out on their next click.

## First sign-in when email is down

Sign-in links are emailed. If email is not working, print one on the server (this works for the site admin and for
anyone already in a club):

```
cd /opt/rideleader
docker compose -f docker-compose.prod.yml exec rl-web flask --app app login-link you@example.com
```

The first person to sign in becomes the admin. In production the link is never shown on the web page.

## Operations

```
cd /opt/rideleader
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f rl-web
bash deploy/backup.sh                       # extra backup now
gunzip -c backups/<file>.sql.gz | docker compose -f docker-compose.prod.yml exec -T db psql -U rlk -d rlk   # restore
```

## Things to know

- **Strava**: leave `STRAVA_MODE=off` until the Strava application's Authorization Callback Domain is changed from
  `localhost` to the production hostname, then set `STRAVA_MODE=live` with the client id and secret in `PROD_ENV`.
- **Card billing (Club Sponsor Publishing)**: leave `STRIPE_SECRET_KEY` empty to keep it off (the site admin can still activate clubs by
  hand). To switch it on, follow [docs/BILLING.md](docs/BILLING.md): `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, optionally
  `STRIPE_PRICE_ID`, `STRIPE_AUTOMATIC_TAX`, `STRIPE_TAX_BEHAVIOR` and `PREMIUM_GRACE_DAYS` go in `PROD_ENV`. Use Stripe **test** keys first.
- **Map data**: production uses the public Overpass and BRouter servers (fair-use throttled). The `maps` profile in
  `docker-compose.yml` runs your own, which needs several GB of disk and a long first import, so it is not on this
  shared server by default.
- **Map tiles**: the public OpenStreetMap tile server is for light use. Set `TILE_URL` to a tile provider if traffic grows.
  The ride sheet's map picture (`/r/<token>/map.webp`) is made by the server from those tiles (about 10 to 30 per route,
  cached 14 days in Redis, identified by a `RideLeaderKit/1.0` user agent), so riders' browsers never contact a tile server.
  `BASEMAP_ENABLED=0` turns the picture off and the sheet goes back to the plain route outline. If the tiles can't be reached
  the sheet shows the outline too.
- **Service names on `edge`**: a compose service name becomes a DNS name on every network it joins, so this app's web
  service is `rl-web`. A second service called `web` on `edge` would hijack checkmysitemap.com's traffic.
- **Shared Caddy**: if the checkmysitemap.com deploy ever replaces its Caddy files, keep the `edge` network, the
  `/opt/edge/sites` mount and the `import` line, or this site goes dark.
