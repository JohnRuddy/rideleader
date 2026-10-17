"""The key journeys, end to end, plus the plain-language guide and the phone basics every page must have."""
import io
import re
from datetime import datetime, timedelta, timezone

import pytest

from app import clubs, db
from app.models import Club, Membership, Route, User
from tests.conftest import hilly_points, make_gpx
from tests.test_auth_security import link_from_mail, other_client, outbox, sign_in


def user(email, role="leader"):
    u = User(email=email, role=role)
    db.session.add(u)
    db.session.commit()
    return u


def member(u, club_id, role="member"):
    db.session.add(Membership(user_id=u.id, club_id=club_id, role=role))
    db.session.commit()


def upload(c, name="Sunday spin"):
    return c.post("/routes/upload", data={"gpx": (io.BytesIO(make_gpx(hilly_points(), name=name)), "a.gpx")},
                  content_type="multipart/form-data")


# ---------------- the guide ----------------

def test_the_guide_is_public_and_explains_the_workings(client):
    r = client.get("/how-it-works")
    assert r.status_code == 200
    page = r.data.decode()
    for needle in ("A ride in four steps", "What the checks mean", "Pace groups and arrival times", "Clubs and invite codes",
                   "Strava", "invite code", "average moving speed", "never calls a ride", "112 or 999"):
        assert needle in page, needle


def test_the_guide_quotes_the_real_default_limits(client):
    from app import club
    page = client.get("/how-it-works").data.decode()
    d = club.DEFAULT_SETTINGS
    assert f"{d['gust_kmh']:g} km/h" in page and f"{d['ice_c']:g}" in page and f"{d['lights_min_before_sunset']} minutes" in page


def test_the_guide_is_linked_from_every_kind_of_page(client):
    assert 'href="/how-it-works"' in client.get("/welcome").data.decode()
    assert 'href="/how-it-works"' in client.get("/login").data.decode()
    a = user("a@example.com")
    member(a, Club.query.first().id)
    sign_in(client, a)
    assert 'href="/how-it-works"' in client.get("/").data.decode()


# ---------------- phone basics on every kind of page ----------------

@pytest.mark.parametrize("path", ["/welcome", "/login", "/how-it-works", "/privacy"])
def test_public_pages_have_the_phone_basics(client, path):
    html = client.get(path).data.decode()
    assert 'name="viewport" content="width=device-width, initial-scale=1' in html
    assert '<html lang="en">' in html
    assert 'class="skip"' in html and 'id="main"' in html                      # keyboard users can skip the menu
    assert html.count("<h1") == 1                                              # one main heading per page
    for img in re.findall(r"<img\b[^>]*>", html):
        assert 'alt="' in img, img                                             # every image says what it is


def test_the_menu_collapses_for_signed_in_people_and_marks_the_current_page(client):
    a = user("a@example.com")
    member(a, Club.query.first().id)
    sign_in(client, a)
    html = client.get("/").data.decode()
    assert '<details class="menu"' in html and "Sign out" in html
    assert re.search(r'<a href="/"[^>]*aria-current="page"[^>]*>My routes</a>', html)
    assert "Velo Club" in html                                                  # the club you are acting in


def test_the_public_ride_sheet_has_plain_links_and_no_menu_script(client):
    a = user("a@example.com")
    member(a, Club.query.first().id)
    sign_in(client, a)
    upload(client)
    token = client.post(f"/api/routes/{Route.query.one().id}/share").get_json()["token"]
    html = other_client(client).get(f"/r/{token}").data.decode()
    assert '<details class="menu"' not in html and 'href="/how-it-works"' in html


# ---------------- journey: a new member joins ----------------

def test_a_new_member_sees_which_club_they_are_joining_and_then_a_welcome(client):
    client.application.config["INVITE_REQUIRED"] = True
    user("boss@example.com", "admin")
    club = Club.query.first()
    _, code = clubs.create_invite(club.id, None)
    db.session.commit()
    client.post("/login", data={"email": "new@example.com", "code": code})
    link = link_from_mail(client)
    page = client.get(link).data.decode()
    assert "Join Velo Club" in page and "new@example.com" in page and "Join and sign in" in page
    token = link.rstrip("/").split("/login/")[1].split("?")[0]
    assert client.post(f"/login/{token}").status_code == 302
    home = client.get("/").data.decode()
    assert "Welcome to Velo Club" in home and "Get your first ride ready" in home
    assert "Bring a route" in home and 'href="/how-it-works"' in home


def test_the_welcome_goes_away_once_there_is_a_route(client):
    a = user("a@example.com")
    member(a, Club.query.first().id)
    sign_in(client, a)
    assert "Get your first ride ready" in client.get("/").data.decode()
    upload(client)
    assert "Get your first ride ready" not in client.get("/").data.decode()


def test_the_login_page_asks_for_the_invite_code_in_plain_words(client):
    page = client.get("/login?code=ABCDE-FGH23").data.decode()
    assert "Invite code" in page and 'value="ABCDE-FGH23"' in page and "First time here?" in page


# ---------------- journey: a leader plans and shares ----------------

def test_the_route_page_says_what_to_do_next(client):
    a = user("a@example.com")
    member(a, Club.query.first().id)
    sign_in(client, a)
    upload(client)
    rid = Route.query.one().id
    page = client.get(f"/routes/{rid}").data.decode()
    assert 'class="next-step"' in page and "Find stops" in page and 'data-goto="stops"' in page
    # once stops have been found the prompt moves on to sharing
    from app.models import Waypoint
    from geoalchemy2.elements import WKTElement
    db.session.add(Waypoint(route_id=rid, kind="cafe", name="Cafe", s_m=1000, origin="osm", geom=WKTElement("POINT(-6.9 53.16)", srid=4326)))
    db.session.commit()
    page = client.get(f"/routes/{rid}").data.decode()
    assert 'data-goto="share"' in page and "Share it" in page
    client.post(f"/api/routes/{rid}/share")
    assert 'class="next-step"' not in client.get(f"/routes/{rid}").data.decode()


def test_other_members_are_not_nagged_with_the_leaders_next_step(client):
    a, b = user("a@example.com"), user("b@example.com")
    for u in (a, b):
        member(u, Club.query.first().id)
    sign_in(client, a)
    upload(client)
    cb = other_client(client)
    sign_in(cb, b)
    assert 'class="next-step"' not in cb.get(f"/routes/{Route.query.one().id}").data.decode()


def test_the_phone_share_button_is_on_the_route_page_and_the_ride_sheet(client):
    a = user("a@example.com")
    member(a, Club.query.first().id)
    sign_in(client, a)
    upload(client)
    rid = Route.query.one().id
    assert 'id="share-native"' in client.get(f"/routes/{rid}").data.decode()
    token = client.post(f"/api/routes/{rid}/share").get_json()["token"]
    sheet = other_client(client).get(f"/r/{token}").data.decode()
    assert 'id="share-native"' in sheet and "navigator.share" in sheet


# ---------------- journey: a rider opens the link on a phone ----------------

def test_the_ride_sheet_shows_pace_groups_in_a_speed_roundel(client):
    a = user("a@example.com")
    member(a, Club.query.first().id)
    sign_in(client, a)
    upload(client)
    token = client.post(f"/api/routes/{Route.query.one().id}/share").get_json()["token"]
    html = other_client(client).get(f"/r/{token}").data.decode()
    assert html.count('class="roundel sm"') == 3 and 'aria-label="Group A, 28 kilometres an hour"' in html


def test_a_rider_can_add_the_ride_to_their_calendar_without_signing_in(client):
    a = user("a@example.com")
    member(a, Club.query.first().id)
    sign_in(client, a)
    upload(client, "Naas, loop; with commas")
    rid = Route.query.one().id
    client.put(f"/api/routes/{rid}/ride", json={"date": "2026-11-01", "start_time": "09:30", "meeting_name": "Naas Harbour",
                                                 "notes": "Mudguards on, please", "groups": [{"name": "A", "speed_kmh": 27, "stop_min": 15}]})
    token = client.post(f"/api/routes/{rid}/share").get_json()["token"]
    r = other_client(client).get(f"/r/{token}/ride.ics")
    assert r.status_code == 200 and r.mimetype == "text/calendar" and "attachment" in r.headers["Content-Disposition"]
    body = r.data.decode()
    assert body.startswith("BEGIN:VCALENDAR\r\n") and body.rstrip().endswith("END:VCALENDAR")
    assert "SUMMARY:Naas\\, loop\\; with commas" in body and "LOCATION:Naas Harbour" in body
    assert "Mudguards on\\, please" in body and f"/r/{token}" in body
    assert "DTSTART:20261101T093000Z" in body                                  # 09:30 in Dublin in November is UTC
    start = re.search(r"DTSTART:(\d{8}T\d{6}Z)", body).group(1)
    end = re.search(r"DTEND:(\d{8}T\d{6}Z)", body).group(1)
    assert end > start
    assert other_client(client).get("/r/not-a-real-token-123456/ride.ics").status_code == 404


def test_the_ride_sheet_links_to_the_calendar_file(client):
    a = user("a@example.com")
    member(a, Club.query.first().id)
    sign_in(client, a)
    upload(client)
    token = client.post(f"/api/routes/{Route.query.one().id}/share").get_json()["token"]
    assert f'href="/r/{token}/ride.ics"' in other_client(client).get(f"/r/{token}").data.decode()


# ---------------- journey: a club admin invites people ----------------

def test_an_admin_can_send_the_invite_on_whatsapp_straight_from_the_new_code(client):
    club = Club.query.first()
    admin = user("a@example.com")
    member(admin, club.id, "admin")
    sign_in(client, admin)
    page = client.post(f"/clubs/{club.id}/invites", data={"days": "30"}, follow_redirects=True).data.decode()
    assert "https://wa.me/?text=" in page and "Send on WhatsApp" in page and 'id="invite-native"' in page
    code = re.search(r"[A-Z2-9]{5}-[A-Z2-9]{5}", page).group(0)
    assert code.replace("-", "") in page.replace("-", "").replace("%2D", "") or code in page


# ---------------- journey: the site admin sets up a club ----------------

def test_the_site_admin_sets_up_a_club_and_is_taken_straight_to_inviting(client):
    boss = user("boss@example.com", "admin")
    sign_in(client, boss)
    r = client.post("/clubs", data={"name": "Kildare Wheelers"}, follow_redirects=True)
    page = r.data.decode()
    assert "Kildare Wheelers is set up" in page and "Invite people" in page and "Make an invite code" in page


# ---------------- the typeface ----------------

def test_every_font_and_asset_the_stylesheets_name_is_really_there(client):
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "app" / "static"
    missing = []
    for css in ("brand.css", "landing.css", "clubs.css"):
        for ref in re.findall(r'url\("(/static/[^")]+)"\)', (root / css).read_text(encoding="utf-8")):
            if not (root / ref.removeprefix("/static/")).exists():
                missing.append(ref)
    assert not missing, missing


def test_fonts_are_served_with_the_right_type_and_kept_for_a_year(client):
    r = client.get("/static/fonts/barlow-condensed-latin-800-normal.woff2")
    assert r.status_code == 200 and r.mimetype == "font/woff2"
    assert "max-age=31536000" in r.headers["Cache-Control"] and "immutable" in r.headers["Cache-Control"]
    assert r.data[:4] == b"wOF2"
    assert client.get("/static/fonts/OFL.txt").status_code == 200                         # the licence travels with the fonts


def test_pages_use_the_house_fonts_and_preload_the_two_that_matter(client):
    html = client.get("/welcome").data.decode()
    assert html.count('rel="preload" as="font"') == 2 and "barlow-condensed-latin-800" in html and "barlow-latin-400" in html
    css = client.get("/static/brand.css").data.decode()
    assert '"Barlow Condensed"' in css and 'font-display: swap' in css
    assert "fonts.googleapis.com" not in html and "fonts.gstatic.com" not in html + css    # nothing is fetched from a font server


# ---------------- a name people know you by ----------------

def test_a_member_can_set_the_name_their_club_sees(client):
    a = user("a@example.com")
    member(a, Club.query.first().id)
    sign_in(client, a)
    assert 'name="display_name"' in client.get("/account").data.decode()
    r = client.post("/account/name", data={"display_name": "  Aoife   Murphy  "})
    assert r.status_code == 302 and "saved=name" in r.headers["Location"]
    assert db.session.get(User, a.id).display_name == "Aoife Murphy"                      # tidied
    club_page = client.get(f"/clubs/{Club.query.first().id}").data.decode()
    assert "Aoife Murphy" in club_page
    client.post("/account/name", data={"display_name": "x" * 200})
    assert len(db.session.get(User, a.id).display_name) == 60
    client.post("/account/name", data={"display_name": "   "})
    assert db.session.get(User, a.id).display_name is None                                # blank means "use my email"


def test_the_ride_list_says_who_is_leading_each_ride(client):
    a, b = user("a@example.com"), user("b@example.com")
    for u in (a, b):
        member(u, Club.query.first().id)
    a.display_name = "Aoife Murphy"
    db.session.commit()
    sign_in(client, a)
    upload(client, "Aoife's loop")
    cb = other_client(client)
    sign_in(cb, b)
    assert "led by Aoife Murphy" in cb.get("/").data.decode()
    assert "led by" not in client.get("/").data.decode()                                  # your own rides don't say so


def test_setting_a_name_needs_sign_in(client):
    r = client.post("/account/name", data={"display_name": "Nobody"})
    assert r.status_code == 302 and "/login" in r.headers["Location"]


# ---------------------------------------------- the call button ----------------------------------------------

def test_the_ride_sheet_has_a_big_call_button_for_the_leader(client):
    a = user("a@example.com")
    member(a, Club.query.first().id)
    sign_in(client, a)
    upload(client)
    rid = Route.query.one().id
    client.put(f"/api/routes/{rid}/ride", json={"date": "2026-11-01", "start_time": "09:30", "leader_phone": "087 123 4567",
                                                 "groups": [{"name": "A", "speed_kmh": 27, "stop_min": 15}]})
    token = client.post(f"/api/routes/{rid}/share").get_json()["token"]
    html = other_client(client).get(f"/r/{token}").data.decode()
    assert 'class="btn small callbtn" href="tel:0871234567"' in html and "Call 087 123 4567" in html


# ---------------- the premium layer: scene, icons, trust, motion ----------------

def test_the_road_scene_is_decorative_self_contained_and_on_the_right_pages(client):
    for path in ("/welcome", "/login"):
        html = client.get(path).data.decode()
        assert 'class="scene' in html and 'aria-hidden="true"' in html and "sc-rider" in html
        assert html.count('class="scene-svg"') == 1
    import pathlib
    svg = (pathlib.Path(__file__).resolve().parent.parent / "app" / "templates" / "_scene.html").read_text(encoding="utf-8")
    assert "http://" not in svg.replace("http://www.w3.org", "") and "https://" not in svg and "<image" not in svg   # drawn, nothing fetched
    assert 'role="presentation"' in svg and 'focusable="false"' in svg


def test_motion_stops_for_people_who_ask_for_less(client):
    for sheet in ("brand.css", "landing.css"):
        css = client.get(f"/static/{sheet}").data.decode()
        assert "prefers-reduced-motion: reduce" in css, sheet
    html = client.get("/welcome").data.decode()
    assert "prefers-reduced-motion: reduce" in html                  # the reveal script checks it before hiding anything


def test_the_product_page_has_icons_for_every_check_and_an_honest_trust_section(client):
    html = client.get("/welcome").data.decode()
    assert html.count('<i class="ico"') >= 10                         # six checks + four trust pillars
    for needle in ("Why you can trust it", "No guesswork.", "Every figure has a source", "Unknown stays unknown", "You decide",
                   "Your club, and only your club", "No ads, no analytics"):
        assert needle in html, needle
    assert "loads no scripts" not in html                             # the ride sheet does run small scripts: don't claim otherwise
    for source in ("Met &Eacute;ireann", "OpenStreetMap", "Open-Meteo", "Strava"):
        assert source in html


def test_the_product_page_invents_no_numbers_or_testimonials(client):
    html = client.get("/welcome").data.decode().lower()
    for banned in ("testimonial", "trusted by", "clubs use", "5-star", "five star", "thousands of", "rated "):
        assert banned not in html, banned


def test_the_hero_route_line_draws_with_html_plates_not_stretched_text(client):
    html = client.get("/welcome").data.decode()
    assert html.count('class="rl-plate"') == 2 and 'class="rl-draw"' in html
    assert "<text" not in html.split('class="routeline"')[1].split("</div>")[0]


def test_the_page_reveals_only_after_its_script_has_run(client):
    css = client.get("/static/landing.css").data.decode()
    assert ".js .reveal" in css                                       # content is hidden only once the script has added .js
    assert ".reveal {" not in css.replace(".js .reveal {", "")


# ---------------- the pen illustrations ----------------

ART = ("club-ride", "bring-a-route", "check-the-weather", "cafe-stop", "regroup", "share-the-plan")


def _imgs(html):
    return re.findall(r"<img\b[^>]*>", html)


def test_every_illustration_is_shipped_small_and_with_both_sizes(client):
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "app" / "static" / "img"
    for slug in ART:
        for width, cap_kb in ((560, 120), (1120, 360)):
            f = root / f"{slug}-{width}.webp"
            assert f.exists(), f.name
            assert f.stat().st_size < cap_kb * 1024, (f.name, f.stat().st_size)
            assert f.read_bytes()[:4] == b"RIFF" and f.read_bytes()[8:12] == b"WEBP"
    assert not list(root.glob("*.png"))                                  # the heavy masters stay out of the site
    r = client.get("/static/img/club-ride-560.webp")
    assert r.status_code == 200 and r.mimetype == "image/webp"


def test_the_product_page_places_the_four_illustrations_where_the_guide_says(client):
    html = client.get("/welcome").data.decode()
    for slug in ("club-ride", "bring-a-route", "check-the-weather", "share-the-plan", "cafe-stop"):
        assert f"img/{slug}-560.webp" in html, slug
    assert "img/regroup-560.webp" not in html                              # that one lives in the guide
    # closing call to action: the club ride sits above the heading, on a cream plate
    final = html.split('<section class="final">')[1]
    assert final.index("club-ride-560") < final.index("see you Sunday")
    # the weather drawing introduces the checks, before the grid of six
    checks = html.split('id="checks"')[1]
    assert checks.index("check-the-weather-560") < checks.index('<ul class="facts">')
    # the route drawing is above step 1, the share drawing above step 3
    how = html.split('id="how"')[1]
    assert how.index("bring-a-route-560") < how.index("Bring a route") and how.index("share-the-plan-560") < how.index("Share it")


def test_the_guide_gets_the_route_stops_and_regroup_drawings(client):
    html = client.get("/how-it-works").data.decode()
    for slug in ("bring-a-route", "cafe-stop", "regroup"):
        assert f"img/{slug}-560.webp" in html, slug
    assert html.index("regroup-560") < html.index("Pace groups and arrival times") < html.index("average moving speed")


def test_every_image_has_alt_dimensions_and_the_below_the_fold_ones_load_lazily(client):
    for path in ("/welcome", "/how-it-works"):
        imgs = [i for i in _imgs(client.get(path).data.decode()) if "/img/" in i]
        assert imgs, path
        for tag in imgs:
            assert 'alt="' in tag and 'width="560"' in tag and 'height="373"' in tag and "srcset=" in tag and "sizes=" in tag, tag
    # at the top of the guide the first drawing is eager; further down they are lazy
    guide = [i for i in _imgs(client.get("/how-it-works").data.decode()) if "/img/" in i]
    assert "loading=" not in guide[0] and all('loading="lazy"' in t for t in guide[1:])
    page = [i for i in _imgs(client.get("/welcome").data.decode()) if "/img/" in i]
    assert all('loading="lazy"' in t for t in page)


def test_alt_text_describes_the_pictures_that_carry_meaning_and_is_empty_where_the_words_already_do(client):
    page = client.get("/welcome").data.decode()
    assert 'alt="Four helmeted club cyclists riding together on a country lane."' in page
    assert 'alt="A bicycle beneath a sun, raincloud and wind symbols."' in page
    steps = page.split('id="how"')[1].split("</section>")[0]
    assert steps.count('alt=""') == 3                                       # the headings beside them already say it
    guide = client.get("/how-it-works").data.decode()
    assert 'alt="A hand traces a cycling route on a folded map beside a GPS device."' in guide
    assert 'alt="Cyclists wait at a hilltop regroup point as another rider arrives."' in guide


def test_a_new_member_with_no_routes_sees_the_route_drawing_and_an_unshared_ride_shows_the_share_drawing(client):
    a = user("a@example.com")
    member(a, Club.query.first().id)
    sign_in(client, a)
    home = client.get("/").data.decode()
    assert "Get your first ride ready" in home and "bring-a-route-560" in home
    upload(client)
    assert "bring-a-route-560" not in client.get("/").data.decode()          # gone once there is a route
    route = client.get(f"/routes/{Route.query.one().id}").data.decode()
    assert "share-the-plan-560" in route and "share-art" in route


def test_the_art_never_sits_on_the_dark_panels_or_in_forms(client):
    css = client.get("/static/brand.css").data.decode()
    assert "#f6f3ea" in css                                                  # the cream plate every drawing sits on
    login = client.get("/login").data.decode()
    assert "/img/" not in login                                              # sign-in stays free of decoration
    page = client.get("/welcome").data.decode()
    dark = page.split('id="group"')[1].split("</section>")[0]
    assert "/img/" not in dark


def test_the_privacy_page_does_not_show_setting_names_to_the_public(client):
    client.application.config.update(CONTROLLER_NAME="", CONTROLLER_CONTACT="")
    html = client.get("/privacy").data.decode()
    assert "CONTROLLER_NAME" not in html and "ask your club admin" in html
    boss = user("boss@example.com", "admin")
    sign_in(client, boss)
    assert "CONTROLLER_NAME" in client.get("/privacy").data.decode()          # the site admin does see how to fix it
    client.application.config.update(CONTROLLER_NAME="Velo Club CLG", CONTROLLER_CONTACT="privacy@velo.example")
    assert "Velo Club CLG. Contact: privacy@velo.example" in client.get("/privacy").data.decode()


def test_images_are_kept_by_browsers_for_a_year(client):
    r = client.get("/static/img/regroup-1120.webp")
    assert r.status_code == 200 and "max-age=31536000" in r.headers["Cache-Control"]
