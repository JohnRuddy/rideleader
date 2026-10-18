"""Club Sponsor Publishing: premium state, who may manage, which campaign a ride shows, history, the widget, clicks, reports."""
import csv
import io
import re
from datetime import timedelta

import pytest
from PIL import Image

from app import billing, db, sponsors
from app import ride_sheet as rs
from app.models import (Club, ClubEmbed, ClubPremium, Route, Sponsor, SponsorCampaign, SponsorDaily)
from tests.test_auth_security import other_client, sign_in
from tests.test_clubs import member, new_club, user
from tests.test_share import FakeHours, SUNDAY
from tests.test_climbs_http import climbing_points
from tests.test_conditions_http import FakeWeather, NOW
from tests.test_editor import FakeRouter
from tests.test_warnings import Feed
from tests.conftest import make_gpx

BROWSER = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1"}


def png(w=200, h=100, fmt="PNG"):
    out = io.BytesIO()
    Image.new("RGB", (w, h), (11, 93, 59)).save(out, fmt)
    return out.getvalue()


def today(club):
    return sponsors.club_today(club.id)


def add_sponsor(club, name="Dunne's Bike Shop"):
    sp = Sponsor(club_id=club.id, name=name, website="https://dunnes.example.com", logo_blob=png(), logo_mime="image/png",
                 logo_token=sponsors.new_token())
    db.session.add(sp)
    db.session.commit()
    return sp


def add_campaign(club, sponsor=None, *, status="published", start=-1, end=30, placements=("ride_sheet", "widget"), route_id=None,
                 default=False, headline="10% off for club members", name=None, cta=True, order=0):
    sponsor = sponsor or add_sponsor(club, f"Sponsor {Sponsor.query.count() + 1}")
    t = today(club)
    c = SponsorCampaign(club_id=club.id, sponsor_id=sponsor.id, name=name or f"Campaign {SponsorCampaign.query.count() + 1}",
                        headline=headline, body="Show your club jersey in store.", cta_label="See the offer" if cta else None,
                        cta_url="https://dunnes.example.com/club" if cta else None, starts_on=t + timedelta(days=start),
                        ends_on=t + timedelta(days=end), status=status, placements=list(placements), route_id=route_id,
                        is_default=default, display_order=order, click_token=sponsors.new_token())
    db.session.add(c)
    db.session.commit()
    return c


def activate(club, months=12):
    billing.activate(club.id, by_user_id=None, months=months)
    db.session.commit()


@pytest.fixture
def world(client):
    """Club A (premium on) with an admin, a ride leader and a member; club B (premium on) with its own admin."""
    cfg = client.application.config
    cfg.update(CLOCK=lambda: NOW, WEATHER_CLIENT=FakeWeather(), WARNINGS_CLIENT=Feed([]), CLOSURE_PROVIDERS=[],
               HOURS_CLIENT=FakeHours(), ROUTING_CLIENT=FakeRouter(), QUEUE_INLINE=True)
    a = Club.query.first()
    b = new_club("Other Wheelers")
    site = user("site@example.com", "admin")
    admin_a, leader_a, plain_a, admin_b = (user(f"{n}@example.com") for n in ("admin-a", "leader-a", "plain-a", "admin-b"))
    member(admin_a, a.id, "admin")
    member(leader_a, a.id)
    member(plain_a, a.id)
    member(admin_b, b.id, "admin")
    activate(a)
    activate(b)
    w = type("W", (), {})()
    w.a, w.b, w.site, w.admin_a, w.leader_a, w.plain_a, w.admin_b = a, b, site, admin_a, leader_a, plain_a, admin_b
    w.client = client
    return w


def login(c, u):
    sign_in(c, u)
    return c


def login_new(w, u, club=None):
    c = other_client(w.client)
    sign_in(c, u)
    with c.session_transaction() as s:
        s["club_id"] = (club or w.a).id
    return c


def make_ride(w, owner=None, club=None):
    """A shared, club-owned ride sheet by `owner`; returns (route_id, token, client)."""
    owner = owner or w.leader_a
    c = login_new(w, owner)
    r = c.post("/routes/upload", data={"gpx": (io.BytesIO(make_gpx(climbing_points(), name="Sunday spin")), "a.gpx")},
               content_type="multipart/form-data")
    assert r.status_code in (200, 302), r.data
    route = Route.query.order_by(Route.id.desc()).first()
    r = c.put(f"/api/routes/{route.id}/ride", json={"date": SUNDAY, "start_time": "09:00", "meeting_name": "Kildare Town Square",
                                                   "leader_phone": "087 123 4567", "notes": "Bring a spare tube.",
                                                   "groups": [{"name": "A", "speed_kmh": 28, "stop_min": 20}]})
    assert r.status_code == 200, r.data
    token = c.post(f"/api/routes/{route.id}/share").get_json()["token"]
    return route.id, token, c


def sheet(w, token):
    r = other_client(w.client).get(f"/r/{token}", headers=BROWSER)
    assert r.status_code == 200
    return r.get_data(as_text=True)


def has_panel(page):
    return 'class="sponsor-panel"' in page


# ===================== premium state =====================

def test_a_club_with_nothing_activated_is_not_premium(client):
    c = Club.query.first()
    assert sponsors.premium_state(c.id)["state"] == "none"
    assert not sponsors.premium_state(c.id)["serving"]


def test_premium_runs_for_the_paid_period_then_is_expired_never_active(world):
    now = sponsors.utcnow()
    assert sponsors.premium_state(world.a.id, now)["state"] == "active"
    later = now + timedelta(days=366)
    st = sponsors.premium_state(world.a.id, later)
    assert st["state"] == "expired" and not st["serving"]


def test_premium_in_the_future_is_pending_and_does_not_serve(client):
    c = Club.query.first()
    billing.activate(c.id, by_user_id=None, months=12, now=sponsors.utcnow() + timedelta(days=10))
    db.session.commit()
    st = sponsors.premium_state(c.id)
    assert st["state"] == "pending" and not st["serving"]


def test_cancelled_premium_stops_serving_immediately(world):
    billing.cancel(world.a.id)
    db.session.commit()
    st = sponsors.premium_state(world.a.id)
    assert st["state"] == "cancelled" and not st["serving"]


def test_a_documented_grace_period_keeps_serving_then_stops(world):
    world.client.application.config["PREMIUM_GRACE_DAYS"] = 7
    p = ClubPremium.query.filter_by(club_id=world.a.id).first()
    p.expires_at = sponsors.utcnow() - timedelta(days=3)
    db.session.commit()
    st = sponsors.premium_state(world.a.id)
    assert st["serving"] and st["in_grace"]
    p.expires_at = sponsors.utcnow() - timedelta(days=9)
    db.session.commit()
    assert not sponsors.premium_state(world.a.id)["serving"]


def test_billing_extends_a_running_premium_and_never_charges(world):
    before = ClubPremium.query.filter_by(club_id=world.a.id).first().expires_at
    billing.activate(world.a.id, by_user_id=world.site.id, months=12, reference="INV-1")
    db.session.commit()
    p = ClubPremium.query.filter_by(club_id=world.a.id).first()
    assert p.expires_at > before + timedelta(days=360)
    assert p.price_cents == 1999 and p.currency == "EUR" and p.source == "manual" and p.external_ref == "INV-1"
    with pytest.raises(NotImplementedError):
        billing.handle_provider_event({"type": "x"}, "sig")


def test_only_the_platform_admin_can_switch_premium_on_or_off(world):
    for who in (world.admin_a, world.leader_a):
        r = login_new(world, who).post("/sponsors/premium", data={"action": "cancel"})
        assert r.status_code == 403
    assert sponsors.premium_state(world.a.id)["serving"]
    c = login_new(world, world.site, world.a)
    assert c.post("/sponsors/premium", data={"action": "cancel"}).status_code == 302
    assert sponsors.premium_state(world.a.id)["state"] == "cancelled"
    assert c.post("/sponsors/premium", data={"action": "activate", "months": "12", "reference": "R1"}).status_code == 302
    assert sponsors.premium_state(world.a.id)["serving"]


def test_the_dashboard_shows_the_price_and_value_statement_and_never_claims_premium_when_it_is_off(world):
    c = login_new(world, world.admin_a)
    page = c.get("/sponsors").get_data(as_text=True)
    assert "€19.99 per club per year" in page
    assert "Give your club sponsors a place in every ride briefing." in page
    assert sponsors.EMPTY_STATE in page.replace("&#39;", "'")
    add_campaign(world.a)
    billing.cancel(world.a.id)
    db.session.commit()
    page = c.get("/sponsors").get_data(as_text=True)
    assert "Cancelled" in page and "pill-active" not in page.split("Campaigns")[0]


# ===================== who may do what / tenant isolation =====================

def test_members_and_leaders_cannot_open_the_sponsors_area(world):
    for who in (world.leader_a, world.plain_a):
        c = login_new(world, who)
        for path in ("/sponsors", "/sponsors/campaign/new", "/sponsors/reports", "/sponsors/embed", "/sponsors/reports.csv"):
            assert c.get(path).status_code == 403, path


def test_signed_out_visitors_are_sent_to_sign_in(client):
    assert client.get("/sponsors").status_code in (302, 401)


def test_the_sponsors_link_is_in_the_nav_only_for_club_admins(world):
    assert "/sponsors" in login_new(world, world.admin_a).get("/").get_data(as_text=True)
    assert 'href="/sponsors"' not in login_new(world, world.plain_a).get("/").get_data(as_text=True)


def test_another_club_cannot_see_or_change_this_clubs_sponsors_campaigns_or_reports(world):
    sp = add_sponsor(world.a)
    camp = add_campaign(world.a, sp)
    sponsors.count(world.a.id, camp.id, "ride_sheet", "delivery")
    b = login_new(world, world.admin_b, world.b)
    for path in (f"/sponsors/sponsor/{sp.id}", f"/sponsors/campaign/{camp.id}", f"/sponsors/campaign/{camp.id}/preview"):
        assert b.get(path).status_code == 404, path
    for action in ("publish", "pause", "resume", "archive", "duplicate"):
        assert b.post(f"/sponsors/campaign/{camp.id}/{action}").status_code == 404, action
    assert b.post(f"/sponsors/sponsor/{sp.id}/archive").status_code == 404
    page = b.get("/sponsors").get_data(as_text=True)
    assert sp.name not in page and camp.name not in page
    rep = b.get(f"/sponsors/reports.csv?start={today(world.a)}&end={today(world.a)}").get_data(as_text=True)
    assert camp.name not in rep and sp.name not in rep
    db.session.refresh(camp)
    assert camp.status == "published"


def test_a_browser_supplied_club_id_does_not_decide_anything(world):
    sp = add_sponsor(world.b)
    c = login_new(world, world.admin_a)
    r = c.post("/sponsors/campaign/new", data={"club_id": world.b.id, "sponsor_id": sp.id, "name": "X", "headline": "Hello",
                                               "starts_on": str(today(world.a)), "ends_on": str(today(world.a)), "placement_ride_sheet": "on"})
    assert r.status_code == 404                       # sponsor belongs to the other club
    assert SponsorCampaign.query.count() == 0


def test_a_campaign_cannot_be_tied_to_another_clubs_ride(world):
    rid, _, _ = make_ride(world)
    sp = add_sponsor(world.b)
    other = login_new(world, world.admin_b, world.b)
    r = other.post("/sponsors/campaign/new", data={"sponsor_id": sp.id, "name": "X", "headline": "Hello", "route_id": rid,
                                                   "starts_on": str(today(world.b)), "ends_on": str(today(world.b)), "placement_ride_sheet": "on"})
    assert r.status_code == 400 and SponsorCampaign.query.count() == 0


# ===================== creating things =====================

def test_a_club_admin_can_add_a_sponsor_with_a_logo_and_a_campaign_and_preview_it(world):
    c = login_new(world, world.admin_a)
    r = c.post("/sponsors/sponsor/new", data={"name": "Dunne's", "website": "https://dunnes.example.com", "description": "Local shop",
                                              "logo": (io.BytesIO(png(600, 300)), "logo.png")}, content_type="multipart/form-data")
    assert r.status_code == 302
    sp = Sponsor.query.one()
    assert sp.club_id == world.a.id and sp.logo_mime in ("image/png", "image/webp")
    t = today(world.a)
    r = c.post("/sponsors/campaign/new", data={"sponsor_id": sp.id, "name": "Autumn offer", "headline": "10% off for members",
                                               "body": "Show your jersey.", "cta_label": "See the offer", "cta_url": "https://dunnes.example.com/club",
                                               "starts_on": str(t), "ends_on": str(t + timedelta(days=30)), "placement_ride_sheet": "on",
                                               "placement_widget": "on", "is_default": "on"})
    assert r.status_code == 302
    camp = SponsorCampaign.query.one()
    assert camp.status == "draft" and camp.is_default and camp.placements == ["ride_sheet", "widget"]
    page = c.get(f"/sponsors/campaign/{camp.id}/preview").get_data(as_text=True)
    assert "10% off for members" in page and "Club sponsor" in page
    assert sponsors.campaign_phase(camp, t) == "draft"


def test_campaign_actions_publish_pause_resume_duplicate_archive(world):
    camp = add_campaign(world.a, status="draft")
    c = login_new(world, world.admin_a)
    assert c.post(f"/sponsors/campaign/{camp.id}/publish").status_code == 302
    db.session.refresh(camp)
    assert camp.status == "published"
    c.post(f"/sponsors/campaign/{camp.id}/pause")
    db.session.refresh(camp)
    assert camp.status == "paused"
    c.post(f"/sponsors/campaign/{camp.id}/resume")
    db.session.refresh(camp)
    assert camp.status == "published"
    c.post(f"/sponsors/campaign/{camp.id}/duplicate")
    copies = SponsorCampaign.query.filter(SponsorCampaign.id != camp.id).all()
    assert len(copies) == 1 and copies[0].status == "draft" and copies[0].click_token != camp.click_token and not copies[0].is_default
    c.post(f"/sponsors/campaign/{camp.id}/archive")
    db.session.refresh(camp)
    assert camp.status == "archived" and SponsorCampaign.query.get(camp.id) is not None      # archived, never deleted


def test_archiving_a_sponsor_keeps_it_and_hides_it(world):
    sp = add_sponsor(world.a)
    camp = add_campaign(world.a, sp)
    c = login_new(world, world.admin_a)
    c.post(f"/sponsors/sponsor/{sp.id}/archive")
    db.session.refresh(sp)
    assert sp.archived_at is not None
    assert sponsors.select_campaign(world.a.id, "ride_sheet") is None
    assert c.get(f"/sponsor-logo/{sp.logo_token}").status_code == 404
    c.post(f"/sponsors/sponsor/{sp.id}/restore")
    db.session.refresh(sp)
    assert sp.archived_at is None and sponsors.select_campaign(world.a.id, "ride_sheet").id == camp.id


def test_nothing_can_be_created_or_published_without_premium_but_everything_is_kept(world):
    camp = add_campaign(world.a, status="draft")
    billing.cancel(world.a.id)
    db.session.commit()
    c = login_new(world, world.admin_a)
    assert c.post(f"/sponsors/campaign/{camp.id}/publish").status_code == 403
    assert c.get("/sponsors/campaign/new").status_code == 403
    assert c.get("/sponsors").status_code == 200
    assert c.get("/sponsors/reports").status_code == 200
    assert SponsorCampaign.query.count() == 1


# ===================== validation =====================

@pytest.mark.parametrize("field,value", [("headline", "Buy <b>now</b>"), ("headline", "x" * 61), ("body", "y" * 141), ("cta_label", "z" * 25),
                                         ("cta_url", "http://dunnes.example.com"), ("cta_url", "javascript:alert(1)"),
                                         ("cta_url", "https://127.0.0.1/x"), ("cta_url", "https://localhost/x"),
                                         ("cta_url", "https://user:pw@dunnes.example.com"), ("cta_url", "https://shop.internal/x"),
                                         ("cta_url", "https://dunnes.example.com:8443/x"), ("starts_on", "not-a-date")])
def test_bad_campaign_content_is_refused(world, field, value):
    sp = add_sponsor(world.a)
    t = today(world.a)
    data = {"sponsor_id": sp.id, "name": "N", "headline": "Fine", "body": "ok", "cta_label": "Go", "cta_url": "https://dunnes.example.com/x",
            "starts_on": str(t), "ends_on": str(t + timedelta(days=5)), "placement_ride_sheet": "on", field: value}
    r = login_new(world, world.admin_a).post("/sponsors/campaign/new", data=data)
    assert r.status_code == 400 and SponsorCampaign.query.count() == 0


def test_dates_must_make_sense_and_a_button_needs_both_label_and_address(world):
    sp = add_sponsor(world.a)
    t = today(world.a)
    base = {"sponsor_id": sp.id, "name": "N", "headline": "Fine", "starts_on": str(t), "ends_on": str(t), "placement_ride_sheet": "on"}
    c = login_new(world, world.admin_a)
    assert c.post("/sponsors/campaign/new", data={**base, "ends_on": str(t - timedelta(days=1))}).status_code == 400
    assert c.post("/sponsors/campaign/new", data={**base, "ends_on": str(t + timedelta(days=900))}).status_code == 400
    assert c.post("/sponsors/campaign/new", data={**base, "cta_label": "Go"}).status_code == 400
    no_place = {k: v for k, v in base.items() if not k.startswith("placement_")}
    assert c.post("/sponsors/campaign/new", data=no_place).status_code == 400
    assert c.post("/sponsors/campaign/new", data=base).status_code == 302


def bad_logo(w, payload, name="logo.png"):
    return login_new(w, w.admin_a).post("/sponsors/sponsor/new", data={"name": "L", "logo": (io.BytesIO(payload), name)},
                                        content_type="multipart/form-data")


def test_logos_must_be_real_png_jpeg_or_webp_pictures(world):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    assert bad_logo(world, svg, "logo.svg").status_code == 400
    assert bad_logo(world, b"GIF89a....").status_code == 400
    assert bad_logo(world, b"not an image at all").status_code == 400
    assert bad_logo(world, png(10, 10)).status_code == 400                      # too small
    assert bad_logo(world, png(4100, 50)).status_code == 400                    # too large a side
    assert bad_logo(world, b"\x89PNG\r\n\x1a\n" + b"0" * 1_600_000).status_code == 400
    assert bad_logo(world, png(100, 100, "GIF")).status_code == 400
    assert Sponsor.query.count() == 0


def test_logos_are_re_encoded_and_served_safely(world):
    sp = add_sponsor(world.a)
    big = Image.new("RGB", (3000, 1500), (0, 80, 0))
    out = io.BytesIO()
    big.save(out, "JPEG")
    data, mime = sponsors.process_logo(io.BytesIO(out.getvalue()))
    im = Image.open(io.BytesIO(data))
    assert mime in ("image/png", "image/webp") and max(im.size) <= 480
    r = other_client(world.client).get(f"/sponsor-logo/{sp.logo_token}")
    assert r.status_code == 200 and r.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'none'" in r.headers["Content-Security-Policy"]
    assert other_client(world.client).get("/sponsor-logo/nonsense").status_code == 404


# ===================== which campaign a ride shows =====================

def test_draft_paused_archived_scheduled_and_expired_campaigns_are_never_shown(world):
    for kw in ({"status": "draft"}, {"status": "paused"}, {"status": "archived"}, {"start": 2, "end": 9}, {"start": -9, "end": -2}):
        SponsorCampaign.query.delete()
        db.session.commit()
        add_campaign(world.a, **kw)
        assert sponsors.select_campaign(world.a.id, "ride_sheet") is None, kw
        assert sponsors.select_campaign(world.a.id, "widget") is None, kw


def test_a_campaign_is_live_on_its_first_and_last_day(world):
    add_campaign(world.a, start=0, end=0)
    assert sponsors.select_campaign(world.a.id, "ride_sheet") is not None


def test_dates_follow_the_club_timezone(world):
    # 23:30 UTC on 1 July is already 2 July in Dublin (BST): a campaign starting on 2 July is live there.
    from datetime import datetime, timezone
    now = datetime(2027, 7, 1, 23, 30, tzinfo=timezone.utc)
    p = ClubPremium.query.filter_by(club_id=world.a.id).first()
    p.starts_at, p.expires_at = now - timedelta(days=30), now + timedelta(days=300)
    db.session.commit()
    sp = add_sponsor(world.a)
    from datetime import date
    db.session.add(SponsorCampaign(club_id=world.a.id, sponsor_id=sp.id, name="TZ", headline="Hi", starts_on=date(2027, 7, 2), ends_on=date(2027, 7, 9),
                                   status="published", placements=["ride_sheet"], click_token=sponsors.new_token()))
    db.session.commit()
    assert sponsors.select_campaign(world.a.id, "ride_sheet", now=now) is not None
    world.a.settings = {**(world.a.settings or {}), "timezone": "America/Los_Angeles"}
    db.session.commit()
    assert sponsors.select_campaign(world.a.id, "ride_sheet", now=now) is None


def test_a_placement_that_is_off_is_not_used(world):
    add_campaign(world.a, placements=("widget",))
    assert sponsors.select_campaign(world.a.id, "ride_sheet") is None
    assert sponsors.select_campaign(world.a.id, "widget") is not None


def test_ride_specific_beats_club_wide_and_club_wide_default_beats_the_rest(world):
    rid, _, _ = make_ride(world)
    wide = add_campaign(world.a, name="wide")
    default = add_campaign(world.a, name="default", default=True)
    route = db.session.get(Route, rid)
    assert sponsors.select_campaign(world.a.id, "ride_sheet", route=route, ride=route.ride).id == default.id
    specific = add_campaign(world.a, name="specific", route_id=rid)
    assert sponsors.select_campaign(world.a.id, "ride_sheet", route=route, ride=route.ride).id == specific.id
    assert wide.id != default.id


def test_a_ride_specific_campaign_is_not_shown_on_other_rides(world):
    rid1, _, _ = make_ride(world)
    rid2, _, _ = make_ride(world)
    add_campaign(world.a, route_id=rid1)
    r2 = db.session.get(Route, rid2)
    assert sponsors.select_campaign(world.a.id, "ride_sheet", route=r2, ride=r2.ride) is None


def test_no_sponsor_on_this_ride_and_explicit_choice(world):
    rid, token, c = make_ride(world)
    default = add_campaign(world.a, name="default", default=True)
    other = add_campaign(world.a, name="other")
    assert has_panel(sheet(world, token))
    assert c.put(f"/api/routes/{rid}/sponsor", json={"mode": "none"}).status_code == 200
    assert not has_panel(sheet(world, token))
    assert c.put(f"/api/routes/{rid}/sponsor", json={"mode": "campaign", "campaign_id": other.id}).status_code == 200
    assert "Sponsor " in sheet(world, token) and sponsors.select_campaign(world.a.id, "ride_sheet", route=db.session.get(Route, rid),
                                                                           ride=db.session.get(Route, rid).ride).id == other.id
    c.put(f"/api/routes/{rid}/sponsor", json={"mode": "auto"})
    assert sponsors.select_campaign(world.a.id, "ride_sheet", route=db.session.get(Route, rid), ride=db.session.get(Route, rid).ride).id == default.id


def test_an_explicit_choice_that_has_ended_falls_back_to_automatic(world):
    rid, token, c = make_ride(world)
    chosen = add_campaign(world.a, name="chosen")
    fallback = add_campaign(world.a, name="fallback", default=True)
    c.put(f"/api/routes/{rid}/sponsor", json={"mode": "campaign", "campaign_id": chosen.id})
    chosen.status = "paused"
    db.session.commit()
    route = db.session.get(Route, rid)
    assert sponsors.select_campaign(world.a.id, "ride_sheet", route=route, ride=route.ride).id == fallback.id


def test_leaders_choose_only_their_clubs_published_ride_sheet_campaigns_and_only_for_their_own_rides(world):
    rid, _, c = make_ride(world)
    mine = add_campaign(world.a, name="mine")
    draft = add_campaign(world.a, name="draft", status="draft")
    widget_only = add_campaign(world.a, name="w", placements=("widget",))
    theirs = add_campaign(world.b, name="theirs")
    for bad in (draft, widget_only, theirs):
        assert c.put(f"/api/routes/{rid}/sponsor", json={"mode": "campaign", "campaign_id": bad.id}).status_code == 400
    assert c.put(f"/api/routes/{rid}/sponsor", json={"mode": "campaign", "campaign_id": mine.id}).status_code == 200
    assert c.put(f"/api/routes/{rid}/sponsor", json={"mode": "bogus"}).status_code == 400
    stranger = login_new(world, world.plain_a)
    assert stranger.put(f"/api/routes/{rid}/sponsor", json={"mode": "none"}).status_code in (403, 404)
    assert login_new(world, world.admin_b).put(f"/api/routes/{rid}/sponsor", json={"mode": "none"}).status_code in (403, 404)
    assert other_client(world.client).put(f"/api/routes/{rid}/sponsor", json={"mode": "none"}).status_code in (302, 401, 403, 404)


def test_rotation_is_off_by_default_and_stable_when_on(world):
    rid, _, _ = make_ride(world)
    first = add_campaign(world.a, name="one", default=True)
    add_campaign(world.a, name="two")
    add_campaign(world.a, name="three")
    route = db.session.get(Route, rid)
    for _ in range(5):
        assert sponsors.select_campaign(world.a.id, "ride_sheet", route=route, ride=route.ride,
                                        rotation_key=sponsors.sheet_rotation_key(route)).id == first.id
    world.a.settings = {**(world.a.settings or {}), "sponsor_rotation": True}
    db.session.commit()
    picks = {sponsors.select_campaign(world.a.id, "ride_sheet", route=route, ride=route.ride, rotation_key=sponsors.sheet_rotation_key(route)).id
             for _ in range(6)}
    assert len(picks) == 1                                                        # the same ride shows the same sponsor all week
    keys = {sponsors.select_campaign(world.a.id, "widget", rotation_key=f"widget:t:{d}").id for d in range(40)}
    assert len(keys) > 1                                                          # different keys do spread across the pool


def test_a_club_never_shows_another_clubs_campaign(world):
    add_campaign(world.b, default=True)
    assert sponsors.select_campaign(world.a.id, "ride_sheet") is None
    rid, token, _ = make_ride(world)
    assert not has_panel(sheet(world, token))


def test_rides_with_no_club_show_no_panel(client):
    assert sponsors.select_campaign(None, "ride_sheet") is None


# ===================== the ride sheet =====================

def test_the_panel_appears_after_the_essentials_and_before_the_footer_with_no_scripts_or_third_parties(world):
    rid, token, _ = make_ride(world)
    add_campaign(world.a, default=True)
    page = sheet(world, token)
    i_panel, i_foot = page.index('class="sponsor-panel"'), page.index('class="sheet-foot')
    assert page.index("Kildare Town Square") < i_panel < i_foot
    panel = page[page.index('<aside class="sponsor-panel"'):page.index("</aside>")]
    assert "Club sponsor" in panel and "<script" not in panel and "<iframe" not in panel and "<form" not in panel
    hrefs = re.findall(r'(?:href|src)="([^"]+)"', panel)
    assert all(h.startswith("/") for h in hrefs), hrefs                            # only our own addresses: logo and click redirect
    assert 'rel="sponsored noopener noreferrer"' in panel and 'alt=""' in panel
    assert "dunnes.example.com" not in panel                                       # the real destination is never in the page
    assert "sponsor-print" not in page                                             # print line only if the campaign asks for it


def test_the_print_acknowledgement_line_appears_only_when_chosen(world):
    rid, token, _ = make_ride(world)
    camp = add_campaign(world.a, default=True, placements=("ride_sheet", "print"))
    assert 'class="sponsor-print"' in sheet(world, token)
    camp.placements = ["ride_sheet"]
    db.session.commit()
    assert 'class="sponsor-print"' not in sheet(world, token)


def test_no_empty_box_when_there_is_nothing_to_show(world):
    rid, token, _ = make_ride(world)
    page = sheet(world, token)
    assert "sponsor" not in page.lower().replace("sponsors.css", "")


def test_a_panel_without_a_button_has_no_link(world):
    rid, token, _ = make_ride(world)
    add_campaign(world.a, default=True, cta=False)
    panel = sheet(world, token)
    panel = panel[panel.index('<aside class="sponsor-panel"'):panel.index("</aside>")]
    assert "/go/" not in panel


def test_sponsors_never_change_the_safety_content_of_the_sheet(world):
    rid, token, _ = make_ride(world)
    route = db.session.get(Route, rid)
    before = repr(rs.build(route, public=True))
    add_campaign(world.a, default=True)
    after = repr(rs.build(route, public=True))
    assert before == after


def test_expiry_removes_the_panel_but_the_shared_link_and_sheet_still_work(world):
    rid, token, _ = make_ride(world)
    add_campaign(world.a, default=True)
    assert has_panel(sheet(world, token))
    p = ClubPremium.query.filter_by(club_id=world.a.id).first()
    p.expires_at = sponsors.utcnow() - timedelta(days=1)
    db.session.commit()
    page = sheet(world, token)
    assert not has_panel(page) and "Kildare Town Square" in page
    assert other_client(world.client).get(f"/r/{token}/cues.pdf", headers=BROWSER).status_code in (200, 404)
    assert login_new(world, world.leader_a).get(f"/routes/{rid}").status_code == 200


def test_a_campaign_existing_never_implies_premium(world):
    rid, token, _ = make_ride(world)
    add_campaign(world.a, default=True)
    billing.cancel(world.a.id)
    db.session.commit()
    assert not has_panel(sheet(world, token))
    page = login_new(world, world.admin_a).get("/sponsors").get_data(as_text=True)
    assert "Cancelled" in page


def test_the_owners_preview_of_the_sheet_is_labelled_and_not_counted(world):
    rid, token, c = make_ride(world)
    add_campaign(world.a, default=True)
    r = c.get(f"/routes/{rid}/sheet")
    assert r.status_code == 200
    page = r.get_data(as_text=True)
    assert has_panel(page) and "Preview: this is how riders see it" in page
    assert SponsorDaily.query.count() == 0


# ===================== repeated and historical rides =====================

def test_running_a_ride_again_makes_a_separate_new_ride_and_leaves_the_old_one_alone(world):
    rid, token, c = make_ride(world)
    camp = add_campaign(world.a, default=True)
    old = db.session.get(Route, rid)
    old_date, old_notes = old.ride.date, old.ride.notes
    r = c.post(f"/routes/{rid}/run-again", data={"date": "2099-05-03"})
    assert r.status_code == 302
    new = Route.query.order_by(Route.id.desc()).first()
    assert new.id != rid and new.reused_from_id == rid and new.series_root_id == rid and new.club_id == world.a.id
    assert str(new.ride.date) == "2099-05-03" and new.ride.share_token is None and new.ride.sponsor_mode == "auto"
    assert len(new.waypoints) == len(old.waypoints) and new.ride.notes == old_notes
    db.session.refresh(old)
    assert old.ride.date == old_date and old.ride.share_token == token           # history and its link untouched
    assert has_panel(sheet(world, token))
    assert sponsors.select_campaign(world.a.id, "ride_sheet", route=new, ride=new.ride).id == camp.id


def test_a_new_run_gets_todays_campaign_and_does_not_copy_an_expired_offer(world):
    rid, token, c = make_ride(world)
    old_offer = add_campaign(world.a, name="old", default=True)
    c.put(f"/api/routes/{rid}/sponsor", json={"mode": "campaign", "campaign_id": old_offer.id})
    old_offer.ends_on = today(world.a) - timedelta(days=1)
    old_offer.starts_on = old_offer.ends_on - timedelta(days=5)
    new_offer = add_campaign(world.a, name="new", default=True)
    db.session.commit()
    c.post(f"/routes/{rid}/run-again", data={})
    new = Route.query.order_by(Route.id.desc()).first()
    assert new.ride.sponsor_mode == "auto" and new.ride.sponsor_campaign_id is None
    assert sponsors.select_campaign(world.a.id, "ride_sheet", route=new, ride=new.ride).id == new_offer.id


def test_the_default_new_date_is_a_future_weekly_occurrence(world):
    rid, _, c = make_ride(world)
    c.post(f"/routes/{rid}/run-again", data={})
    new = Route.query.order_by(Route.id.desc()).first()
    assert new.ride.date > NOW.date() and (new.ride.date - db.session.get(Route, rid).ride.date).days % 7 == 0


def test_a_campaign_tied_to_the_original_ride_follows_the_series(world):
    rid, _, c = make_ride(world)
    camp = add_campaign(world.a, route_id=rid)
    c.post(f"/routes/{rid}/run-again", data={})
    new = Route.query.order_by(Route.id.desc()).first()
    assert sponsors.select_campaign(world.a.id, "ride_sheet", route=new, ride=new.ride).id == camp.id


def test_historical_reporting_keeps_the_campaign_it_was_counted_against(world):
    rid, token, _ = make_ride(world)
    camp = add_campaign(world.a, default=True)
    sheet(world, token)
    camp.status = "archived"
    db.session.commit()
    t = today(world.a)
    rep = sponsors.report(world.a.id, t, t)
    assert [(l["campaign_id"], l["deliveries"]) for l in rep["lines"]] == [(camp.id, 1)]
    assert not has_panel(sheet(world, token))                                      # archived: stops, but its history stays


def test_only_people_who_can_see_a_ride_can_run_it_again_and_strava_routes_are_refused(world):
    rid, _, _ = make_ride(world)
    assert login_new(world, world.admin_b).post(f"/routes/{rid}/run-again", data={}).status_code == 404
    route = db.session.get(Route, rid)
    route.source = "strava"
    db.session.commit()
    c = login_new(world, world.leader_a)
    n = Route.query.count()
    c.post(f"/routes/{rid}/run-again", data={})
    assert Route.query.count() == n


# ===================== the website widget =====================

def embed(w, club=None):
    emb = sponsors.regenerate_embed((club or w.a).id, None)
    db.session.commit()
    return emb.token


def test_embed_setup_gives_an_opaque_address_and_snippet(world):
    c = login_new(world, world.admin_a)
    assert "No widget yet" in c.get("/sponsors/embed").get_data(as_text=True)
    assert c.post("/sponsors/embed/regenerate").status_code == 302
    page = c.get("/sponsors/embed").get_data(as_text=True)
    emb = ClubEmbed.query.filter_by(club_id=world.a.id, revoked_at=None).one()
    assert emb.token in page and "<iframe" in page and 'height="300"' in page
    assert re.fullmatch(r"[A-Za-z0-9_-]{20,}", emb.token) and "velo" not in emb.token.lower()


def test_only_admins_manage_the_widget(world):
    for who in (world.leader_a, world.plain_a):
        c = login_new(world, who)
        assert c.post("/sponsors/embed/regenerate").status_code == 403
        assert c.post("/sponsors/embed/revoke").status_code == 403


def test_the_widget_shows_only_approved_public_widget_content(world):
    tok = embed(world)
    add_campaign(world.a, name="sheet only", placements=("ride_sheet",), headline="Not for the website")
    add_campaign(world.a, name="draft", status="draft", headline="Draft headline")
    show = add_campaign(world.a, name="site", placements=("widget",), headline="Shown on the website")
    make_ride(world)
    r = other_client(world.client).get(f"/embed/{tok}/sponsor", headers=BROWSER)
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and "Shown on the website" in page
    for hidden in ("Not for the website", "Draft headline", "087 123", "Kildare", "leader-a", "@"):
        assert hidden not in page
    assert "<script" not in page and 'class="strip"' in page
    assert show.click_token in page


def test_widget_headers_allow_framing_but_lock_everything_else(world):
    tok = embed(world)
    add_campaign(world.a, placements=("widget",))
    h = other_client(world.client).get(f"/embed/{tok}/sponsor", headers=BROWSER).headers
    csp = h["Content-Security-Policy"]
    assert "frame-ancestors *" in csp and "default-src 'none'" in csp and "script-src" not in csp
    assert h["Referrer-Policy"] == "no-referrer" and h["X-Content-Type-Options"] == "nosniff"
    assert "X-Frame-Options" not in h and "max-age=300" in h["Cache-Control"] and "noindex" in h["X-Robots-Tag"]


def test_widget_with_nothing_eligible_is_empty_not_broken(world):
    tok = embed(world)
    r = other_client(world.client).get(f"/embed/{tok}/sponsor", headers=BROWSER)
    assert r.status_code == 200 and 'class="strip"' not in r.get_data(as_text=True)


def test_revoked_regenerated_and_unknown_widget_addresses_stop_working(world):
    tok = embed(world)
    add_campaign(world.a, placements=("widget",))
    c = login_new(world, world.admin_a)
    c.post("/sponsors/embed/regenerate")
    new = ClubEmbed.query.filter_by(club_id=world.a.id, revoked_at=None).one().token
    assert new != tok
    anon = other_client(world.client)
    assert anon.get(f"/embed/{tok}/sponsor").status_code == 404
    assert anon.get(f"/embed/{new}/sponsor", headers=BROWSER).status_code == 200
    c.post("/sponsors/embed/revoke")
    assert anon.get(f"/embed/{new}/sponsor").status_code == 404
    assert anon.get("/embed/nonsense/sponsor").status_code == 404


def test_the_widget_follows_premium_and_never_shows_another_clubs_sponsor(world):
    tok = embed(world)
    add_campaign(world.b, placements=("widget",), headline="Belongs to B")
    assert "Belongs to B" not in other_client(world.client).get(f"/embed/{tok}/sponsor", headers=BROWSER).get_data(as_text=True)
    add_campaign(world.a, placements=("widget",))
    billing.cancel(world.a.id)
    db.session.commit()
    assert 'class="strip"' not in other_client(world.client).get(f"/embed/{tok}/sponsor", headers=BROWSER).get_data(as_text=True)


def test_the_proxy_template_frames_everything_but_the_widget():
    text = open("deploy/rideleader.caddy.tmpl", encoding="utf-8").read()
    assert "@notembed not path /embed/*" in text and 'header @notembed X-Frame-Options "SAMEORIGIN"' in text
    assert text.count("X-Frame-Options") == 1


# ===================== clicks =====================

def test_a_click_goes_to_the_stored_address_and_is_counted(world):
    camp = add_campaign(world.a)
    anon = other_client(world.client)
    r = anon.get(f"/go/{camp.click_token}?p=sheet", headers=BROWSER)
    assert r.status_code == 302 and r.headers["Location"] == "https://dunnes.example.com/club"
    assert r.headers["Cache-Control"] == "no-store" and r.headers["Referrer-Policy"] == "no-referrer"
    t = today(world.a)
    assert sponsors.report(world.a.id, t, t)["lines"][0]["clicks"] == 1


def test_the_click_address_is_not_an_open_redirect(world):
    camp = add_campaign(world.a)
    anon = other_client(world.client)
    for extra in ("&url=https://evil.example.org", "&next=//evil.example.org", "&to=https://evil.example.org", "&redirect=https://evil.example.org"):
        r = anon.get(f"/go/{camp.click_token}?p=sheet{extra}", headers=BROWSER)
        assert r.headers["Location"] == "https://dunnes.example.com/club"
    assert anon.get("/go/not-a-real-token?p=sheet", headers=BROWSER).status_code == 404
    assert anon.get(f"/go/{camp.click_token}?p=bogus", headers=BROWSER).status_code == 404
    camp.cta_url = "http://insecure.example.com"                                 # a damaged stored address is never followed
    db.session.commit()
    assert anon.get(f"/go/{camp.click_token}?p=sheet", headers=BROWSER).status_code == 404


def test_bots_prefetches_previews_and_the_admin_preview_are_not_counted(world):
    camp = add_campaign(world.a)
    anon = other_client(world.client)
    anon.get(f"/go/{camp.click_token}?p=sheet", headers={"User-Agent": "Googlebot/2.1"})
    anon.get(f"/go/{camp.click_token}?p=sheet", headers={"User-Agent": "WhatsApp/2.23"})
    anon.get(f"/go/{camp.click_token}?p=sheet", headers={**BROWSER, "Sec-Purpose": "prefetch"})
    anon.get(f"/go/{camp.click_token}?p=sheet", headers={"User-Agent": ""})
    anon.get(f"/go/{camp.click_token}?p=preview", headers=BROWSER)
    assert SponsorDaily.query.filter_by(kind="click").count() == 0


def test_clicks_on_an_ended_campaign_show_a_notice_and_are_not_counted(world):
    camp = add_campaign(world.a, status="paused")
    r = other_client(world.client).get(f"/go/{camp.click_token}?p=sheet", headers=BROWSER)
    assert r.status_code == 410 and "Location" not in r.headers
    camp.status = "published"
    billing.cancel(world.a.id)
    db.session.commit()
    assert other_client(world.client).get(f"/go/{camp.click_token}?p=sheet", headers=BROWSER).status_code == 410
    assert SponsorDaily.query.count() == 0


def test_a_widget_click_is_counted_against_the_widget_placement(world):
    camp = add_campaign(world.a)
    other_client(world.client).get(f"/go/{camp.click_token}?p=widget", headers=BROWSER)
    row = SponsorDaily.query.filter_by(kind="click").one()
    assert row.placement == "widget" and row.campaign_id == camp.id


# ===================== measurement and reports =====================

def test_sheet_requests_and_panel_deliveries_are_counted_separately(world):
    rid, token, _ = make_ride(world)
    camp = add_campaign(world.a, default=True)
    sheet(world, token)
    sheet(world, token)
    camp.status = "paused"
    db.session.commit()
    sheet(world, token)                                                            # a request with no panel: still a request
    t = today(world.a)
    rep = sponsors.report(world.a.id, t, t)
    assert rep["sheet_requests"] == 3 and rep["lines"][0]["deliveries"] == 2


def test_bots_and_previews_are_not_counted_on_the_sheet_or_widget(world):
    rid, token, _ = make_ride(world)
    add_campaign(world.a, default=True)
    tok = embed(world)
    anon = other_client(world.client)
    for ua in ("Googlebot/2.1", "facebookexternalhit/1.1", "WhatsApp/2.23", "curl/8.0", ""):
        anon.get(f"/r/{token}", headers={"User-Agent": ua})
        anon.get(f"/embed/{tok}/sponsor", headers={"User-Agent": ua})
    anon.head(f"/r/{token}", headers=BROWSER)
    anon.get(f"/r/{token}", headers={**BROWSER, "Purpose": "prefetch"})
    assert SponsorDaily.query.count() == 0


def test_counts_are_daily_totals_with_no_visitor_identity(world):
    cols = {c.name for c in SponsorDaily.__table__.columns}
    assert cols == {"id", "day", "club_id", "campaign_id", "placement", "kind", "count"}
    rid, token, _ = make_ride(world)
    add_campaign(world.a, default=True)
    for _ in range(3):
        sheet(world, token)
    assert SponsorDaily.query.filter_by(kind="sheet_request").count() == 1


def test_a_ride_with_no_club_or_no_panel_still_counts_nothing_wrong(world):
    rid, token, _ = make_ride(world)
    sheet(world, token)
    t = today(world.a)
    assert sponsors.report(world.a.id, t, t)["lines"] == []


def test_the_report_pages_are_honestly_labelled(world):
    camp = add_campaign(world.a)
    sponsors.count(world.a.id, camp.id, "ride_sheet", "delivery")
    c = login_new(world, world.admin_a)
    for path in ("/sponsors/reports", "/sponsors/report"):
        page = c.get(path).get_data(as_text=True).lower()
        assert "ride sheet requests" in page and "recorded clicks" in page and "panel deliveries" in page
        assert page.count("unique cyclists") == page.count("count of unique cyclists")          # only ever in "not a count of unique cyclists"
        assert page.count("viewable impressions") == page.count("not viewable impressions")


def test_the_csv_has_the_right_columns_isolates_clubs_and_blocks_formula_injection(world):
    evil = add_campaign(world.a, name="=HYPERLINK(\"http://x\")", sponsor=add_sponsor(world.a, "+cmd|x"))
    other = add_campaign(world.b, name="B campaign")
    sponsors.count(world.a.id, evil.id, "ride_sheet", "delivery")
    sponsors.count(world.b.id, other.id, "ride_sheet", "delivery")
    t = today(world.a)
    r = login_new(world, world.admin_a).get(f"/sponsors/reports.csv?start={t}&end={t}")
    assert r.status_code == 200 and r.mimetype == "text/csv" and "attachment" in r.headers["Content-Disposition"]
    text = r.get_data(as_text=True)
    assert "B campaign" not in text
    rows = list(csv.reader(io.StringIO(text)))
    flat = [cell for row in rows for cell in row]
    assert not any(cell.startswith(("=", "+", "-", "@")) for cell in flat if cell and not cell.lstrip("-").isdigit())
    assert any("HYPERLINK" in cell for cell in flat)


def test_report_date_ranges_are_clamped_and_tolerant(world):
    c = login_new(world, world.admin_a)
    assert c.get("/sponsors/reports?start=junk&end=junk").status_code == 200
    assert c.get("/sponsors/reports?start=2000-01-01&end=2999-01-01").status_code == 200


def test_reports_are_still_available_after_premium_ends(world):
    camp = add_campaign(world.a)
    sponsors.count(world.a.id, camp.id, "ride_sheet", "delivery")
    p = ClubPremium.query.filter_by(club_id=world.a.id).first()
    p.expires_at = sponsors.utcnow() - timedelta(days=1)
    db.session.commit()
    t = today(world.a)
    c = login_new(world, world.admin_a)
    assert camp.name in c.get(f"/sponsors/reports?start={t}&end={t}").get_data(as_text=True)
    assert c.get(f"/sponsors/reports.csv?start={t}&end={t}").status_code == 200


# ===================== settings =====================

def test_club_settings_for_timezone_and_rotation(world):
    c = login_new(world, world.admin_a)
    assert c.post("/sponsors/settings", data={"timezone": "Europe/Dublin", "sponsor_rotation": "on"}).status_code == 302
    from app import club as clubmod
    s = clubmod.settings(world.a.id)
    assert s["timezone"] == "Europe/Dublin" and s["sponsor_rotation"] is True
    c.post("/sponsors/settings", data={"timezone": "Mars/Olympus"})
    assert clubmod.settings(world.a.id)["timezone"] == "Europe/Dublin"
    assert login_new(world, world.leader_a).post("/sponsors/settings", data={"timezone": "Europe/Paris"}).status_code == 403


# ===================== the platform stays free of ad tech =====================

def test_no_ad_network_or_tracking_code_in_the_sponsor_templates_and_assets():
    import pathlib
    root = pathlib.Path("app")
    blob = ""
    for p in list(root.glob("templates/*sponsor*.html")) + list(root.glob("templates/embed_public.html")) + [root / "static/sponsors.css", root / "static/embed.css"]:
        blob += p.read_text(encoding="utf-8").lower()
    for banned in ("adsbygoogle", "googlesyndication", "doubleclick", "gtag(", "fbq(", "analytics.js", "<script src=\"http"):
        assert banned not in blob
    public = (root / "templates/embed_public.html").read_text(encoding="utf-8") + (root / "templates/_sponsor_panel.html").read_text(encoding="utf-8")
    assert "<script" not in public.lower()
