"""Met Éireann warnings: stored as issued, relevance by county and time, expiry, the 'couldn't check' state."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import warnings_feed as wf
from app.models import Warning, WarningPoll

FIXTURES = Path(__file__).parent / "fixtures"
UTC = timezone.utc
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
RIDE_START, RIDE_END = datetime(2026, 10, 11, 9, 0, tzinfo=UTC), datetime(2026, 10, 11, 13, 0, tzinfo=UTC)   # Sunday

ON_ROUTE = [{"code": "EI12", "name": "Kildare", "name_key": "kildare", "sections": [[0.0, 20000.0]]},
            {"code": "EI07", "name": "Dublin", "name_key": "dublin", "sections": [[20000.0, 30000.0]]}]


def item(cap="cap-1", level="yellow", type_="Wind", regions=("EI12",), headline=None, onset=None, expiry=None, **extra):
    onset = onset or datetime(2026, 10, 11, 6, 0, tzinfo=UTC)
    expiry = expiry or datetime(2026, 10, 11, 20, 0, tzinfo=UTC)
    return {"id": 1, "capId": cap, "type": type_, "severity": "Moderate", "certainty": "Likely", "level": level,
            "issued": "2026-10-10T10:00:00+01:00", "updated": "2026-10-10T10:00:00+01:00",
            "onset": onset.isoformat(), "expiry": expiry.isoformat(),
            "headline": headline or f"Status {level.title()} - {type_} warning for Kildare",
            "description": "<p>Strong winds.&nbsp;</p><ul><li>Difficult travelling conditions</li></ul>",
            "status": "Warning", "regions": list(regions), **extra}


class Feed:
    def __init__(self, items=None, error=None):
        self.items, self.error, self.calls = items or [], error, 0

    def fetch(self):
        self.calls += 1
        if self.error:
            raise wf.WarningsError(self.error)
        return self.items


def poll(items, now=NOW):
    return wf.poll(Feed(items), now)


def banner():
    return wf.banner(ON_ROUTE, RIDE_START, RIDE_END, 2.0, NOW)


# ---------------- the five scenarios from the brief ----------------

def test_no_warnings(client):
    poll([])
    b = banner()
    assert b["status"] == "ok" and b["warnings"] == [] and b["top_level"] is None and not b["postpone_prompt"]
    assert b["checked_at"] is not None                                   # we can say when we last looked


def test_a_yellow_wind_warning_for_a_county_on_the_route(client):
    poll([item(level="yellow", type_="Wind", regions=("EI12",))])
    b = banner()
    [w] = b["warnings"]
    assert b["status"] == "ok" and b["top_level"] == "yellow" and not b["postpone_prompt"]
    assert (w["level_title"], w["instruction"], w["type"]) == ("Weather Alert", "Be Aware", "Wind")
    assert w["headline"] == "Status Yellow - Wind warning for Kildare" and w["counties"] == ["Kildare"]
    assert w["sections"] == [[0.0, 20000.0]]                             # the whole Kildare stretch is highlighted


def test_an_orange_rain_warning_for_a_county_not_on_the_route_is_ignored(client):
    poll([item(cap="donegal", level="orange", type_="Rain", regions=("EI06",), headline="Status Orange - Rain warning for Donegal")])
    b = banner()
    assert Warning.query.count() == 1                                    # we hold it...
    assert b["warnings"] == [] and b["top_level"] is None and not b["postpone_prompt"]   # ...but it isn't relevant


def test_an_expired_warning_is_removed_and_never_shown(client):
    old = item(cap="old", onset=NOW - timedelta(days=1), expiry=NOW - timedelta(minutes=1))
    poll([old, item(cap="live")])
    assert [w.cap_id for w in Warning.query.all()] == ["live"]           # removed on the poll that sees it
    db_row = Warning(cap_id="stale", type="Wind", level="yellow", onset=NOW - timedelta(days=1), expiry=NOW - timedelta(seconds=1),
                     headline="Status Yellow - Wind warning for Kildare", regions=["EI12"])
    from app import db
    db.session.add(db_row); db.session.commit()
    assert "stale" not in [w["id"] for w in banner()["warnings"]]        # and even if one lingers, it is not relevant
    assert wf.purge_expired(NOW) == 1 and Warning.query.filter_by(cap_id="stale").count() == 0


def test_an_unreachable_feed_says_so_instead_of_implying_there_are_none(client):
    b0 = banner()                                                         # never polled at all
    assert b0["status"] == "unreachable" and b0["message"] == wf.UNREACHABLE_TEXT and b0["warnings"] == []
    assert "Couldn't check Met Éireann weather warnings" in b0["message"] and "met.ie/warnings" in b0["message"]
    assert wf.poll(Feed(error="Met Éireann answered 503"), NOW)["ok"] is False
    assert banner()["status"] == "unreachable"
    poll([])                                                              # it comes back
    assert banner()["status"] == "ok" and banner()["message"] is None


def test_a_feed_that_has_gone_quiet_for_30_minutes_counts_as_unreachable(client):
    poll([], now=NOW)
    assert wf.feed_status(NOW + timedelta(minutes=29))["ok"] is True
    assert wf.feed_status(NOW + timedelta(minutes=31))["ok"] is False


# ---------------- licence: stored exactly as issued ----------------

def test_fields_are_stored_verbatim_with_the_raw_item_beside_them(client):
    src = item(cap="x1", level="orange", type_="Rain", regions=("EI12", "EI07"),
               headline="Status Orange - Rain warning for Kildare, Dublin")
    poll([src])
    w = Warning.query.one()
    assert (w.cap_id, w.type, w.level, w.severity, w.certainty) == ("x1", "Rain", "orange", "Moderate", "Likely")
    assert w.headline == src["headline"] and w.description_html == src["description"] and w.regions == ["EI12", "EI07"]
    assert w.onset == datetime.fromisoformat(src["onset"]) and w.expiry == datetime.fromisoformat(src["expiry"])
    assert w.issued == datetime.fromisoformat(src["issued"]) and w.raw == src


def test_the_real_feed_sample_is_stored_without_alteration(client):
    items = json.loads((FIXTURES / "metie_warnings_live.json").read_text(encoding="utf-8"))
    wf.poll(Feed(items), datetime(2026, 10, 8, 12, 0, tzinfo=UTC))
    assert Warning.query.count() == len(items) == 2
    for src in items:
        w = Warning.query.filter_by(cap_id=src["capId"]).one()
        assert w.headline == src["headline"] and w.description_html == src["description"] and w.regions == src["regions"]
        assert w.onset == datetime.fromisoformat(src["onset"]) and w.expiry == datetime.fromisoformat(src["expiry"])


def test_display_changes_only_the_clock_and_never_the_meaning_of_a_time(client):
    poll([item(onset=datetime(2026, 10, 11, 6, 0, tzinfo=UTC), expiry=datetime(2026, 10, 11, 20, 0, tzinfo=UTC))])
    [w] = banner()["warnings"]
    assert w["valid_from"] == "Sun 11 Oct 07:00" and w["valid_to"] == "Sun 11 Oct 21:00"       # Irish summer time
    assert w["valid_from_iso"] == "2026-10-11T06:00:00+00:00" and w["valid_to_iso"] == "2026-10-11T20:00:00+00:00"
    assert w["issued"] == "Sat 10 Oct 10:00"


def test_the_description_text_is_kept_while_markup_that_isnt_text_is_dropped():
    out = wf.clean_html('<p onclick="x()">Heavy rain.&nbsp;</p><script>alert(1)</script><ul><li>One</li><li><a href="http://x">Two</a></li></ul><img src=x>')
    assert out == "<p>Heavy rain.&nbsp;</p>alert(1)<ul><li>One</li><li>Two</li></ul>"
    assert "<script" not in out and "onclick" not in out and "href" not in out
    assert wf.clean_html(None) == "" and wf.clean_html("a < b") == "a &lt; b"


# ---------------- relevance rules ----------------

def test_the_warning_window_must_overlap_the_ride_plus_a_buffer(client):
    just_before = item(cap="a", onset=RIDE_START - timedelta(hours=8), expiry=RIDE_START - timedelta(hours=1, minutes=30))
    way_before = item(cap="b", onset=RIDE_START - timedelta(hours=8), expiry=RIDE_START - timedelta(hours=2, minutes=30))
    just_after = item(cap="c", onset=RIDE_END + timedelta(hours=1, minutes=30), expiry=RIDE_END + timedelta(hours=6))
    way_after = item(cap="d", onset=RIDE_END + timedelta(hours=2, minutes=30), expiry=RIDE_END + timedelta(hours=6))
    poll([just_before, way_before, just_after, way_after])
    got = {w["id"] for w in banner()["warnings"]}
    assert got == {"a", "c"}                                              # inside the 2 h buffer either side
    zero = {w.cap_id for w in wf.relevant(ON_ROUTE, RIDE_START, RIDE_END, 0.0, NOW)}
    assert zero == set()                                                  # the buffer is what brought them in
    wide = {w.cap_id for w in wf.relevant(ON_ROUTE, RIDE_START, RIDE_END, 3.0, NOW)}
    assert wide == {"a", "b", "c", "d"}                                   # and it is configurable


def test_orange_and_red_ask_the_leader_to_consider_postponing(client):
    poll([item(cap="y", level="yellow"), item(cap="o", level="orange", type_="Rain", headline="Status Orange - Rain warning for Kildare")])
    b = banner()
    assert b["top_level"] == "orange" and b["postpone_prompt"] is True
    assert [w["id"] for w in b["warnings"]] == ["o", "y"]                 # most severe first
    poll([item(cap="r", level="red", type_="Wind", headline="Status Red - Wind warning for Kildare")])
    b = banner()
    assert b["top_level"] == "red" and b["warnings"][0]["level_title"] == "Severe Weather Warning" and b["warnings"][0]["instruction"] == "Take Action"
    assert wf.LEVELS["orange"][:2] == ("Weather Warning", "Be Prepared")


def test_county_names_in_the_headline_catch_a_warning_even_if_a_region_code_is_wrong(client):
    poll([item(cap="n", regions=("EI99",), headline="Status Yellow - Wind warning for Kerry and Kildare")])
    assert [w["id"] for w in banner()["warnings"]] == ["n"]


def test_region_codes_alone_are_enough_too(client):
    poll([item(cap="c", regions=("EI07",), headline="Status Yellow - Wind warning")])      # no names in the headline
    assert [w["id"] for w in banner()["warnings"]] == ["c"]


def test_marine_warnings_are_not_relevant_to_a_road_route(client):
    poll([item(cap="m", type_="Small Craft", regions=("EI811", "EI822"), headline="Small Craft warning for all coasts of Ireland")])
    assert banner()["warnings"] == []


def test_each_warning_highlights_only_the_stretches_in_its_counties(client):
    poll([item(cap="dub", regions=("EI07",), headline="Status Yellow - Wind warning for Dublin")])
    [w] = banner()["warnings"]
    assert w["sections"] == [[20000.0, 30000.0]]


def test_headline_names_parsing():
    assert wf.headline_names("Status Yellow - Rain warning for Galway, Leitrim, Mayo, Sligo") == {"galway", "leitrim", "mayo", "sligo"}
    assert wf.headline_names("Wind warning for Cork and Kerry") == {"cork", "kerry"}
    assert wf.headline_names("Small Craft warning for all coasts of Ireland") == {"allcoastsofireland"}
    assert wf.headline_names("No area here") == set()


def test_the_day_picker_dot_is_the_highest_level_for_that_days_ride(client):
    poll([item(cap="y", level="yellow"), item(cap="o", level="orange", headline="Status Orange - Wind warning for Dublin", regions=("EI07",))])
    assert wf.day_level(ON_ROUTE, RIDE_START, RIDE_END, 2.0, NOW) == "orange"
    assert wf.day_level(ON_ROUTE, RIDE_START + timedelta(days=3), RIDE_END + timedelta(days=3), 2.0, NOW) is None


# ---------------- polling behaviour ----------------

def test_a_warning_that_disappears_from_the_feed_is_removed(client):
    poll([item(cap="a"), item(cap="b")])
    assert Warning.query.count() == 2
    poll([item(cap="b")], now=NOW + timedelta(minutes=10))
    assert [w.cap_id for w in Warning.query.all()] == ["b"]


def test_an_updated_warning_replaces_the_old_text_in_place(client):
    poll([item(cap="a", level="yellow")])
    poll([item(cap="a", level="orange", headline="Status Orange - Wind warning for Kildare")], now=NOW + timedelta(minutes=10))
    w = Warning.query.one()
    assert w.level == "orange" and w.headline.startswith("Status Orange")


def test_a_failed_poll_leaves_what_we_had_and_is_recorded(client):
    poll([item(cap="a")])
    assert wf.poll(Feed(error="boom"), NOW + timedelta(minutes=5)) == {"ok": False, "error": "boom"}
    assert Warning.query.count() == 1
    last = WarningPoll.query.order_by(WarningPoll.at.desc()).first()
    assert last.ok is False and last.error == "boom"
    b = wf.banner(ON_ROUTE, RIDE_START, RIDE_END, 2.0, NOW + timedelta(minutes=40))
    assert b["status"] == "unreachable" and [w["id"] for w in b["warnings"]] == ["a"]   # stale but unexpired: shown, with the caveat


def test_malformed_items_are_skipped_not_fatal(client):
    res = poll([{"nonsense": True}, item(cap="ok"), {**item(cap="bad-level"), "level": "purple"}, {**item(cap="no-dates"), "onset": None}, "x"])
    assert res["stored"] == 1 and [w.cap_id for w in Warning.query.all()] == ["ok"]


def test_ensure_fresh_polls_only_when_needed(client):
    feed = Feed([item()])
    wf.ensure_fresh(feed, NOW)
    assert feed.calls == 1
    wf.ensure_fresh(feed, NOW + timedelta(minutes=5))
    assert feed.calls == 1                                                # recent: no extra request
    wf.ensure_fresh(feed, NOW + timedelta(minutes=13))
    assert feed.calls == 2
    bad = Feed(error="x")
    wf.poll(bad, NOW + timedelta(minutes=20))
    wf.ensure_fresh(Feed([item()]), NOW + timedelta(minutes=21))          # last poll failed: try again straight away
    assert Warning.query.count() == 1


def test_the_real_http_provider_reports_failures_as_warnings_errors():
    class R:
        def __init__(self, code, body=None, bad=False):
            self.status_code, self._b, self._bad = code, body, bad

        def json(self):
            if self._bad:
                raise ValueError("no json")
            return self._b

    class S:
        def __init__(self, r): self.r = r
        def get(self, url, timeout=None): return self.r

    assert wf.MetEireannWarnings(session=S(R(200, []))).fetch() == []
    for resp in (R(503), R(200, bad=True), R(200, {"not": "a list"})):
        with pytest.raises(wf.WarningsError):
            wf.MetEireannWarnings(session=S(resp)).fetch()


def _rss(*titles):
    items = "".join(f"<item><title>{t}</title></item>" for t in titles)
    return f'<?xml version="1.0"?><rss><channel><title>x</title>{items}</channel></rss>'.encode()


def test_a_missing_json_file_is_all_clear_only_when_the_rss_index_lists_nothing_but_marine():
    class R:
        def __init__(self, code, content=b""):
            self.status_code, self.content = code, content

    class S:
        def __init__(self, rss): self.rss = rss
        def get(self, url, timeout=None): return R(404) if url == wf.FEED_URL else self.rss

    p = lambda rss: wf.MetEireannWarnings(session=S(rss))
    assert p(R(200, _rss("Small Craft", "Gale Warning"))).fetch() == []
    assert p(R(200, _rss())).fetch() == []
    for rss in (R(200, _rss("Small Craft", "Wind")), R(503), R(200, b"<not xml")):
        with pytest.raises(wf.WarningsError):
            p(rss).fetch()
