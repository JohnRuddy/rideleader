"""'Ready to ride?': one glance at everything a leader checks before telling the group the ride is on.

Built from the same ride-sheet data the rider sees, so the two can never disagree. Every row says what it found, in the
words a leader would use, and links to the part of the page that fixes it. Where we couldn't check something the row says
so (status 'unknown'); an unchecked item is never reported as fine.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import hours as hrs
from . import ride_sheet as rs
from .plan import Plan, clock_now

OK, WARN, BAD, UNKNOWN = "ok", "warn", "bad", "unknown"
ORDER = {BAD: 0, WARN: 1, UNKNOWN: 2, OK: 3}


def _row(key, label, status, text, anchor=None):
    return {"key": key, "label": label, "status": status, "text": text, "anchor": anchor}


def readiness(route, plan: Plan | None = None) -> dict:
    plan = plan or Plan(route)
    s = rs.build(route, plan)
    tz = ZoneInfo(s["tz"])
    today = clock_now().astimezone(tz).date()
    ride_day = date.fromisoformat(s["date_iso"])
    rows = []

    # day and time
    when = f"{ride_day.strftime('%A %d %B')}, {s['start_time']} start"
    if ride_day < today:
        rows.append(_row("day", "Day", BAD, f"{when} has already passed. Pick the day you are actually riding.", "daypicker"))
    else:
        rows.append(_row("day", "Day", OK, when + (" (today)" if ride_day == today else ""), "daypicker"))

    # meeting point
    if (s["meeting"]["name"] or "").strip() in ("", "Route start"):
        rows.append(_row("meet", "Meeting point", WARN, "No meeting point set, so riders will be told “Route start”.", "ride"))
    else:
        rows.append(_row("meet", "Meeting point", OK, s["meeting"]["name"], "ride"))

    # warnings
    b = s["banner"]
    if b["status"] != "ok":
        rows.append(_row("warnings", "Weather warnings", UNKNOWN,
                         "Couldn't check Met Éireann just now. Look at met.ie/warnings before you ride.", "warn-banner"))
    elif b["warnings"]:
        top = b["warnings"][0]
        lvl = {"yellow": WARN, "orange": BAD, "red": BAD}.get(b["top_level"], WARN)
        rows.append(_row("warnings", "Weather warnings", lvl,
                         f"{top['level_title']} ({top['level'].title()}): {top['type']}, {', '.join(top['counties'])}", "warn-banner"))
    else:
        rows.append(_row("warnings", "Weather warnings", OK, "None for this route in the ride window.", "warn-banner"))

    # forecast
    w = s["weather"]
    if not w.get("available"):
        rows.append(_row("weather", "Forecast", UNKNOWN, w.get("reason") or "No forecast for that day yet.", "weather"))
    elif w.get("flags"):
        rows.append(_row("weather", "Forecast", WARN, w["flags"][0]["text"], "weather"))
    else:
        rows.append(_row("weather", "Forecast", OK, w.get("summary") or "Nothing flagged.", "weather"))

    # daylight
    d = s.get("daylight") or {}
    if d.get("warnings"):
        rows.append(_row("daylight", "Daylight", WARN, d["warnings"][0], "weather"))
    elif d.get("sunset"):
        fin = s["groups"][0]["finish"] if s["groups"] else None
        rows.append(_row("daylight", "Daylight", OK, f"Sunset {d['sunset']}" + (f", first group finishes {fin}" if fin else ""), "weather"))

    # closures
    c = s["closures"]
    if c.get("error"):
        rows.append(_row("closures", "Closures", UNKNOWN, c["error"], "closures"))
    elif c["hits"]:
        rows.append(_row("closures", "Closures", BAD, f"{len(c['hits'])} on the route: {c['hits'][0]['description']}", "closures"))
    elif not c.get("all_sources_ok", True):
        rows.append(_row("closures", "Closures", WARN, "None found, but not every source could be checked.", "closures"))
    else:
        rows.append(_row("closures", "Closures", OK, "None found. Councils and TII don't publish a feed, so ask locally.", "closures"))

    # stops
    ticked = [x for x in s["stops"]]
    aeds = [x for x in ticked if x["kind"] == "aed"]
    if not ticked:
        rows.append(_row("stops", "Stops on the device", WARN, "Nothing ticked yet, so the file goes out as a bare route.", "panel"))
    else:
        rows.append(_row("stops", "Stops on the device", OK, f"{len(ticked)} ticked ({len(aeds)} defibrillator{'s' if len(aeds) != 1 else ''}).", "panel"))
    if ticked and not aeds:
        rows.append(_row("aed", "Defibrillator", WARN, "None ticked. The ride sheet will still list the nearest in each 10 km.", "panel"))

    # cafés open when the group gets there
    if s.get("cafe_error"):
        rows.append(_row("cafes", "Café stops", UNKNOWN, s["cafe_error"], "panel"))
    elif s["cafes"]:
        bad = [(c_["name"], g) for c_ in s["cafes"] for g in c_["groups"] if g["status"] in (hrs.CLOSED, hrs.CLOSED_TODAY)]
        unsure = [c_["name"] for c_ in s["cafes"] if all(g["status"] == hrs.UNKNOWN for g in c_["groups"])]
        if bad:
            rows.append(_row("cafes", "Café stops", BAD, f"{bad[0][0]} is closed when group {bad[0][1]['group']} gets there ({bad[0][1]['eta']}).", "panel"))
        elif unsure:
            rows.append(_row("cafes", "Café stops", WARN, f"No opening hours for {unsure[0]}" + (f" and {len(unsure) - 1} more" if len(unsure) > 1 else "") + ". Ring ahead.", "panel"))
        else:
            rows.append(_row("cafes", "Café stops", OK, f"{len(s['cafes'])} café{'s' if len(s['cafes']) != 1 else ''} open on arrival.", "panel"))

    rows_sorted = sorted(rows, key=lambda r: ORDER[r["status"]])
    worst = rows_sorted[0]["status"] if rows_sorted else OK
    headline = {BAD: "Not ready: fix the red items", WARN: "Nearly there: a few things to check",
                UNKNOWN: "Ready, but some checks couldn't run", OK: "Ready to ride"}[worst]
    return {"status": worst, "headline": headline, "checked_at": datetime.now(tz).strftime("%H:%M"), "items": rows_sorted}
