"""Met Éireann weather warnings: fetch, store exactly as issued, decide which matter to a ride, present them.

Licence conditions (CC BY 4.0 plus Met Éireann's own): do not alter a warning's issue, onset or expiry times, area,
description, severity, certainty, event or headline; keep warnings up to date and remove expired ones; acknowledge
Met Éireann. So fields are stored verbatim in their own columns (with the raw feed item beside them), display
formatting only converts a time to local time and strips markup that isn't text, and a feed that can't be reached
produces "couldn't check", never a quiet "no warnings".
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Protocol
from zoneinfo import ZoneInfo

import requests
from xml.etree import ElementTree

from . import counties, db
from .models import Warning, WarningPoll

log = logging.getLogger("rlk.warnings")

FEED_URL = "https://www.met.ie/Open_Data/json/warning_IRELAND.json"
WARNINGS_PAGE = "https://www.met.ie/warnings"
RSS_URL = "https://www.met.ie/warningsxml/rss.xml"
MARINE_TITLES = ("small craft", "gale", "storm", "marine", "sea area")   # not land warnings; nothing to show a cyclist
ATTRIBUTION = "Weather warnings: Met Éireann (met.ie), CC BY 4.0. Shown as issued."
STALE_AFTER = timedelta(minutes=30)       # a feed we haven't heard from for this long is "couldn't check"
REFRESH_AFTER = timedelta(minutes=12)     # polled in the background every 10-15 min; this tops up on demand
LOCAL = ZoneInfo("Europe/Dublin")
UNREACHABLE_TEXT = "Couldn't check Met Éireann weather warnings. Check met.ie/warnings before you ride."

LEVELS = {  # level -> (title as Met Éireann describes it, instruction, rank)
    "yellow": ("Weather Alert", "Be Aware", 1),
    "orange": ("Weather Warning", "Be Prepared", 2),
    "red": ("Severe Weather Warning", "Take Action", 3),
}


class WarningsError(RuntimeError):
    pass


class WarningsProvider(Protocol):
    def fetch(self) -> list[dict]: ...


class MetEireannWarnings:
    def __init__(self, url: str = FEED_URL, session=None, timeout: float = 8):
        self.url, self.session, self.timeout = url, session or requests.Session(), timeout

    def fetch(self) -> list[dict]:
        try:
            r = self.session.get(self.url, timeout=self.timeout)
            if r.status_code == 404 and self.url == FEED_URL:
                return self._empty_if_rss_has_only_marine()
            if r.status_code != 200:
                raise WarningsError(f"Met Éireann answered {r.status_code}")
            data = r.json()
        except requests.RequestException as e:
            raise WarningsError(f"couldn't reach Met Éireann: {e}") from e
        except ValueError as e:
            raise WarningsError("Met Éireann sent something that isn't JSON") from e
        if not isinstance(data, list):
            raise WarningsError("Met Éireann's feed wasn't a list of warnings")
        return data

    def _empty_if_rss_has_only_marine(self) -> list[dict]:
        """The JSON file has been seen missing (404). Met Éireann's RSS index is a second view of the same warnings:
        if it answers and lists nothing but marine items there are no land warnings. Anything else stays "couldn't check"
        (the RSS only links CAP files, which we don't geolocate), so a missing file is never silently read as "all clear"."""
        try:
            r = self.session.get(RSS_URL, timeout=self.timeout)
            if r.status_code != 200:
                raise WarningsError(f"Met Éireann answered 404 (and its RSS index {r.status_code})")
            root = ElementTree.fromstring(r.content)
        except requests.RequestException as e:
            raise WarningsError(f"couldn't reach Met Éireann: {e}") from e
        except ElementTree.ParseError as e:
            raise WarningsError("Met Éireann answered 404 and its RSS index wasn't readable") from e
        titles = [(i.findtext("title") or "").strip().lower() for i in root.iter("item")]
        if all(any(m in t for m in MARINE_TITLES) for t in titles):
            return []
        raise WarningsError("Met Éireann's warnings file is missing and the RSS index lists land warnings we can't place")


def _dt(s) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=LOCAL)


def get_provider(config) -> WarningsProvider:
    return config.get("WARNINGS_CLIENT") or MetEireannWarnings()


# ------------------------------------------------------------------ polling

def poll(provider: WarningsProvider, now: datetime | None = None) -> dict:
    """Fetch the feed and make our table match it: new/changed warnings stored as issued, anything no longer in the
    feed or past its expiry removed. A failed fetch is recorded and leaves what we had untouched."""
    now = now or datetime.now(timezone.utc)
    try:
        items = provider.fetch()
    except WarningsError as e:
        db.session.add(WarningPoll(at=now, ok=False, error=str(e)[:300]))
        db.session.commit()
        log.warning("warnings poll failed: %s", e)
        return {"ok": False, "error": str(e)}
    seen, kept = set(), 0
    for it in items:
        try:
            cap_id, level = str(it["capId"]), str(it["level"]).lower()
            onset, expiry = _dt(it["onset"]), _dt(it["expiry"])
            headline = str(it["headline"])
        except (KeyError, TypeError, ValueError) as e:
            log.warning("skipping malformed warning %r: %s", it.get("id") if isinstance(it, dict) else it, e)
            continue
        if level not in LEVELS or onset is None or expiry is None:
            continue
        seen.add(cap_id)
        w = Warning.query.filter_by(cap_id=cap_id).first() or Warning(cap_id=cap_id)
        w.type, w.level, w.severity, w.certainty = str(it.get("type", "")), level, it.get("severity"), it.get("certainty")
        w.issued, w.updated, w.onset, w.expiry = _dt(it.get("issued")), _dt(it.get("updated")), onset, expiry
        w.headline, w.description_html = headline, it.get("description")
        w.regions, w.raw, w.fetched_at = [str(r) for r in (it.get("regions") or [])], it, now
        db.session.add(w)
        kept += 1
    removed = 0
    for w in Warning.query.all():
        if w.cap_id not in seen or w.expiry <= now:
            db.session.delete(w)
            removed += 1
    db.session.add(WarningPoll(at=now, ok=True))
    db.session.commit()
    return {"ok": True, "stored": kept, "removed": removed}


def purge_expired(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    n = Warning.query.filter(Warning.expiry <= now).delete()
    db.session.commit()
    return n


def feed_status(now: datetime | None = None) -> dict:
    """Is what we show trustworthy? Only if the latest poll succeeded and is recent."""
    now = now or datetime.now(timezone.utc)
    last = WarningPoll.query.order_by(WarningPoll.at.desc()).first()
    last_ok = WarningPoll.query.filter_by(ok=True).order_by(WarningPoll.at.desc()).first()
    ok = bool(last and last.ok and now - last.at < STALE_AFTER)
    return {"ok": ok, "checked_at": last_ok.at if last_ok else None,
            "message": None if ok else UNREACHABLE_TEXT}


def ensure_fresh(provider: WarningsProvider, now: datetime | None = None) -> None:
    """Top up on demand if the background poll hasn't run lately (first visit, scheduler down)."""
    now = now or datetime.now(timezone.utc)
    last = WarningPoll.query.order_by(WarningPoll.at.desc()).first()
    if last is None or not last.ok or now - last.at >= REFRESH_AFTER:
        poll(provider, now)


# ------------------------------------------------------------------ relevance

def headline_names(headline: str) -> set[str]:
    """County names in 'Status Yellow - Rain warning for Galway, Leitrim and Mayo' (as name keys)."""
    m = re.search(r"\bfor\s+(.+)$", headline or "", flags=re.I)
    if not m:
        return set()
    return {counties.name_key(p) for p in re.split(r",|\band\b", m.group(1)) if counties.name_key(p)}


def headline_display_names(headline: str) -> list[str]:
    """The same county names, as written in the headline and in its order, for showing to people."""
    m = re.search(r"\bfor\s+(.+)$", headline or "", flags=re.I)
    if not m:
        return []
    return [p.strip() for p in re.split(r",|\band\b", m.group(1)) if p.strip()]


def affects_counties(w: Warning, route_counties: list[dict]) -> bool:
    """True if the warning names any county the route passes through, by region code OR by name in the headline
    (see counties.py: a wrong code can then only add a warning, never hide one)."""
    codes = {c["code"] for c in route_counties if c["code"]}
    names = {c["name_key"] for c in route_counties}
    return bool(set(w.regions) & codes) or bool(headline_names(w.headline) & names)


def overlaps(w: Warning, start: datetime, end: datetime, buffer_h: float) -> bool:
    buf = timedelta(hours=buffer_h)
    return w.onset <= end + buf and w.expiry >= start - buf


def relevant(route_counties: list[dict], start: datetime, end: datetime, buffer_h: float = 2.0,
             now: datetime | None = None) -> list[Warning]:
    now = now or datetime.now(timezone.utc)
    out = [w for w in Warning.query.all()
           if w.expiry > now and affects_counties(w, route_counties) and overlaps(w, start, end, buffer_h)]
    return sorted(out, key=lambda w: (-LEVELS[w.level][2], w.onset))


def affected_sections(w: Warning, route_counties: list[dict]) -> list[list[float]]:
    """Stretches of the route inside counties the warning covers (metres along the route)."""
    codes, names = set(w.regions), headline_names(w.headline)
    out = []
    for c in route_counties:
        if (c["code"] in codes) or (c["name_key"] in names):
            out += c["sections"]
    return sorted(out)


# ------------------------------------------------------------------ presentation

class _Clean(HTMLParser):
    """Keeps the text exactly; keeps only simple structural tags; drops attributes and anything else."""
    ALLOWED = {"p", "ul", "ol", "li", "br", "strong", "b", "em", "i"}

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.out = []

    def handle_starttag(self, tag, attrs):
        if tag in self.ALLOWED:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if tag in self.ALLOWED and tag != "br":
            self.out.append(f"</{tag}>")

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self.out.append("<br>")

    def handle_data(self, data):
        self.out.append(data.replace("<", "&lt;").replace(">", "&gt;"))

    def handle_entityref(self, name):
        self.out.append(f"&{name};")

    def handle_charref(self, name):
        self.out.append(f"&#{name};")


def clean_html(html: str | None) -> str:
    p = _Clean()
    p.feed(html or "")
    p.close()
    return "".join(p.out)


def _local(dt: datetime | None) -> str | None:
    return dt.astimezone(LOCAL).strftime("%a %d %b %H:%M") if dt else None


def present(w: Warning, route_counties: list[dict]) -> dict:
    title, instruction, rank = LEVELS[w.level]
    return {"id": w.cap_id, "level": w.level, "level_title": title, "instruction": instruction, "rank": rank,
            "type": w.type, "headline": w.headline, "counties": headline_display_names(w.headline),
            "regions": list(w.regions), "severity": w.severity, "certainty": w.certainty,
            "issued": _local(w.issued), "valid_from": _local(w.onset), "valid_to": _local(w.expiry),
            "valid_from_iso": w.onset.isoformat(), "valid_to_iso": w.expiry.isoformat(),
            "description_html": clean_html(w.description_html), "url": WARNINGS_PAGE,
            "sections": affected_sections(w, route_counties)}


def banner(route_counties: list[dict], start: datetime, end: datetime, buffer_h: float = 2.0, now=None) -> dict:
    """Everything the warnings banner needs for one ride window."""
    status = feed_status(now)
    warns = relevant(route_counties, start, end, buffer_h, now) if status["ok"] or Warning.query.count() else []
    shown = [present(w, route_counties) for w in warns]
    top = max((p["rank"] for p in shown), default=0)
    return {"status": "ok" if status["ok"] else "unreachable", "message": status["message"],
            "checked_at": status["checked_at"].isoformat() if status["checked_at"] else None,
            "warnings": shown, "top_level": next((k for k, v in LEVELS.items() if v[2] == top), None),
            "postpone_prompt": top >= 2, "attribution": ATTRIBUTION, "url": WARNINGS_PAGE}


def day_level(route_counties: list[dict], start: datetime, end: datetime, buffer_h: float, now=None) -> str | None:
    """Highest warning level overlapping a day's ride window, for the dot in the day picker."""
    warns = relevant(route_counties, start, end, buffer_h, now)
    return warns[0].level if warns else None
