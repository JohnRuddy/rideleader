"""Club sponsor publishing: who is entitled, what a campaign is, which one a ride sheet shows, how it is counted.

The rules in one place, so they can be read and tested:

* Premium belongs to the club. 'Expired' is worked out from the dates, never stored, so a lapsed club cannot look paid.
* A campaign is served only if: the club's premium is serving, the club approved it (status published), its sponsor is not
  archived, today (in the club's timezone) is inside its dates, and the placement is switched on for it.
* Which campaign: the one chosen for this ride, else one assigned to this route (or earlier runs of it), else the club's
  default or first club-wide one, else nothing. With rotation switched on the choice rotates, but is fixed for a ride for
  a week (and for the website widget for a day), so it never changes while someone reads.
* Nothing here touches route checks, warnings or stop rankings: it runs after those are built and only adds a panel.
* Counting keeps daily totals only. No visitor rows, addresses, cookies or fingerprints.
"""
import hashlib
import io
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from ipaddress import ip_address
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app
from sqlalchemy.dialects.postgresql import insert as pg_insert

from . import club as clubmod
from . import db
from .models import ClubEmbed, ClubPremium, Sponsor, SponsorCampaign, SponsorDaily

PRICE_CENTS = 7999
PRICE_LABEL = "€79.99"
PRICE_LINE = f"{PRICE_LABEL} per club per year"
VALUE_STATEMENT = ("Give your club sponsors a place in every ride briefing. Manage campaigns once, publish across your ride "
                   f"sheets and website, and share clear engagement reports. {PRICE_LINE}.")
EMPTY_STATE = "Add your club sponsor once and publish it across your ride sheets and website."
DEFAULT_TZ = "Europe/Dublin"

LIMITS = {"name": 80, "headline": 60, "body": 140, "cta": 24, "description": 200, "url": 500}
PLACEMENTS = ("ride_sheet", "print", "widget")
PLACEMENT_LABELS = {"ride_sheet": "Ride sheet", "print": "Printed sheet (acknowledgement line)", "widget": "Club website"}
COUNT_PLACEMENT = {"ride_sheet": "ride_sheet", "widget": "widget"}
STATUSES = ("draft", "published", "paused", "archived")
MAX_CAMPAIGN_DAYS = 400
LOGO_MAX_UPLOAD = 1_500_000
LOGO_MAX_SIDE = 4000
LOGO_BOX = (480, 240)
LOGO_MAX_STORED = 300_000


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_token() -> str:
    return secrets.token_urlsafe(16)


# ------------------------------------------------------------------ the club's clock

def club_timezone(club_id: int | None) -> ZoneInfo:
    name = clubmod.settings(club_id).get("timezone") or DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_TZ)


def club_today(club_id: int | None, now: datetime | None = None) -> date:
    return (now or utcnow()).astimezone(club_timezone(club_id)).date()


# ------------------------------------------------------------------ premium

def premium_state(club_id: int, now: datetime | None = None) -> dict:
    """What a club's premium is right now. 'serving' is the only thing that decides whether sponsor panels are shown."""
    now = now or utcnow()
    p = ClubPremium.query.filter_by(club_id=club_id).first()
    base = {"state": "none", "serving": False, "expires_at": None, "days_left": None, "in_grace": False, "price": PRICE_LINE,
            "source": None, "note": None, "ref": None}
    if p is None:
        return base
    grace = timedelta(days=int(current_app.config.get("PREMIUM_GRACE_DAYS", 0)))
    base.update(expires_at=p.expires_at, source=p.source, note=p.note, ref=p.external_ref)
    if p.status == "cancelled":
        return {**base, "state": "cancelled"}
    if now < p.starts_at:
        return {**base, "state": "pending"}
    if now < p.expires_at:
        return {**base, "state": "active", "serving": True, "days_left": (p.expires_at - now).days}
    if now < p.expires_at + grace:
        return {**base, "state": "active", "serving": True, "in_grace": True, "days_left": 0}
    return {**base, "state": "expired"}


STATE_TEXT = {
    "none": "Not active. Club Sponsor Publishing hasn't been switched on for this club.",
    "active": "Active.",
    "pending": "Not started yet.",
    "expired": "Expired. Your sponsors, campaigns and reports are kept, but sponsor panels are not being shown.",
    "cancelled": "Cancelled. Your sponsors, campaigns and reports are kept, but sponsor panels are not being shown.",
}


# ------------------------------------------------------------------ campaigns

def campaign_phase(c: SponsorCampaign, today: date) -> str:
    """draft, scheduled, active, paused, expired or archived. The club decides status; the dates decide the rest."""
    if c.status == "archived":
        return "archived"
    if c.status == "draft":
        return "draft"
    if c.status == "paused":
        return "paused"
    if today < c.starts_on:
        return "scheduled"
    if today > c.ends_on:
        return "expired"
    return "active"


PHASE_TEXT = {
    "draft": "Draft: not shown anywhere yet.",
    "scheduled": "Scheduled: will start on its first day.",
    "active": "Active: being shown.",
    "paused": "Paused: not shown until you resume it.",
    "expired": "Expired: its last day has passed, so it is no longer shown.",
    "archived": "Archived: kept for reports, never shown.",
}


def _live(club_id: int, placement: str, now: datetime) -> list[SponsorCampaign]:
    if not premium_state(club_id, now)["serving"]:
        return []
    today = club_today(club_id, now)
    rows = (SponsorCampaign.query.join(Sponsor, Sponsor.id == SponsorCampaign.sponsor_id)
            .filter(SponsorCampaign.club_id == club_id, SponsorCampaign.status == "published", Sponsor.archived_at.is_(None))
            .order_by(SponsorCampaign.display_order, SponsorCampaign.id).all())
    return [c for c in rows if campaign_phase(c, today) == "active" and placement in (c.placements or [])]


def _stable_index(key: str, n: int) -> int:
    return int(hashlib.sha256(key.encode()).hexdigest(), 16) % n


def select_campaign(club_id: int | None, placement: str, *, route=None, ride=None, now: datetime | None = None,
                    rotation_key: str | None = None) -> SponsorCampaign | None:
    """The one campaign this placement shows, or None (and then no panel at all)."""
    if club_id is None:
        return None
    now = now or utcnow()
    live = _live(club_id, placement, now)
    if not live:
        return None
    if ride is not None:
        if ride.sponsor_mode == "none":
            return None                                    # the club published this ride without a sponsor panel
        if ride.sponsor_mode == "campaign" and ride.sponsor_campaign_id:
            chosen = next((c for c in live if c.id == ride.sponsor_campaign_id), None)
            if chosen:
                return chosen                              # (if that one has ended we fall through to the automatic rules)
    pool = []
    if route is not None:
        ids = {route.id, route.series_root_id} - {None}
        pool = [c for c in live if c.route_id in ids]
    if not pool:
        pool = [c for c in live if c.route_id is None]
    if not pool:
        return None
    if len(pool) > 1 and clubmod.settings(club_id).get("sponsor_rotation") and rotation_key:
        return pool[_stable_index(rotation_key, len(pool))]
    return next((c for c in pool if c.is_default), pool[0])


def sheet_rotation_key(route, now: datetime | None = None) -> str:
    today = club_today(route.club_id, now)
    y, w, _ = today.isocalendar()
    return f"sheet:{route.id}:{y}-{w}"                      # fixed for a ride for a week


def widget_rotation_key(token: str, club_id: int, now: datetime | None = None) -> str:
    return f"widget:{token}:{club_today(club_id, now)}"      # fixed for the widget for a day


def panel_for_sheet(route, ride, *, now: datetime | None = None, preview: bool = False) -> dict | None:
    """The sponsor panel for a ride sheet, as plain data for the template; None when there is nothing to show.
    The club comes from the route record, never from anything the browser sent."""
    if route.club_id is None:
        return None
    c = select_campaign(route.club_id, "ride_sheet", route=route, ride=ride, now=now, rotation_key=sheet_rotation_key(route, now))
    if c is None:
        return None
    return panel_data(c, "preview" if preview else "sheet", print_line="print" in (c.placements or []))


def panel_data(c: SponsorCampaign, where: str, *, print_line: bool = False) -> dict:
    sp = db.session.get(Sponsor, c.sponsor_id)
    return {"campaign_id": c.id, "sponsor_name": sp.name, "logo_token": sp.logo_token if sp.logo_blob else None,
            "headline": c.headline, "body": c.body, "cta_label": c.cta_label if (c.cta_url and c.cta_label) else None,
            "click_token": c.click_token, "where": where, "ends_on": c.ends_on if (c.body or c.cta_url) else None,
            "print_line": print_line, "label": "Club sponsor"}


# ------------------------------------------------------------------ the website widget

def active_embed(club_id: int) -> ClubEmbed | None:
    return ClubEmbed.query.filter_by(club_id=club_id, revoked_at=None).order_by(ClubEmbed.id.desc()).first()


def embed_by_token(token: str) -> ClubEmbed | None:
    if not token or len(token) > 40:
        return None
    e = ClubEmbed.query.filter_by(token=token).first()
    return e if e and e.revoked_at is None else None


def regenerate_embed(club_id: int, user_id: int | None) -> ClubEmbed:
    """Revokes the old embed name (old widgets stop) and makes a new one."""
    revoke_embed(club_id)
    e = ClubEmbed(club_id=club_id, token=new_token(), created_by=user_id)
    db.session.add(e)
    db.session.flush()
    return e


def revoke_embed(club_id: int) -> None:
    for e in ClubEmbed.query.filter_by(club_id=club_id, revoked_at=None).all():
        e.revoked_at = utcnow()


# ------------------------------------------------------------------ counting

AUTOMATED_UA = re.compile(
    r"bot|crawl|spider|slurp|preview|fetch|monitor|uptime|curl|wget|python|java/|go-http|okhttp|libwww|headless|lighthouse|"
    r"facebookexternalhit|whatsapp|telegram|slackbot|discord|twitterbot|linkedin|skypeuri|googleimageproxy|bingpreview", re.I)


def looks_automated(req) -> bool:
    """Obvious bots, link-preview fetchers and browser prefetches. Not perfect, and the reports say so."""
    if req.method == "HEAD":
        return True
    ua = req.headers.get("User-Agent", "")
    if not ua or AUTOMATED_UA.search(ua):
        return True
    purpose = " ".join(req.headers.get(h, "") for h in ("Purpose", "Sec-Purpose", "X-Purpose", "X-Moz", "X-Requested-With")).lower()
    return any(w in purpose for w in ("prefetch", "prerender", "preview"))


def count(club_id: int, campaign_id: int, placement: str, kind: str, now: datetime | None = None) -> None:
    """Add one to today's total for this club/campaign/placement/kind. Totals only: nothing identifies a visitor."""
    day = club_today(club_id, now)
    stmt = pg_insert(SponsorDaily).values(day=day, club_id=club_id, campaign_id=campaign_id, placement=placement, kind=kind, count=1)
    stmt = stmt.on_conflict_do_update(index_elements=["day", "club_id", "campaign_id", "placement", "kind"],
                                      set_={"count": SponsorDaily.count + 1})
    db.session.execute(stmt)
    db.session.commit()


def count_sheet_view(club_id: int, panel: dict | None, now: datetime | None = None) -> None:
    count(club_id, 0, "ride_sheet", "sheet_request", now)
    if panel:
        count(club_id, panel["campaign_id"], "ride_sheet", "delivery", now)


def count_widget_view(club_id: int, panel: dict | None, now: datetime | None = None) -> None:
    count(club_id, 0, "widget", "widget_request", now)
    if panel:
        count(club_id, panel["campaign_id"], "widget", "delivery", now)


def report(club_id: int, start: date, end: date) -> dict:
    """Totals for a club between two dates (club-local, inclusive), grouped for the report page and the CSV."""
    rows = (SponsorDaily.query.filter(SponsorDaily.club_id == club_id, SponsorDaily.day >= start, SponsorDaily.day <= end)
            .order_by(SponsorDaily.day, SponsorDaily.campaign_id).all())
    club_level = {"sheet_request": 0, "widget_request": 0}
    per = {}
    for r in rows:
        if r.campaign_id == 0:
            club_level[r.kind] = club_level.get(r.kind, 0) + r.count
            continue
        key = (r.campaign_id, r.placement)
        per.setdefault(key, {"delivery": 0, "click": 0})
        per[key][r.kind] = per[key].get(r.kind, 0) + r.count
    camps = {c.id: c for c in SponsorCampaign.query.filter(SponsorCampaign.club_id == club_id,
                                                           SponsorCampaign.id.in_({k[0] for k in per} or {0})).all()}
    sponsors = {s.id: s for s in Sponsor.query.filter(Sponsor.club_id == club_id).all()}
    lines = []
    for (cid, placement), v in sorted(per.items()):
        c = camps.get(cid)
        if c is None:
            continue
        lines.append({"campaign_id": cid, "campaign": c.name, "sponsor": sponsors[c.sponsor_id].name if c.sponsor_id in sponsors else "",
                      "placement": placement, "deliveries": v.get("delivery", 0), "clicks": v.get("click", 0)})
    return {"start": start, "end": end, "sheet_requests": club_level.get("sheet_request", 0),
            "widget_requests": club_level.get("widget_request", 0), "lines": lines, "raw": rows}


# ------------------------------------------------------------------ what clubs may type

def clean_text(value, limit: int, label: str, *, required: bool = False) -> str | None:
    """Plain text only: tidy whitespace, refuse markup and control characters, enforce the length."""
    v = " ".join(str(value or "").split())
    if not v:
        if required:
            raise ValueError(f"{label} is required.")
        return None
    if re.search(r"[<>]", v) or re.search(r"[\x00-\x1f\x7f]", v) or re.search(r"javascript\s*:", v, re.I):
        raise ValueError(f"{label} can only be plain text: no HTML, scripts or tags.")
    if len(v) > limit:
        raise ValueError(f"{label} can be up to {limit} characters (this is {len(v)}).")
    return v


def clean_url(value, label: str, *, required: bool = False) -> str | None:
    """An https address on a real public hostname. Refuses other schemes, passwords in the address, IP addresses and
    local names, so a campaign can't point riders at something odd or internal."""
    v = str(value or "").strip()
    if not v:
        if required:
            raise ValueError(f"{label} is required.")
        return None
    if len(v) > LIMITS["url"] or re.search(r"[\s<>\"'\\]", v):
        raise ValueError(f"{label} isn't a valid web address.")
    u = urlsplit(v)
    if u.scheme != "https":
        raise ValueError(f"{label} must start with https:// (a secure address).")
    host = (u.hostname or "").lower()
    if not host or u.username or u.password:
        raise ValueError(f"{label} isn't a valid web address.")
    try:
        ip_address(host)
        raise ValueError(f"{label} must use a website name, not a number.")
    except ValueError as e:
        if "website name" in str(e):
            raise
    if "." not in host or host.endswith((".local", ".internal", ".localhost", ".lan", ".home", ".test")) or host == "localhost":
        raise ValueError(f"{label} must be a public website.")
    if u.port not in (None, 443):
        raise ValueError(f"{label} must use the normal secure port.")
    return v


def clean_dates(starts, ends) -> tuple[date, date]:
    try:
        a, b = date.fromisoformat(str(starts)), date.fromisoformat(str(ends))
    except ValueError:
        raise ValueError("Pick a start and an end date.")
    if b < a:
        raise ValueError("The end date can't be before the start date.")
    if (b - a).days > MAX_CAMPAIGN_DAYS:
        raise ValueError(f"A campaign can run for up to {MAX_CAMPAIGN_DAYS} days. Make another one after it.")
    return a, b


def clean_value(value) -> int | None:
    v = str(value or "").strip().replace(",", ".").replace("€", "")
    if not v:
        return None
    try:
        cents = round(float(v) * 100)
    except ValueError:
        raise ValueError("Enter the sponsorship value as a number, for example 250.")
    if not 0 <= cents <= 100_000_000:
        raise ValueError("That sponsorship value isn't sensible.")
    return cents


def process_logo(storage) -> tuple[bytes, str]:
    """Check an uploaded logo and re-encode it. Only PNG, JPEG and WebP; never SVG (it can carry scripts). We keep our own
    re-encoded copy, so nothing from the uploaded file (metadata, extra data) is ever served."""
    from PIL import Image, UnidentifiedImageError
    raw = storage.read(LOGO_MAX_UPLOAD + 1)
    if not raw:
        raise ValueError("Choose a logo file.")
    if len(raw) > LOGO_MAX_UPLOAD:
        raise ValueError("That logo file is too big. Keep it under 1.5 MB.")
    try:
        probe = Image.open(io.BytesIO(raw))
        fmt = probe.format
        probe.verify()
        im = Image.open(io.BytesIO(raw))
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise ValueError("That doesn't look like a picture. Use a PNG, JPEG or WebP logo.")
    if fmt not in ("PNG", "JPEG", "WEBP"):
        raise ValueError("Use a PNG, JPEG or WebP logo.")
    if max(im.size) > LOGO_MAX_SIDE or min(im.size) < 24:
        raise ValueError(f"The logo should be between 24 and {LOGO_MAX_SIDE} pixels on each side.")
    im.load()
    has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    im = im.convert("RGBA" if has_alpha else "RGB")
    im.thumbnail(LOGO_BOX)
    out = io.BytesIO()
    im.save(out, "PNG", optimize=True)
    data, mime = out.getvalue(), "image/png"
    if len(data) > LOGO_MAX_STORED:
        out = io.BytesIO()
        im.save(out, "WEBP", quality=82)
        data, mime = out.getvalue(), "image/webp"
    if len(data) > LOGO_MAX_STORED:
        raise ValueError("That logo is too detailed. Try a simpler or smaller version.")
    return data, mime
