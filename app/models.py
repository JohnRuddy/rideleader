from datetime import datetime, timezone

from geoalchemy2 import Geometry
from sqlalchemy import BigInteger, Double, Float, LargeBinary, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from . import db


def _now():
    return datetime.now(timezone.utc)


class Route(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    source = db.Column(db.String(20), nullable=False, default="gpx")
    geom = db.Column(Geometry("LINESTRING", srid=4326))
    length_m = db.Column(Double, nullable=False)
    ascent_m = db.Column(Double)
    descent_m = db.Column(Double)
    # Aligned 1:1 with geom vertices (see DESIGN.md section 4)
    dist_m = db.Column(ARRAY(Double), nullable=False)
    ele_m = db.Column(ARRAY(Float))
    version = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime(timezone=True), default=_now)
    waypoints = db.relationship("Waypoint", backref="route", cascade="all, delete-orphan", order_by="Waypoint.s_m")
    ride = db.relationship("Ride", backref="route", uselist=False, cascade="all, delete-orphan")
    road_summary = db.Column(JSONB)               # road types and surfaces along the line (roadinfo.summarise)
    climb_settings = db.Column(JSONB)             # regroup threshold etc.; see climb_service.DEFAULT_SETTINGS
    climbs_version = db.Column(db.Integer)        # route.version the climbs were detected for
    editor = db.Column(JSONB)                     # via points and legs (see editor.Track); null until first edited
    owner_id = db.Column(db.Integer, db.ForeignKey("app_user.id", ondelete="CASCADE"), index=True)  # null = unowned (legacy / signed-out upload)
    visibility = db.Column(db.String(10), nullable=False, default="club", server_default="club")    # private | club
    club_id = db.Column(db.Integer, db.ForeignKey("club.id", ondelete="SET NULL"), index=True)   # the club it is shared with (if visibility = club)
    reused_from_id = db.Column(db.Integer, db.ForeignKey("route.id", ondelete="SET NULL"))       # the route this one is another run of
    series_root_id = db.Column(db.Integer, db.ForeignKey("route.id", ondelete="SET NULL"))       # the first route in that chain (null = this is it)
    strava_route_id = db.Column(BigInteger)       # set only on routes imported from Strava (source = 'strava')
    strava_expires_at = db.Column(db.DateTime(timezone=True))  # Strava data is kept for at most 7 days
    state_seq = db.Column(db.Integer)             # undo cursor: seq of the current RouteState; null = no history yet
    climbs = db.relationship("Climb", backref="route", cascade="all, delete-orphan", order_by="Climb.s_start_m")


class Waypoint(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    route_id = db.Column(db.Integer, db.ForeignKey("route.id", ondelete="CASCADE"), index=True, nullable=False)
    kind = db.Column(db.String(30), nullable=False, default="generic")
    name = db.Column(db.String(200), nullable=False)
    note = db.Column(db.Text)
    geom = db.Column(Geometry("POINT", srid=4326))
    s_m = db.Column(Double, nullable=False)  # authoritative anchor: metres along route
    offset_m = db.Column(Float)
    pinned = db.Column(db.Boolean, default=False, nullable=False)
    included = db.Column(db.Boolean, default=True, nullable=False)
    origin = db.Column(db.String(20), default="gpx", nullable=False)  # gpx | osm | manual
    tags = db.Column(JSONB)
    side = db.Column(db.String(1))
    osm_ref = db.Column(db.String(40), index=True)  # e.g. node/123
    planned_stop = db.Column(db.Boolean, default=False, nullable=False)  # the group dwells here (adds stop time to later ETAs)
    club_hours = db.Column(db.String(300))                # opening_hours string entered/confirmed by a leader
    club_hours_at = db.Column(db.DateTime(timezone=True))
    osm_hours_changed_at = db.Column(db.DateTime(timezone=True))  # when a rescan saw OSM's hours change
    club_stop_id = db.Column(db.Integer, db.ForeignKey("club_stop.id", ondelete="SET NULL"))


class Climb(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    route_id = db.Column(db.Integer, db.ForeignKey("route.id", ondelete="CASCADE"), index=True, nullable=False)
    s_start_m = db.Column(Double, nullable=False)
    s_end_m = db.Column(Double, nullable=False)
    length_m = db.Column(Double, nullable=False)
    gain_m = db.Column(Double, nullable=False)
    avg_grade = db.Column(Float, nullable=False)
    max_grade = db.Column(Float, nullable=False)
    score = db.Column(Double, nullable=False)
    category = db.Column(db.String(3))           # 4 | 3 | 2 | 1 | HC | null
    regroup_mode = db.Column(db.String(10), nullable=False, default="off")   # off | ease_back | stop
    ease_km = db.Column(Float, nullable=False, default=2.0)


class Ride(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    route_id = db.Column(db.Integer, db.ForeignKey("route.id", ondelete="CASCADE"), unique=True, nullable=False)
    date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    tz = db.Column(db.String(40), nullable=False, default="Europe/Dublin")
    meeting_name = db.Column(db.String(120))
    meeting_lat = db.Column(Float)
    meeting_lon = db.Column(Float)
    leader_phone = db.Column(db.String(30))
    notes = db.Column(db.Text)
    share_token = db.Column(db.String(40), unique=True)   # the link anyone can view the ride sheet with; null = not shared
    sponsor_mode = db.Column(db.String(10), nullable=False, default="auto", server_default="auto")   # auto | none | campaign
    sponsor_campaign_id = db.Column(db.Integer, db.ForeignKey("sponsor_campaign.id", ondelete="SET NULL"))
    groups = db.relationship("PaceGroup", backref="ride", cascade="all, delete-orphan", order_by="PaceGroup.position")


class PaceGroup(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ride_id = db.Column(db.Integer, db.ForeignKey("ride.id", ondelete="CASCADE"), index=True, nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)
    name = db.Column(db.String(20), nullable=False)
    speed_kmh = db.Column(Float, nullable=False)
    stop_min = db.Column(db.Integer, nullable=False, default=20)


class ScanJob(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    route_id = db.Column(db.Integer, db.ForeignKey("route.id", ondelete="CASCADE"), index=True, nullable=False)
    route_version = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(12), nullable=False, default="queued")  # queued|running|done|failed
    progress = db.Column(db.Float, nullable=False, default=0.0)
    message = db.Column(db.String(300))
    params = db.Column(JSONB)
    summary = db.Column(JSONB)  # counts per category, added/updated/removed
    created_at = db.Column(db.DateTime(timezone=True), default=_now)
    finished_at = db.Column(db.DateTime(timezone=True))


class PoiTile(db.Model):
    """Cached raw Overpass elements for one 0.1 degree tile. OSM data, not Strava data."""
    id = db.Column(db.String(40), primary_key=True)
    payload = db.Column(JSONB, nullable=False)
    fetched_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)


class RouteState(db.Model):
    """One undo step: a gzip'd JSON snapshot of the route's editable state (see history.py)."""
    __table_args__ = (UniqueConstraint("route_id", "seq"),)
    id = db.Column(db.Integer, primary_key=True)
    route_id = db.Column(db.Integer, db.ForeignKey("route.id", ondelete="CASCADE"), index=True, nullable=False)
    seq = db.Column(db.Integer, nullable=False)
    label = db.Column(db.String(120), nullable=False)
    digest = db.Column(db.String(40), nullable=False)
    blob = db.Column(LargeBinary, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_now)


class GeomBlob(db.Model):
    """A route line, stored once per distinct shape and referenced from snapshots by hash."""
    __table_args__ = (UniqueConstraint("route_id", "hash"),)
    id = db.Column(db.Integer, primary_key=True)
    route_id = db.Column(db.Integer, db.ForeignKey("route.id", ondelete="CASCADE"), index=True, nullable=False)
    hash = db.Column(db.String(40), nullable=False)
    blob = db.Column(LargeBinary, nullable=False)


class User(db.Model):
    __tablename__ = "app_user"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(254), unique=True, nullable=False)
    display_name = db.Column(db.String(100))
    created_at = db.Column(db.DateTime(timezone=True), default=_now)
    last_login_at = db.Column(db.DateTime(timezone=True))
    role = db.Column(db.String(10), nullable=False, default="leader", server_default="leader")  # admin | leader


class LoginToken(db.Model):
    """One-time sign-in link. Only a hash of the token is stored."""
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(254), nullable=False)
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    used_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), default=_now)
    invite_id = db.Column(db.Integer, db.ForeignKey("invite.id", ondelete="SET NULL"))   # the code entered when asking for the link


class StravaConnection(db.Model):
    """An athlete's connection. Tokens are encrypted at rest; refresh tokens rotate on every refresh."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("app_user.id", ondelete="CASCADE"), unique=True, nullable=False)
    athlete_id = db.Column(BigInteger, unique=True, nullable=False)
    access_token_enc = db.Column(db.Text, nullable=False)
    refresh_token_enc = db.Column(db.Text, nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    scope = db.Column(db.String(200), nullable=False, default="")
    consent_version = db.Column(db.String(20), nullable=False)
    consented_at = db.Column(db.DateTime(timezone=True), nullable=False)
    connected_at = db.Column(db.DateTime(timezone=True), default=_now)


class StravaRouteCache(db.Model):
    """Strava route metadata we have fetched for one athlete. Deleted at expires_at (7 days), on disconnect or
    revocation, on account deletion, and when the route disappears from Strava."""
    __table_args__ = (UniqueConstraint("user_id", "strava_route_id"),)
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("app_user.id", ondelete="CASCADE"), index=True, nullable=False)
    strava_route_id = db.Column(BigInteger, nullable=False)
    name = db.Column(db.String(200), nullable=False)
    distance_m = db.Column(Double, nullable=False, default=0)
    elevation_gain_m = db.Column(Double, nullable=False, default=0)
    summary_polyline = db.Column(db.Text, nullable=False, default="")
    private = db.Column(db.Boolean, nullable=False, default=False)
    tidy_name = db.Column(db.String(200))        # '063 km · Naas': derived from Strava data, so it expires with the row
    fetched_at = db.Column(db.DateTime(timezone=True), nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    last_validated_at = db.Column(db.DateTime(timezone=True))


class ConsentLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("app_user.id", ondelete="CASCADE"), index=True, nullable=False)
    kind = db.Column(db.String(30), nullable=False)    # strava_connect | strava_publish | strava_disconnect
    version = db.Column(db.String(20))
    text_hash = db.Column(db.String(64))
    detail = db.Column(JSONB)
    at = db.Column(db.DateTime(timezone=True), default=_now)


class Club(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, default="")
    settings = db.Column(JSONB)          # see club.DEFAULT_SETTINGS
    created_at = db.Column(db.DateTime(timezone=True), default=_now)


class Membership(db.Model):
    """A person belongs to a club as a 'member' or a club 'admin' (who can invite and remove people)."""
    __table_args__ = (UniqueConstraint("user_id", "club_id"),)
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("app_user.id", ondelete="CASCADE"), index=True, nullable=False)
    club_id = db.Column(db.Integer, db.ForeignKey("club.id", ondelete="CASCADE"), index=True, nullable=False)
    role = db.Column(db.String(10), nullable=False, default="member", server_default="member")   # member | admin
    joined_at = db.Column(db.DateTime(timezone=True), default=_now)
    invited_by = db.Column(db.Integer, db.ForeignKey("app_user.id", ondelete="SET NULL"))


class Invite(db.Model):
    """An invite code for one club. Only a hash of the code is stored, so a code is shown once, when it is made."""
    id = db.Column(db.Integer, primary_key=True)
    club_id = db.Column(db.Integer, db.ForeignKey("club.id", ondelete="CASCADE"), index=True, nullable=False)
    code_hash = db.Column(db.String(64), unique=True, nullable=False)
    hint = db.Column(db.String(8), nullable=False, default="")      # last characters, to tell codes apart in the list
    label = db.Column(db.String(80))
    role = db.Column(db.String(10), nullable=False, default="member", server_default="member")   # what the code grants
    email = db.Column(db.String(254))                                # set: only this address can use it
    max_uses = db.Column(db.Integer)                                 # null = unlimited until it expires
    uses = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    revoked_at = db.Column(db.DateTime(timezone=True))
    created_by = db.Column(db.Integer, db.ForeignKey("app_user.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime(timezone=True), default=_now)


class County(db.Model):
    """County boundaries (Tailte Éireann, CC BY 4.0) with Met Éireann's FIPS region code.
    Named ie_county because the PostGIS image's geocoder extension already provides a table called county."""
    __tablename__ = "ie_county"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(6), index=True)        # EI12 ...
    name = db.Column(db.String(40), nullable=False)
    name_key = db.Column(db.String(40), nullable=False, index=True)
    geom = db.Column(Geometry("MULTIPOLYGON", srid=4326), nullable=False)


class Warning(db.Model):
    """A Met Éireann warning exactly as issued (CC BY 4.0 plus: do not alter times, areas, text or severity)."""
    id = db.Column(db.Integer, primary_key=True)
    cap_id = db.Column(db.String(120), unique=True, nullable=False)
    type = db.Column(db.String(40), nullable=False)
    level = db.Column(db.String(10), nullable=False)          # yellow | orange | red
    severity = db.Column(db.String(20))
    certainty = db.Column(db.String(20))
    issued = db.Column(db.DateTime(timezone=True))
    updated = db.Column(db.DateTime(timezone=True))
    onset = db.Column(db.DateTime(timezone=True), nullable=False)
    expiry = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    headline = db.Column(db.Text, nullable=False)
    description_html = db.Column(db.Text)
    regions = db.Column(ARRAY(db.String(8)), nullable=False, default=list)
    raw = db.Column(JSONB)
    fetched_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)


class WarningPoll(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now, index=True)
    ok = db.Column(db.Boolean, nullable=False)
    error = db.Column(db.String(300))


class ForecastCache(db.Model):
    """Parsed forecast steps for a ~5 km grid cell, reused for an hour."""
    key = db.Column(db.String(60), primary_key=True)
    payload = db.Column(JSONB, nullable=False)
    fetched_at = db.Column(db.DateTime(timezone=True), nullable=False)


class Closure(db.Model):
    """A road closure or works entered by a leader or the club. Always available, always has an end date."""
    id = db.Column(db.Integer, primary_key=True)
    geom = db.Column(Geometry("POINT", srid=4326), nullable=False)
    radius_m = db.Column(Float, nullable=False, default=100)
    description = db.Column(db.String(300), nullable=False)
    starts_at = db.Column(db.DateTime(timezone=True), nullable=False)
    ends_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    club_id = db.Column(db.Integer, db.ForeignKey("club.id", ondelete="CASCADE"), index=True)
    entered_by = db.Column(db.Integer, db.ForeignKey("app_user.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime(timezone=True), default=_now)


class OsmClosureCache(db.Model):
    """Roadworks / no-access ways from OpenStreetMap near a route, reused for a few hours."""
    key = db.Column(db.String(64), primary_key=True)
    payload = db.Column(JSONB, nullable=False)
    fetched_at = db.Column(db.DateTime(timezone=True), nullable=False)


class ClubStop(db.Model):
    """A stop the club has saved: favourite café, regroup spot, hazard, verified defibrillator."""
    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(30), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    note = db.Column(db.Text)
    hours = db.Column(db.String(300))                  # opening_hours string the club has confirmed
    geom = db.Column(Geometry("POINT", srid=4326), nullable=False)
    verified_by = db.Column(db.Integer, db.ForeignKey("app_user.id", ondelete="SET NULL"))
    last_verified_at = db.Column(db.DateTime(timezone=True))
    active = db.Column(db.Boolean, nullable=False, default=True)
    club_id = db.Column(db.Integer, db.ForeignKey("club.id", ondelete="CASCADE"), index=True)
    created_by = db.Column(db.Integer, db.ForeignKey("app_user.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime(timezone=True), default=_now)


# ---------------------------------------------------------------- club sponsor publishing (premium)

class ClubPremium(db.Model):
    """A club's premium entitlement. Attached to the club, never to a person. 'expired' is not stored: it is worked out
    from expires_at, so a lapsed club can never look paid. Only a platform admin (or, later, a verified billing
    webhook, see billing.py) writes this."""
    __tablename__ = "club_premium"
    id = db.Column(db.Integer, primary_key=True)
    club_id = db.Column(db.Integer, db.ForeignKey("club.id", ondelete="CASCADE"), unique=True, nullable=False)
    plan = db.Column(db.String(30), nullable=False, default="club_sponsor_annual")
    status = db.Column(db.String(12), nullable=False, default="active")          # active | cancelled
    price_cents = db.Column(db.Integer, nullable=False, default=7999)
    currency = db.Column(db.String(3), nullable=False, default="EUR")
    starts_at = db.Column(db.DateTime(timezone=True), nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    source = db.Column(db.String(20), nullable=False, default="manual")         # manual | billing
    external_ref = db.Column(db.String(120))                                     # invoice or billing reference
    note = db.Column(db.String(300))
    activated_by = db.Column(db.Integer, db.ForeignKey("app_user.id", ondelete="SET NULL"))
    cancelled_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), default=_now)
    updated_at = db.Column(db.DateTime(timezone=True), default=_now, onupdate=_now)


class Sponsor(db.Model):
    """A local business or supporter a club lists. Archived, never deleted, so history keeps its meaning."""
    id = db.Column(db.Integer, primary_key=True)
    club_id = db.Column(db.Integer, db.ForeignKey("club.id", ondelete="CASCADE"), index=True, nullable=False)
    name = db.Column(db.String(80), nullable=False)
    website = db.Column(db.String(300))
    description = db.Column(db.String(200))
    logo_blob = db.Column(LargeBinary)                         # re-encoded by us; never the uploaded bytes
    logo_mime = db.Column(db.String(20))
    logo_token = db.Column(db.String(32), unique=True)         # opaque public name of the logo; changes when the logo does
    archived_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), default=_now)


class SponsorCampaign(db.Model):
    """What a sponsor says, where, and when. status is the club's decision; whether it is scheduled, live or expired
    is worked out from the dates in the club's timezone (see sponsors.campaign_phase)."""
    __tablename__ = "sponsor_campaign"
    id = db.Column(db.Integer, primary_key=True)
    club_id = db.Column(db.Integer, db.ForeignKey("club.id", ondelete="CASCADE"), index=True, nullable=False)
    sponsor_id = db.Column(db.Integer, db.ForeignKey("sponsor.id"), nullable=False)
    name = db.Column(db.String(80), nullable=False)             # internal
    headline = db.Column(db.String(60), nullable=False)
    body = db.Column(db.String(140))
    cta_label = db.Column(db.String(24))
    cta_url = db.Column(db.String(500))
    starts_on = db.Column(db.Date, nullable=False)              # club-local dates, both ends inclusive
    ends_on = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(10), nullable=False, default="draft")   # draft | published | paused | archived
    placements = db.Column(JSONB, nullable=False, default=list)         # ride_sheet | print | widget
    route_id = db.Column(db.Integer, db.ForeignKey("route.id", ondelete="SET NULL"))  # null = club-wide
    display_order = db.Column(db.Integer, nullable=False, default=0)
    is_default = db.Column(db.Boolean, nullable=False, default=False)
    value_cents = db.Column(db.Integer)                         # what the sponsor pays the club; admins only, never public
    click_token = db.Column(db.String(32), unique=True, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("app_user.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime(timezone=True), default=_now)
    updated_at = db.Column(db.DateTime(timezone=True), default=_now, onupdate=_now)


class SponsorDaily(db.Model):
    """Counts per day, and nothing else: no row per visitor, no addresses, no cookies. campaign_id 0 is the club-level
    counter (all requests, with or without a panel). Kinds: sheet_request, widget_request, delivery, click."""
    __tablename__ = "sponsor_daily"
    __table_args__ = (UniqueConstraint("day", "club_id", "campaign_id", "placement", "kind"),)
    id = db.Column(db.Integer, primary_key=True)
    day = db.Column(db.Date, nullable=False, index=True)        # in the club's timezone
    club_id = db.Column(db.Integer, db.ForeignKey("club.id", ondelete="CASCADE"), index=True, nullable=False)
    campaign_id = db.Column(db.Integer, nullable=False, default=0)
    placement = db.Column(db.String(12), nullable=False)
    kind = db.Column(db.String(16), nullable=False)
    count = db.Column(db.Integer, nullable=False, default=0)


class ClubEmbed(db.Model):
    """The opaque name of a club's public sponsor widget. Revoking it stops the widget; a new one replaces it."""
    __tablename__ = "club_embed"
    id = db.Column(db.Integer, primary_key=True)
    club_id = db.Column(db.Integer, db.ForeignKey("club.id", ondelete="CASCADE"), index=True, nullable=False)
    token = db.Column(db.String(32), unique=True, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_now)
    created_by = db.Column(db.Integer, db.ForeignKey("app_user.id", ondelete="SET NULL"))
    revoked_at = db.Column(db.DateTime(timezone=True))


class ClubBilling(db.Model):
    """What we know of a club's card billing (Stripe). The premium entitlement itself is ClubPremium; this only links the
    club to its Stripe customer and subscription and mirrors their state for the Billing page."""
    __tablename__ = "club_billing"
    id = db.Column(db.Integer, primary_key=True)
    club_id = db.Column(db.Integer, db.ForeignKey("club.id", ondelete="CASCADE"), unique=True, nullable=False)
    customer_id = db.Column(db.String(64), unique=True)
    subscription_id = db.Column(db.String(64))
    sub_status = db.Column(db.String(24))                      # active | trialing | past_due | canceled | unpaid | incomplete ...
    cancel_at_period_end = db.Column(db.Boolean, nullable=False, default=False)
    current_period_end = db.Column(db.DateTime(timezone=True))
    last_payment_failed_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), default=_now)
    updated_at = db.Column(db.DateTime(timezone=True), default=_now, onupdate=_now)


class BillingInvoice(db.Model):
    """A copy of a Stripe invoice's headline facts and its download links, kept so the club can always find its invoices
    and receipts. The documents themselves live at Stripe; we store only the links."""
    __tablename__ = "billing_invoice"
    id = db.Column(db.Integer, primary_key=True)
    club_id = db.Column(db.Integer, db.ForeignKey("club.id", ondelete="CASCADE"), index=True, nullable=False)
    stripe_invoice_id = db.Column(db.String(64), unique=True, nullable=False)
    number = db.Column(db.String(64))
    status = db.Column(db.String(16))                          # draft | open | paid | void | uncollectible
    amount_due = db.Column(db.Integer, nullable=False, default=0)
    amount_paid = db.Column(db.Integer, nullable=False, default=0)
    tax = db.Column(db.Integer)
    amount_refunded = db.Column(db.Integer, nullable=False, default=0)
    currency = db.Column(db.String(3), nullable=False, default="EUR")
    description = db.Column(db.String(200))
    period_start = db.Column(db.DateTime(timezone=True))
    period_end = db.Column(db.DateTime(timezone=True))
    hosted_url = db.Column(db.String(500))
    pdf_url = db.Column(db.String(500))
    receipt_url = db.Column(db.String(500))
    issued_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), default=_now)
    updated_at = db.Column(db.DateTime(timezone=True), default=_now, onupdate=_now)


class BillingEvent(db.Model):
    """Stripe events already applied, so a repeated delivery changes nothing."""
    __tablename__ = "billing_event"
    event_id = db.Column(db.String(80), primary_key=True)
    type = db.Column(db.String(80), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_now)
