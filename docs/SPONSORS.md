# Club Sponsor Publishing

A premium feature for clubs: **€79.99 per club per year**. A club puts its own sponsors on its shared ride sheets and on its
own website, and gets plain, honestly labelled reports. Riders need no account and nothing is added to the platform's own
pages. There is no ad network, no auction, no third-party ad code and no revenue share: the €79.99 is for the software.

Branch: `sponsored_version`. **Not deployed.** Nothing here takes a payment or sends an email.

Screenshots are in [docs/sponsors](sponsors/): `admin-dashboard-desktop.jpg`, `admin-campaigns-phone.jpg`,
`admin-preview-phone.jpg`, `admin-reports-phone.jpg`, `website-widget-phone.jpg`, `ride-sheet-panel-phone.jpg`.

## Shortest walkthrough: a club admin adds a first sponsor

1. Sign in, open **Sponsors** in the top bar (club admins only). Check it says **Active**. If not, ask the site admin to switch it on after you have paid.
2. **Add a sponsor**: name, logo (PNG, JPEG or WebP), website. Save.
3. **New campaign**: pick the sponsor, a headline (60 characters), optional short text (140) and button (label 24 + https address), start and end dates, and tick *Ride sheet* (and *Website* / *Printed sheet line* if wanted). Tick *Use as the club's default*.
4. **Preview** (phone, wide, print), then **Publish**.
5. Open any shared ride sheet: the sponsor panel is near the end, after the safety information and before the footer. For your website, open **Website widget → Make my widget** and paste the code.

## What a club admin can do

- Sponsors: add, edit, archive, restore. Campaigns: create, edit, preview, publish, pause, resume, duplicate, archive. Nothing is deleted; history is kept.
- Placements: ride sheet, printed-sheet acknowledgement line, website widget. Optional tie to one ride (it follows that ride's repeats). Display order. One *default* campaign. Optional admin-only value.
- Settings: club timezone (default Europe/Dublin; campaign dates are club-local, last day included) and *rotate between several sponsors* (off by default).
- Website widget with an opaque address that can be regenerated or switched off, reports, CSV and a printable page to share by hand.
- Ride leaders (any club member who owns a ride) can, on the ride's **Share** tab, choose *automatic*, *no sponsor on this ride*, or one of the club's published ride-sheet campaigns. They cannot create or edit campaigns.

## Premium: how it is attached, and the billing integration point

Premium belongs to the **club** (`ClubPremium`, one row per club: `status active|cancelled`, `starts_at`, `expires_at`, `price_cents` 7999, `currency` EUR, `source manual|billing`, `external_ref`).

- Premium is switched on in one of two ways: **by card**, through Stripe (see [BILLING.md](BILLING.md)), or by the **site admin** (Sponsors page → *Site admin: record payment*) for a club that paid some other way. A club is only ever marked paid by a person or by a signature-verified Stripe event, never by a value from the browser.
- State is *derived*, never stored as "expired": `none`, `pending`, `active`, `expired`, `cancelled` (`app/sponsors.py:premium_state`). Only `serving` decides whether anything is shown.
- **`app/billing.py`** is the only writer of `ClubPremium`: `activate()` (manual, by months), `activate_until()` (card: runs to the end of the period Stripe was paid for, and never shortens a paid period) and `cancel()`. `app/stripe_billing.py` calls it from verified Stripe events.
- **After expiry** (plus `PREMIUM_GRACE_DAYS`, default `0`): panels and the widget stop; clicks on old panels show a short "offer has ended" page; shared ride links, route planning and every other feature keep working; sponsors, campaigns and reports stay visible; nothing can be created or published. No platform ad ever replaces a sponsor. A campaign existing is never read as premium being active.

## Which sponsor a ride shows

Premium serving → campaign published, today (club timezone) between its first and last day, sponsor not archived, placement on →
1. the ride's own choice (`Ride.sponsor_mode`: `auto` | `none` | `campaign`); *none* shows nothing; a chosen campaign that has ended falls back to automatic;
2. a campaign tied to this route (or to the route this ride was repeated from);
3. club-wide campaigns: the default, otherwise the first by display order; with rotation on, a stable pick by ride and ISO week (widget: by address and day), so it does not change while someone is reading.

One panel per sheet. The club comes from the route's record, never from the browser. The panel never feeds into the weather, daylight, closure, hours, stop or warning logic (tested).

## Repeated and historical rides

**Run this ride again** (route page → Share) makes a *new* route and ride: same line, stops, climbs, pace groups and leader's word; new date (default next weekly occurrence); no share link, sponsor choice reset to automatic; date-dependent checks are worked out fresh. The earlier ride, its date, link and notes are never changed. `Route.reused_from_id` and `series_root_id` record the lineage. Counts stay against the campaign they were recorded for, so historical reports are unaffected. Strava-sourced routes must be shared (which makes the club's own copy) first.

## Ride-sheet panel

Cream card with green edge, "Club sponsor" label, logo (empty alt: decorative), sponsor name, headline, text, optional button, optional "Offer shown until". No popups, sticky or animation; no script (the panel adds none); omitted entirely when there is nothing to show. The button goes through `/go/<token>` with `rel="sponsored noopener noreferrer"` and opens in a new tab with a screen-reader note. On paper the card is hidden and, if the campaign has *Printed sheet line* ticked, one compact line is printed instead.

## Website widget

An iframe (`/embed/<opaque token>/sponsor`): a sponsor strip only, using the club's campaigns with the *Website* placement. No rides, riders or phone numbers. No scripts, no cookies; CSP `default-src 'none'`, `frame-ancestors *`, `no-referrer`, `nosniff`, `noindex`. Cached by browsers for 5 minutes. The token can be regenerated (old one stops at once) or switched off. The snippet is `height="300"`; narrow boxes need the height. No `sandbox` or `loading="lazy"` attribute: the page has no scripts and a strict CSP, and in testing lazy loading did not trigger in a small emulated viewport.

Website builders that do not allow custom HTML/iframes (some free or locked-down plans) cannot use it; the page tells club admins this and suggests the ride-sheet link or a manual logo. We do not promise it works everywhere.

There is no "upcoming public rides" layout: the app has no public ride listing, so none was built.

## Measurement, honestly

Daily **totals only** per club / campaign / placement / metric (`SponsorDaily`): no cookies, no IPs, no fingerprints, nothing that identifies a rider. Metrics:

| Label | What it is |
|---|---|
| Ride sheet requests | Shared sheet fetched (with or without a panel) |
| Website widget requests | Widget fetched |
| Panel deliveries (requests) | Our server sent a panel with that response |
| Recorded clicks | Sponsor button used (via our redirect) |

Not claimed anywhere: unique cyclists, viewable impressions, conversions or sales. Known limits (also shown on the report pages): a request is not a person; delivery is not "seen"; HEAD requests, obvious bots, link-preview fetchers, empty user agents and prefetch/prerender headers are skipped, imperfectly; browser-cached widget loads (up to 5 min) and offline copies of a sheet are not counted again; the admin preview and `p=preview` clicks are never counted; clicks are rate-limited per address.

Click redirect: the destination is the campaign's stored address, re-validated at click time; nothing in the request can change it (no open redirect). An unknown, ended or non-premium campaign gets `410`.

Reports: `/sponsors/reports` (date range, per campaign), `/sponsors/report` (printable page to share by hand), `/sponsors/reports.csv` (spreadsheet formula characters are neutralised). Never sent by email automatically. Admins only; another club's data is never returned.

## Security summary

- Everything club-supplied is plain text only (no `<` `>`, control characters or `javascript:`), length-limited, and auto-escaped by the templates.
- Destinations must be `https://`, a public host name (no IPs, credentials, local/internal names or odd ports).
- Logos: PNG/JPEG/WebP only (SVG refused), up to 1.5 MB and 24–4000 px, **re-encoded** server-side (metadata dropped), served with `nosniff` and a locked-down CSP via an unguessable token; archived sponsors' logos 404.
- Tenant isolation: every query is scoped by the signed-in person's current club; ids from the browser (sponsor, campaign, route) are always re-checked against that club. Only the site admin can change premium.
- Rate limits on clicks. CSRF as for the rest of the app.

## Configuration and migrations

- **Migrations:** automatic at start-up (`_ADDED_COLUMNS` + `db.create_all()` under the existing advisory lock): new tables `club_premium`, `sponsor`, `sponsor_campaign`, `sponsor_daily`, `club_embed`, `club_billing`, `billing_invoice`, `billing_event`; new columns `route.reused_from_id`, `route.series_root_id`, `ride.sponsor_mode`, `ride.sponsor_campaign_id`. All additive and idempotent.
- **Config:** `PREMIUM_GRACE_DAYS` (env, default `0`; `7` is sensible with card billing so a late retry doesn't switch panels off) and the `STRIPE_*` settings in [BILLING.md](BILLING.md). Club settings `timezone` and `sponsor_rotation` are edited on the Sponsors page.
- **Proxy:** `deploy/rideleader.caddy.tmpl` now sends `X-Frame-Options: SAMEORIGIN` on everything except `/embed/*`, so the widget can be framed by club sites (the app itself also sets `frame-ancestors *` for that path only). Validated with Caddy 2; **not deployed**: it must be rolled out with the release or the widget will be blocked in production.
- Ride sheets are still served fresh. They contain some small functional inline scripts that existed before; the sponsor panel adds none.

## Tests

`tests/test_sponsors.py` (82 tests) and `tests/test_billing_stripe.py` (35 tests, see BILLING.md). The sponsor tests cover: premium states, expiry and grace, platform-admin-only activation, tenant isolation on every sponsor/campaign/report/CSV/embed route, campaign phases and timezone, placement flags, selection precedence, none/explicit/rotation, panel markup and position, print line, expiry keeps links alive, run-again and historical reporting, widget content/headers/revocation, click redirect and open-redirect attempts, bot/preview filtering, honest report labels, CSV isolation and formula safety, validation, logo handling and a no-ad-tech check. Run alone (shared test database):

```
docker compose run --rm -e TEST_DATABASE_URL=postgresql+psycopg://rlk:rlk_dev_only@db:5432/rlk_test web pytest -q -W ignore -p no:cacheprovider
```
