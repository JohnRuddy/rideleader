"""Opening-hours evaluation for stops: ETA per pace group -> open / closes soon / closed / unknown.

Parsing is done by opening_hours.js in the `hours` sidecar (handles Irish public holidays and seasonal
rules). Classification and club overrides live here so they are plain, testable Python.
"""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from .pace import Group, eta

CLOSES_SOON = timedelta(minutes=30)

OPEN, CLOSES_SOON_S, CLOSED, CLOSED_TODAY, UNKNOWN = "open", "closes_soon", "closed", "closed_today", "unknown"
LABELS = {
    OPEN: "Open on arrival",
    CLOSES_SOON_S: "Closes within 30 min of arrival",
    CLOSED: "Closed on arrival",
    CLOSED_TODAY: "Closed that day",
    UNKNOWN: "Hours unknown",
}


class HoursUnavailable(RuntimeError):
    pass


class HoursClient:
    def __init__(self, url: str, session=None, timeout: float = 10):
        self.url, self.session, self.timeout = url.rstrip("/"), session or requests.Session(), timeout

    def evaluate(self, items: list[dict]) -> list[dict]:
        if not items:
            return []
        try:
            r = self.session.post(f"{self.url}/evaluate", json={"items": items}, timeout=self.timeout)
            r.raise_for_status()
            return r.json()["results"]
        except (requests.RequestException, KeyError, ValueError) as e:
            raise HoursUnavailable(str(e)) from e


def effective_hours(w) -> tuple[str | None, str | None, bool]:
    """(rules, source, osm_changed_since_club) for a waypoint.

    The club's hours win unless OSM's value changed (as seen by a scan) after the club entered theirs, in
    which case the newer OSM value is used and the conflict is flagged so a leader can re-confirm.
    """
    osm = ((w.tags or {}).get("opening_hours") or "").strip() or None
    club = (w.club_hours or "").strip() or None
    if club:
        osm_newer = bool(osm and w.osm_hours_changed_at and w.club_hours_at and w.osm_hours_changed_at > w.club_hours_at)
        if not osm_newer:
            return club, "club", False
        return osm, "osm", True
    return (osm, "osm", False) if osm else (None, None, False)


def _t(iso: str | None, tz: ZoneInfo) -> datetime | None:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz) if iso else None


def _hhmm(dt: datetime | None) -> str | None:
    return dt.strftime("%H:%M") if dt else None


def classify(arrival: datetime, raw_arrival: dict | None, day_intervals: list | None, tz: ZoneInfo) -> dict:
    """Status for one arrival. `raw_arrival` / `day_intervals` come from the sidecar; None means no usable hours."""
    base = {"eta": _hhmm(arrival.astimezone(tz)), "hours_today": [], "opens_at": None, "closes_at": None, "comment": None}
    if raw_arrival is None:
        return {**base, "status": UNKNOWN, "label": LABELS[UNKNOWN]}
    base["comment"] = raw_arrival.get("comment")
    base["hours_today"] = [f"{_hhmm(_t(a, tz))}–{_hhmm(_t(b, tz))}" + (" (?)" if u else "")
                           for a, b, u, _c in (day_intervals or [])]
    if raw_arrival.get("unknown"):
        return {**base, "status": UNKNOWN, "label": LABELS[UNKNOWN]}
    if raw_arrival["open"]:
        nc = _t(raw_arrival.get("next_change"), tz)
        if nc and not raw_arrival.get("next_change_open") and nc <= arrival + CLOSES_SOON:
            return {**base, "status": CLOSES_SOON_S, "label": LABELS[CLOSES_SOON_S], "closes_at": _hhmm(nc)}
        return {**base, "status": OPEN, "label": LABELS[OPEN], "closes_at": _hhmm(nc) if nc else None}
    opens = _t(raw_arrival.get("next_open"), tz)
    if not day_intervals:
        return {**base, "status": CLOSED_TODAY, "label": LABELS[CLOSED_TODAY],
                "opens_at": opens.strftime("%a %H:%M") if opens else None}
    return {**base, "status": CLOSED, "label": LABELS[CLOSED], "opens_at": _hhmm(opens) if opens and opens.date() == arrival.astimezone(tz).date() else (opens.strftime("%a %H:%M") if opens else None)}


def _day_bounds(day: date, tz: ZoneInfo) -> tuple[str, str]:
    a = datetime.combine(day, time(0), tzinfo=tz)
    return a.astimezone(timezone.utc).isoformat(), (a + timedelta(days=1)).astimezone(timezone.utc).isoformat()


def evaluate_stops(waypoints, start: datetime, groups: list[Group], planned_s: list[float], tz_name: str,
                   client, country_code: str = "ie") -> list[dict]:
    """Per candidate stop with hours or in the café category: status for each pace group.

    Raises HoursUnavailable if the sidecar can't be reached (callers show "unknown", never "open").
    """
    from geoalchemy2.shape import to_shape
    tz = ZoneInfo(tz_name)
    day_from, day_to = _day_bounds(start.astimezone(tz).date(), tz)
    wanted = [w for w in waypoints if w.kind == "cafe" or (w.club_hours or "").strip()]
    items, meta = [], {}
    for w in wanted:
        rules, source, conflict = effective_hours(w)
        meta[w.id] = (rules, source, conflict)
        if rules:
            pt = to_shape(w.geom)
            items.append({"id": w.id, "rules": rules, "lat": pt.y, "lon": pt.x, "country_code": country_code,
                          "tz": tz_name, "day_from": day_from, "day_to": day_to,
                          "arrivals": [eta(start, w.s_m, g, planned_s).astimezone(timezone.utc).isoformat() for g in groups]})
    raw = {r["id"]: r for r in client.evaluate(items)}
    out = []
    for w in wanted:
        rules, source, conflict = meta[w.id]
        res = raw.get(w.id)
        error = res.get("error") if res else None
        per_group = []
        for i, g in enumerate(groups):
            arrival = eta(start, w.s_m, g, planned_s)
            ra = res["arrivals"][i] if res and not error else None
            per_group.append({"group": g.name, **classify(arrival, ra, res.get("day") if res and not error else None, tz)})
        out.append({"id": w.id, "name": w.name, "s_m": w.s_m, "hours": rules, "hours_source": source,
                    "osm_changed_since_club": conflict, "hours_error": error, "groups": per_group,
                    "alternatives": {}})
    _suggest_alternatives(out, groups)
    return out


def _suggest_alternatives(stops: list[dict], groups: list[Group]) -> None:
    """For each group where a stop is closed (or about to close), name the nearest stop along the route
    that is open on that group's arrival."""
    for gi, g in enumerate(groups):
        open_stops = [s for s in stops if s["groups"][gi]["status"] == OPEN]
        for s in stops:
            if s["groups"][gi]["status"] in (CLOSED, CLOSED_TODAY, CLOSES_SOON_S) and open_stops:
                alt = min(open_stops, key=lambda o: abs(o["s_m"] - s["s_m"]))
                s["alternatives"][g.name] = {"id": alt["id"], "name": alt["name"], "s_m": alt["s_m"]}
