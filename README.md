# Ride Leader Kit

Steps 1-9 of the build (all nine built; see the status table at the end of this file) (step 7 = Strava, read-only, built and tested against mocks; LIVE TESTING PENDING) (step 6 = route editor: drag/click via points snapped to roads with BRouter, undo/redo, waypoints re-anchored, auto-rescan) (step 5 = climb detection, ease-back / stop regroups, descent and long-stretch suggestions) (step 4 = ride plan with pace groups, café opening hours on arrival via an opening_hours.js sidecar) (step 3 = exports: FIT, TCX, GPX for Garmin, Wahoo, Karoo, generic, Strava hand-off): GPX import, route model, map and linked elevation profile; Overpass POI scan (background worker, tile cache), corridor matching, bulk waypoint panel.
See [DESIGN.md](DESIGN.md) for the full plan.

```bash
docker compose up --build        # http://localhost:9431
docker compose exec db psql -U rlk -d rlk -c "CREATE DATABASE rlk_test"   # once
docker compose exec db psql -U rlk -d rlk_test -c "CREATE EXTENSION postgis"  # once
docker compose run --rm -e TEST_DATABASE_URL=postgresql+psycopg://rlk:rlk_dev_only@db:5432/rlk_test web pytest -q
```

Tests need their own database (`rlk_test`) because they drop all tables afterwards. `samples/kildare-sample.gpx` is a synthetic route for manual testing.

`TILE_URL` selects the basemap (default is OSM's public raster tiles, fine for development only; choose a proper provider before launch).

Services: `web` (Flask), `worker` (RQ, runs scans; restart it after changing job code), `db` (PostGIS), `redis`. `OVERPASS_URL` can point at a self-hosted instance. Overpass tiles are cached for `POI_TTL_DAYS` (default 7).

Exports live in `app/export/`; the category -> FIT / GPX sym / Karoo / TCX mapping is one table in `app/export/mapping.py`. Golden files are in `tests/golden/`; regenerate after an intentional change with `UPDATE_GOLDEN=1` (review the diff!).

The `hours` service (`hours/`, Node + opening_hours.js 3.15.0) parses OSM opening_hours incl. Irish bank holidays; the app does all ETA and open/closed classification. If it is down the UI says hours are unknown and never shows a café as open.

Climbs and regroups: `app/climbs.py` (profile smoothing, detection, gradient/bend lookups), `app/regroup.py` (placement rules), `app/climb_service.py` (persistence). Regroups are ordinary waypoints (kind `regroup`) and export as course points. `samples/hilly-sample.gpx` has two climbs and a long descent.

Route editor: `app/editor.py` (pure engine: legs between anchors; only ~1.5 km around an edit is rerouted, the rest of an imported line is never silently re-snapped), `app/routing.py` (RoutingProvider + BRouter), `app/elevation.py` (ElevationProvider + Open-Meteo), `app/history.py` (undo/redo snapshots), `app/edit_service.py` (re-anchors waypoints, carries climb choices, queues a rescan). Set `BROUTER_URL` to a self-hosted BRouter before real use: the public brouter.de is shared and throttled.

## Strava (step 7): status and going-live checklist

Built and tested against a fake client and a development mock (`STRAVA_MODE=mock`, the compose default).
**Nothing has been run against the real Strava yet.** Before using it for real:

1. Register an API application at strava.com/settings/api; note the client id and secret and set its callback domain.
2. Set `STRAVA_MODE=live`, `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `STRAVA_VERIFY_TOKEN` (any long random string), `APP_BASE_URL`.
3. Set the production secrets: `RLK_ENV=production`, `SECRET_KEY`, `TOKEN_ENCRYPTION_KEY` (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`), `SESSION_COOKIE_SECURE=1`, SMTP settings, `CONTROLLER_NAME`, `CONTROLLER_CONTACT`.
4. Once the site is reachable over HTTPS: `docker compose exec web flask --app app strava-subscribe https://YOUR-HOST/strava/webhook`, then set `STRAVA_SUBSCRIPTION_ID` and restart.
5. **Brand assets (required by Strava's brand guidelines):** put Strava's official "Connect with Strava" button at `app/static/strava/connect_with_strava.svg` and the "Powered by Strava" mark at `app/static/strava/powered_by_strava.svg`. Until then plain text stands in; do not launch like that.
6. Read Strava's current API Policy yourself and confirm the figures below. Strava starts every app at a one-athlete limit; a capacity review is needed before the club rolls out.

What the code enforces: read-only access (a test fails if the client could write anything); 7-day cache with a purge every 15 minutes; deletion on disconnect, revocation (webhook), Strava rejecting our token, and account deletion, with an email confirmation; deletions made on Strava are picked up by a re-check (webhooks don't cover routes) run every 6 hours on anything unchecked for 20 hours; Strava data visible only to its athlete; nothing shared except a separate copy the athlete confirms is their own; no AI/ML (a test fails if an AI library is imported).

Figures taken from the project brief and **not confirmed in Strava's published documents I could read**: the 7-day cache limit (secondary sources agree) and the 48-hour window for reflecting Strava-side deletions (no source found). Both are implemented as briefed.


## Weather, warnings, closures (step 8)

- **Warnings** (`app/warnings_feed.py`): Met Éireann's JSON feed is polled every 10 minutes by the `scheduler` service and stored
  exactly as issued in its own columns (plus the raw item). Expired and withdrawn warnings are removed. A warning is relevant if
  it names a county the route passes through (by region code **or** by the county names in its headline) and its window
  overlaps the ride plus a buffer (default 2 h, a club setting). If the feed can't be reached the banner says
  "Couldn't check Met Éireann weather warnings" instead of implying there are none. The banner is shown at the top of the plan,
  the ride sheet and the shared page, and accompanies every response that carries forecast data (the licence requires both together).
  Orange and Red ask the leader to consider postponing and draft a "spin under review" message; nothing is ever cancelled automatically.
- **Counties**: Tailte Éireann's generalised county boundaries (CC BY 4.0, simplified, in `data/counties.geojson`) are loaded into
  PostGIS (`ie_county`). Met Éireann's region codes are FIPS 10-4. Five pairs were confirmed from live CAP data
  (EI06 Donegal, EI10 Galway, EI14 Leitrim, EI20 Mayo, EI25 Sligo); the rest come from the FIPS standard as I know it, which is why
  matching also uses the county names in each headline (a wrong code can only add a warning, never hide one). Marine areas
  (`EI8xx`) and Northern Ireland are not covered.
- **Forecast** (`app/weather.py`): Met Éireann's point forecast inside the Republic, Open-Meteo elsewhere (behind `WeatherProvider`),
  cached for an hour per ~5 km cell. Hourly to ~90 h, then 3-6 hourly; the UI labels these Detailed / Indicative / Outlook only
  and widens the stated uncertainty. Weather is looked up along the route at each pace group's own arrival time, with wind
  worked out relative to the direction of travel.
- **Daylight** uses the NOAA sunrise equation; checked against Open-Meteo's published sunrise/sunset to within 2 minutes.
- **Closures** (`app/closures.py`): leader/club-entered closures (always available, with an end date), roadworks and closed roads
  from OpenStreetMap, and any ArcGIS feature layer configured in `ARCGIS_CLOSURE_SOURCES` (a JSON list of
  `{name, label, url, mapping: {id, description, start, end, url}}`). **Not available yet and said so on screen:** TII national road events
  (no public feed found; the DATEX II endpoint needs confirming with opendata@tii.ie), most county councils (notices only), TrafficWatchNI.
  I could not confirm the Fingal County Council closures service URL or field names, so it is not configured. "Reroute around this"
  uses BRouter's `nogos`.

## Ride sheet, sharing, club library (step 9)

- **Ride sheet** (`app/ride_sheet.py`): warnings first, then date/meeting/leader, pace groups, an inline-SVG map and elevation
  profile, stops and regroups with café opening status per group, climbs, hazards and closures (with coverage and a "not a guarantee"
  line), road and surface summary, weather, emergency details with the nearest defibrillator and pharmacy by 10 km section, ways home.
  Server-rendered, ~16 KB, no scripts to fetch, readable in sunlight, prints cleanly, and a network-first service worker keeps the last copy
  for poor signal (with an "offline copy, check met.ie/warnings" banner).
- **Sharing**: a long random token the leader can revoke. **A route imported from Strava can never be shared**: the leader publishes
  their own copy to the club first. Shared pages are `noindex`.
- **WhatsApp text** and an A5 **cue card PDF** come from the same data as the page.
- **Club stop library** (`/club/stops`): favourite cafés, regroup spots, hazards and verified defibrillators with a last-verified date.
  Club stops are badged and listed first, replace OpenStreetMap copies of the same place within 40 m, and a club café's hours override OSM's.
- **Hazards**: busy main roads, unpaved stretches (from OSM road tags near the route) and steep sections (over 10%, from the profile)
  become unselected hazard waypoints.

## Status: what is verified and what is not

| Area | State |
|---|---|
| Everything above | Unit and integration tested; weather, warnings, BRouter and Open-Meteo checked once against the live services |
| Strava | Built and tested against mocks only. **Live testing pending** (see the Strava checklist above) |
| Strava brand artwork | Not installed (required before launch) |
| 7-day / 48-hour Strava figures | From the brief; not confirmed in Strava's published text I could read |
| BRouter | Public instance is dev-only; self-host before real use |
| TII / council closures | Not available (no public feed found) |
| Met Éireann FIPS codes | 5 of 26 confirmed from live data; names back them up |
| Map overlays (wind colours, warning bands, closures) | Code reviewed and data-path tested, but not seen rendered: the browser pane was hidden during that session |
| Touch drag-editing on a real phone | Not tested on a device |
| Garmin / Wahoo / Karoo files on real hardware | Not tested; Karoo GPX POI import is unconfirmed by Hammerhead |
| Hosting, SMTP, `CONTROLLER_NAME` / `CONTROLLER_CONTACT`, TLS | To be set up before club use |

## Local map servers (own Overpass and BRouter)

The public Overpass and BRouter servers are shared, rate limited and sometimes unreachable. The `maps` profile in
`docker-compose.yml` runs our own copies for Ireland and Northern Ireland. Nothing starts or downloads until you ask.

| Service | What it does | Data it needs | Download (Oct 2026) | Disk |
|---|---|---|---|---|
| `overpass` | stop search (cafes, water, AEDs...) | Geofabrik Ireland + NI extract, then daily updates | about 415 MB | several GB once indexed |
| `brouter` | route editor snapping | routing tiles W10_N50, W15_N50, W10_N55 from brouter.de | about 49 MB | about 50 MB |

Start (first run downloads, converts and indexes; Overpass takes a while, then serves):

    docker compose --profile maps up -d --build

When `docker compose --profile maps ps` shows both healthy, point the app at them in `.env`:

    OVERPASS_URL=http://overpass/api/interpreter
    OVERPASS_MIN_INTERVAL=0
    OVERPASS_WORKERS=4
    BROUTER_URL=http://brouter:17777/brouter
    BROUTER_MIN_INTERVAL=0

then `docker compose up -d`. Remove those lines to go back to the public servers. Overpass keeps itself current from
Geofabrik daily diffs; BRouter tiles are refreshed by deleting the `brouter-segments` volume and starting again.

## Deployment and contributing

CI, branching and the release flow are in [CONTRIBUTING.md](CONTRIBUTING.md); the server runbook is [DEPLOY.md](DEPLOY.md).

## Data licences

Map data © OpenStreetMap contributors (ODbL). County boundaries from Tailte Éireann (CC BY 4.0). Weather data from Met Éireann (CC BY 4.0).
Strava logos are Strava's trademarks, used under their brand guidelines. No licence is granted for the code in this repository yet.
