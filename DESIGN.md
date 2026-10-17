# Ride Leader Kit — pre-build review

Status: proposal for review. Nothing built yet. Probes run 2026-10-08.

## 1. Data source reachability (probed from this machine)

| Source | Result | Notes |
|---|---|---|
| Met Éireann forecast `openaccess.pf.api.met.ie/metno-wdb2ts/locationforecast?lat=;long=` | **200 XML**, ~96 KB, schema `weatherapi-0.4.xsd` | Works. Parse with lxml; pick `from==to` point nodes + interval nodes (precip) by nearest time. |
| Met Éireann warnings `met.ie/Open_Data/json/warning_IRELAND.json` | **200 JSON** | Live data present. Fields: `id, capId, type, severity, certainty, level, issued, updated, onset, expiry, headline, description (HTML), status, regions[]`. **Regions are codes (`EI06` = Donegal, `EI10` Galway, …), not names**, so we need a code→county table, and a boundary layer keyed by those codes. Descriptions contain HTML: sanitise for display only, never alter text. |
| Overpass `overpass-api.de` | **200**, 4 slots free | Fine for dev. Production: self-host or cache aggressively. |
| BRouter public (`brouter.de`) | **200** GeoJSON with `trekking` profile | Fine for dev; public instance is best-effort. Self-host in Docker for real use (needs Ireland segment files, ~tens of MB). Check that a road-bike profile (e.g. `fastbike`) fits. |
| Open-Elevation | **200** | Public instance is flaky/slow historically. Open-Meteo elevation (also 200, 90 m Copernicus) is a better fallback. Both behind `ElevationProvider`. |
| Strava API | **401 without token (expected)** | Reachable; needs a registered app to test further. |
| **TII road events (national roads)** | **Not confirmed.** `data.tii.ie` is a JS app (Blazor, "unhandled error" to plain HTTP); `/Datasets/Traffic-Events/` returned 403. data.gov.ie lists only TII *traffic counts / WIM*, not live events. Legacy NRA DATEX II feed needed credentials per the DATEX II node directory (entry dated 2011). | **Blocker for automated TII closures.** TII's site says its ITS datasets are DATEX II, 5-minute refresh, CC BY 4.0, but I couldn't find the endpoint. Action: ask opendata@tii.ie (address appeared on a TII dataset record) or open data.tii.ie in a real browser to find the ITS section. |
| **Fingal CoCo "Road Closures Temporary"** | Metadata on data.gov.ie (CC BY 4.0, modified 2026-09-22); resource is an HTML link to ArcGIS Hub `data.fingal.ie/datasets/FingalCoCo::road-closures-temporary-fcc`. | Probably an ArcGIS FeatureServer behind it, so queryable as GeoJSON; need the service URL from the Hub page (my ArcGIS search found nothing, so needs a browser look). **Fingal is Dublin, not Kildare.** |
| **Kildare County Council closures** | Nothing found in data.gov.ie. | Assume notices only → link out + manual entry. |
| **TrafficWatchNI** | Site up (200). Documented RSS paths I guessed all 404. Search says RSS with feed-specific T&Cs, OGL on the older listing, ITS Directive allows charging. | Need the real feed URL and T&Cs from the site's RSS FAQ before coding an adapter. NI is low priority for a Kildare club. |
| Google Places | Not tested (needs key). | Off by default. |

**Bottom line on closures:** for the Kildare launch, the dependable adapters are (1) leader/club-entered closures, (2) OSM construction/access tags, (3) link-outs. TII, Fingal and TWNI adapters stay "planned" until endpoint and terms are confirmed. The UI copy must say coverage is partial (your rule: never imply "clear").

## 2. Assumptions (please correct)

1. Single-club launch (Kildare), ≤ ~50 users. Strava runs in single-athlete mode first (Strava's default 1-athlete limit until capacity review).
2. Hosting: Docker Compose (app, worker, Postgres+PostGIS, Redis, BRouter). **Local venvs on this box can't load C extensions (per my notes), so dev is Docker-only.** Dev port 8765 (8096–8295 is reserved on this host).
3. RQ over Celery (simpler; jobs are short). Postgres is the source of truth.
4. MapLibre GL JS with vanilla ES modules + a small build (esbuild/Vite). Basemap tiles: need a provider with a licence for app use (OSM's own tile servers forbid heavy use) — MapTiler/Stadia free tier or self-hosted OpenMapTiles. **Decision needed.**
5. Opening hours: a small Node sidecar running `opening_hours.js` (most complete; handles `PH`, seasonal and week rules, Irish holidays via its country data). Python ports are incomplete.
6. Auth: email magic link for leaders; viewers of share links need none. Club has roles `member`, `leader`, `admin`.
7. FIT export via the official `fit-tool`/Garmin FIT SDK writer (fitparse is read-only, used in tests only; `garmin_fit_sdk` can decode for validation).
8. Strava consent covers the **leader's own routes only**. Publishing converts to a club-owned GPX; nothing Strava-derived is stored on the club route except what's in the file the leader owns. The consent screen is logged.
9. All analysis is deterministic. No LLM anywhere in the pipeline, so no Strava-compliance risk there.
10. Times are stored UTC, displayed `Europe/Dublin`. Ride date + start are local.
11. Karoo type mapping and Wahoo behaviour are from the spec you wrote; I'll verify against current Hammerhead/Wahoo docs when building exports and record any differences.
12. Met Éireann warnings: relevant if area ∩ route counties and window overlaps ride window ± buffer. Warnings stored verbatim (raw JSON + text columns), never edited.

## 3. Internal route and waypoint model

The key idea: **a route is an ordered polyline in a linear-referencing system. Everything else hangs off `s` (metres along route).**

```
Route
  id, club_id, owner_id, title, source ('gpx'|'strava'|'editor'), visibility
  geom            LINESTRINGZ 4326       -- canonical track
  length_m, ascent_m, descent_m
  version         int                    -- bump on every geometry change

TrackPoint (stored as arrays on Route for speed, not rows; see schema)
  i, lat, lon, ele_m, dist_m (cumulative), grade (smoothed)

Waypoint
  id, route_id, kind (category enum), name, note
  lat, lon
  s_m             float   -- distance along route; the anchor (authoritative)
  offset_m        float   -- straight-line distance off route
  side            'L'|'R'|null
  pinned          bool    -- survives rescans
  included        bool    -- in export
  origin          'osm'|'club_stop'|'manual'|'computed'
  osm_ref         'node/123'|null, tags jsonb
  extra           jsonb   -- e.g. cafe hours, regroup mode, ease_back_km
```

**Out-and-back / figure-of-eight:** each waypoint stores `s_m` as the anchor. When a POI lies near two parts of the route it gets one candidate per pass (multiple `s_m` for the same `osm_ref`, e.g. café at km 22 outbound and km 58 return). Snapping uses PostGIS `ST_LineLocatePoint` on route *segments* with a monotonic window, not on the whole line, so shared sections resolve correctly. After an edit, waypoints are re-anchored by keeping `s_m` and, if the line before that point changed, re-projecting within a window around the old `s_m` (pinned ones too), flagging any that moved more than 100 m for leader review.

## 4. Proposed database schema (PostgreSQL + PostGIS)

```sql
CREATE EXTENSION postgis;

-- identity & tenancy
club(id, name, slug, tz default 'Europe/Dublin', settings jsonb, -- spin days, default start, pace groups, safety thresholds
     created_at)
app_user(id, email unique, display_name, phone, created_at, deleted_at)
membership(club_id, user_id, role, PRIMARY KEY(club_id,user_id))

-- strava (cached, purge ≤7d, per-athlete only)
strava_connection(user_id PK, athlete_id, access_token_enc, refresh_token_enc, expires_at,
                  scope, consent_version, consented_at, revoked_at)
strava_route_cache(id, user_id, strava_route_id, name, summary_polyline, gpx bytea,
                   fetched_at, expires_at)           -- DELETE WHERE expires_at < now(); wiped on disconnect
consent_log(id, user_id, kind, version, text_hash, at, ip_hash)

-- routes
route(id, club_id, owner_id, title, source, visibility,   -- 'private'|'club'|'link'
      geom geometry(LineStringZ,4326), length_m, ascent_m, descent_m,
      dist_m double precision[], ele_m real[],            -- aligned with geom vertices
      version int, share_token text unique null,
      shared_from_strava_consent_id bigint null,          -- set only after explicit consent
      created_at, updated_at, deleted_at)
route_edit(id, route_id, version, op jsonb, created_at)    -- undo/redo log
route_via(id, route_id, seq, lat, lon, locked bool)        -- editor via-points

-- waypoints & stops
waypoint(id, route_id, kind, name, note, geom geometry(Point,4326),
         s_m double precision, offset_m real, side char(1),
         pinned bool, included bool, origin text, osm_ref text, tags jsonb, extra jsonb,
         club_stop_id bigint null, created_at, updated_at)
CREATE INDEX ON waypoint(route_id, s_m);

club_stop(id, club_id, kind, name, note, geom geometry(Point,4326), tags jsonb,
          verified_by, last_verified_at, active bool)     -- verified AEDs, cafés, regroups, hazards

-- poi cache (OSM, not Strava; long TTL OK)
poi_tile(z, x, y, category, fetched_at, expires_at, payload jsonb, PRIMARY KEY(z,x,y,category))
osm_poi(osm_ref pk, kind, geom, tags jsonb, fetched_at)    -- optional normalised store
scan_job(id, route_id, route_version, status, progress, error, started_at, finished_at)

-- derived analysis (recomputed per route version)
climb(id, route_id, version, s_start_m, s_end_m, length_m, gain_m, avg_grade, max_grade, score, category)
regroup(id, route_id, climb_id null, mode ('ease_back'|'stop'), s_m, ease_km real, waypoint_id null)
hazard(id, route_id, version, kind, s_start_m, s_end_m, geom, source, detail jsonb)

-- rides
ride(id, club_id, route_id, leader_id, date, start_time, meeting_name, meeting_geom,
     leader_phone, notes, share_token unique, status)
pace_group(id, ride_id, name, speed_kmh, stop_min)

-- cafés
cafe_info(waypoint_id PK, opening_hours_raw, hours_source ('osm'|'club'|'places'),
          club_note, club_hours_raw, confirmed_by, confirmed_at)
-- ETA/open-state is computed at read time per (ride, pace_group), not stored.

-- weather, warnings, closures
forecast_cache(grid_lat, grid_lon, fetched_at, payload xml/jsonb, PRIMARY KEY(grid_lat,grid_lon)) -- 1h TTL
county(code char(4) PK, name, geom geometry(MultiPolygon,4326))       -- EI01..EI26 etc
warning(id, cap_id unique, type, level, severity, certainty, issued, updated, onset, expiry,
        headline, description_html, regions text[], raw jsonb, fetched_at)  -- stored verbatim; purge when expired
warning_poll(id, at, ok bool, error)                                  -- drives "couldn't check" banner
closure(id, source, external_id, geom geometry(Geometry,4326), starts_at, ends_at,
        description, url, club_id null, entered_by null, fetched_at, expires_at)

-- exports & gdpr
export_job(id, route_id, device, format, status, file_path, warnings jsonb, created_at)
user_pref(user_id, key, value)            -- remembered device picker etc.
deletion_request(id, user_id, requested_at, completed_at, confirmation_sent_at)
```

Notes:
- Per-point arrays on `route` (not a row per point) keep a 10k-point route to one row; profile and climbs read directly from them.
- `warning` stores Met Éireann fields verbatim in dedicated columns plus `raw`.
- `strava_route_cache` is the only place Strava route data lives. A purge job deletes rows past `expires_at`, and disconnect cascades. Published club routes are copies of a GPX the leader supplies, with no foreign key into the cache.
- Retention of `route_edit` log: 30 days after last edit.

## 5. Provider interfaces

`RoutingProvider.route(points, profile, avoid) -> LineStringZ + stats`
`ElevationProvider.lookup(points) -> [ele]`
`PoiProvider` (Overpass; later Places) · `WeatherProvider` (Met Éireann; Open-Meteo fallback)
`WarningsProvider` · `TrafficProvider` (club-entered, OSM, later TII/Fingal/TWNI)
`HoursEvaluator` (opening_hours.js sidecar) · `Exporter` (one class per device, all fed by the single category mapping table)

## 6. Open questions for you

1. **Basemap tiles:** pay-per-use provider, free tier, or self-host?
2. **Strava:** have you registered a Strava API app? I need client id/secret to go beyond the 401 test.
3. **TII:** are you happy to email TII for the DATEX II endpoint, or should I start with club-entered + OSM closures only?
4. **Hosting target** (VPS vs home server) so BRouter and Overpass choices are realistic.
5. Auth: magic-link email OK, and do you have an SMTP/transactional mail service?
6. Should I scaffold step 1 (GPX import, route model, map + profile) next, in `E:\Powershell\ride-leader-kit`?
