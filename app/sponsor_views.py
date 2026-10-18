"""Club Sponsor Publishing: the admin area, the public click redirect and logos, and the website widget.

Tenant rule: the club is always g.club (taken from the signed-in session and the person's membership). A club id, sponsor id
or campaign id sent by a browser is only ever looked up *within* that club, so another club's records simply don't exist
for the request. The platform admin works in a club by switching to it first, like everyone else.
"""
import csv
import io
from datetime import date, timedelta

from flask import (Blueprint, Response, abort, current_app, flash, g, get_flashed_messages, jsonify, redirect, render_template,
                   request, url_for)

from . import billing, clubs, db, ratelimit, sponsors
from . import club as clubmod
from .models import Route, Sponsor, SponsorCampaign
from .security import login_required

bp = Blueprint("sponsors", __name__)


# ------------------------------------------------------------------ guards

def _club():
    if g.get("club") is None:
        abort(404)
    return g.club


def _admin():
    club = _club()
    if not (g.get("club_admin") or clubs.is_platform_admin(g.user)):
        abort(403, description="Only a club admin can manage sponsors.")
    return club


def _premium(club):
    return sponsors.premium_state(club.id)


def _manage():
    """An admin of a club whose premium is serving: needed to create, edit or publish."""
    club = _admin()
    if not _premium(club)["serving"]:
        abort(403, description="Club Sponsor Publishing isn't active for this club, so sponsors can't be created or published. "
                               "Your existing sponsors, campaigns and reports are kept.")
    return club


def _sponsor(club, sponsor_id: int) -> Sponsor:
    return Sponsor.query.filter_by(id=sponsor_id, club_id=club.id).first() or abort(404)


def _campaign(club, campaign_id: int) -> SponsorCampaign:
    return SponsorCampaign.query.filter_by(id=campaign_id, club_id=club.id).first() or abort(404)


def _base_url() -> str:
    return (current_app.config.get("APP_BASE_URL") or request.url_root).rstrip("/")


def _notes():
    return get_flashed_messages(with_categories=True)


def _back(endpoint="sponsors.home", **kw):
    return redirect(url_for(endpoint, **kw))


# ------------------------------------------------------------------ the Sponsors area

@bp.get("/sponsors")
@login_required
def home():
    club = _admin()
    state = _premium(club)
    today = sponsors.club_today(club.id)
    rows = (SponsorCampaign.query.filter_by(club_id=club.id).order_by(SponsorCampaign.status == "archived",
                                                                       SponsorCampaign.display_order, SponsorCampaign.id.desc()).all())
    sponsor_rows = Sponsor.query.filter_by(club_id=club.id).order_by(Sponsor.archived_at.isnot(None), Sponsor.name).all()
    by_id = {s.id: s for s in sponsor_rows}
    camps = [{"c": c, "sponsor": by_id.get(c.sponsor_id), "phase": sponsors.campaign_phase(c, today)} for c in rows]
    return render_template("sponsors.html", club=club, state=state, state_text=sponsors.STATE_TEXT[state["state"]], camps=camps,
                           sponsor_rows=sponsor_rows, phase_text=sponsors.PHASE_TEXT, price=sponsors.PRICE_LINE,
                           value=sponsors.VALUE_STATEMENT, empty=sponsors.EMPTY_STATE, tz=sponsors.club_timezone(club.id).key,
                           rotation=bool(clubmod.settings(club.id).get("sponsor_rotation")), today=today,
                           platform_admin=clubs.is_platform_admin(g.user), notes=_notes(), can_manage=state["serving"],
                           embed=sponsors.active_embed(club.id))


@bp.post("/sponsors/settings")
@login_required
def settings():
    club = _admin()
    body = {"sponsor_rotation": request.form.get("sponsor_rotation") == "on"}
    if request.form.get("timezone"):
        body["timezone"] = request.form["timezone"].strip()
    try:
        updates = clubmod.validate(body)
    except ValueError as e:
        flash(str(e), "error")
        return _back()
    c = clubmod.get_club(club.id)
    c.settings = {**(c.settings or {}), **updates}
    db.session.commit()
    flash("Sponsor settings saved.", "ok")
    return _back()


@bp.post("/sponsors/premium")
@login_required
def premium():
    """Switching premium on or off is for the platform admin only. It records a payment made elsewhere; it takes none."""
    club = _club()
    if not clubs.is_platform_admin(g.user):
        abort(403, description="Only the site admin can switch Club Sponsor Publishing on or off.")
    action = request.form.get("action")
    if action == "activate":
        try:
            months = int(request.form.get("months") or 12)
        except ValueError:
            months = 12
        if not 1 <= months <= 36:
            flash("Choose between 1 and 36 months.", "error")
            return _back()
        ref = sponsors.clean_text(request.form.get("reference"), 120, "Reference") if request.form.get("reference") else None
        billing.activate(club.id, by_user_id=g.user.id, months=months, reference=ref, note="Recorded by the site admin (no payment taken here)")
        db.session.commit()
        flash(f"Club Sponsor Publishing is on for {club.name}. This records that the club has paid elsewhere; no payment was taken here.", "ok")
    elif action == "cancel":
        billing.cancel(club.id)
        db.session.commit()
        flash("Club Sponsor Publishing was cancelled. Sponsors and reports are kept; panels stop being shown.", "ok")
    else:
        abort(400)
    return _back()


# ------------------------------------------------------------------ sponsors

def _sponsor_fields(club, existing: Sponsor | None):
    f = request.form
    name = sponsors.clean_text(f.get("name"), sponsors.LIMITS["name"], "Sponsor name", required=True)
    website = sponsors.clean_url(f.get("website"), "Website")
    desc = sponsors.clean_text(f.get("description"), sponsors.LIMITS["description"], "Description")
    clash = Sponsor.query.filter(Sponsor.club_id == club.id, db.func.lower(Sponsor.name) == name.lower(), Sponsor.archived_at.is_(None))
    if existing is not None:
        clash = clash.filter(Sponsor.id != existing.id)
    if clash.first():
        raise ValueError("You already have a sponsor with that name.")
    logo = None
    file = request.files.get("logo")
    if file and file.filename:
        logo = sponsors.process_logo(file)
    elif existing is None:
        raise ValueError("Add the sponsor's logo.")
    return name, website, desc, logo


@bp.route("/sponsors/sponsor/new", methods=["GET", "POST"])
@login_required
def sponsor_new():
    club = _manage()
    if request.method == "POST":
        try:
            name, website, desc, logo = _sponsor_fields(club, None)
        except ValueError as e:
            return render_template("sponsor_form.html", sp=None, error=str(e), form=request.form, club=club), 400
        sp = Sponsor(club_id=club.id, name=name, website=website, description=desc, logo_blob=logo[0], logo_mime=logo[1],
                     logo_token=sponsors.new_token())
        db.session.add(sp)
        db.session.commit()
        flash(f"{sp.name} added. Now make a campaign for it.", "ok")
        return _back("sponsors.campaign_new", sponsor=sp.id)
    return render_template("sponsor_form.html", sp=None, error=None, form={}, club=club)


@bp.route("/sponsors/sponsor/<int:sponsor_id>", methods=["GET", "POST"])
@login_required
def sponsor_edit(sponsor_id):
    club = _admin()
    sp = _sponsor(club, sponsor_id)
    if request.method == "POST":
        _manage()
        try:
            name, website, desc, logo = _sponsor_fields(club, sp)
        except ValueError as e:
            return render_template("sponsor_form.html", sp=sp, error=str(e), form=request.form, club=club), 400
        sp.name, sp.website, sp.description = name, website, desc
        if logo:
            sp.logo_blob, sp.logo_mime, sp.logo_token = logo[0], logo[1], sponsors.new_token()
        db.session.commit()
        flash("Sponsor saved.", "ok")
        return _back()
    return render_template("sponsor_form.html", sp=sp, error=None, form={}, club=club)


@bp.post("/sponsors/sponsor/<int:sponsor_id>/archive")
@login_required
def sponsor_archive(sponsor_id):
    club = _admin()
    sp = _sponsor(club, sponsor_id)
    sp.archived_at = sponsors.utcnow()
    db.session.commit()
    flash(f"{sp.name} was archived. Its campaigns stop being shown; reports keep their history.", "ok")
    return _back()


@bp.post("/sponsors/sponsor/<int:sponsor_id>/restore")
@login_required
def sponsor_restore(sponsor_id):
    club = _manage()
    sp = _sponsor(club, sponsor_id)
    sp.archived_at = None
    db.session.commit()
    flash(f"{sp.name} is back.", "ok")
    return _back()


@bp.get("/sponsor-logo/<token>")
def logo(token):
    """A sponsor's logo, by its opaque name. Public by design (it is on public ride sheets); archived sponsors 404."""
    sp = Sponsor.query.filter_by(logo_token=token).first() if 8 <= len(token) <= 40 else None
    if sp is None or sp.archived_at is not None or not sp.logo_blob:
        abort(404)
    resp = Response(sp.logo_blob, mimetype=sp.logo_mime or "image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    resp.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
    return resp


# ------------------------------------------------------------------ campaigns

def _route_choices(club):
    return (Route.query.filter(Route.club_id == club.id, Route.source != "strava", Route.visibility == "club")
            .order_by(Route.created_at.desc()).limit(200).all())


def _campaign_from_form(club, existing: SponsorCampaign | None):
    f = request.form
    try:
        sponsor_id = int(f.get("sponsor_id", ""))
    except ValueError:
        raise ValueError("Choose a sponsor.")
    sp = _sponsor(club, sponsor_id)
    if sp.archived_at is not None:
        raise ValueError("That sponsor is archived. Restore it first, or choose another.")
    L = sponsors.LIMITS
    name = sponsors.clean_text(f.get("name"), 80, "Campaign name", required=True)
    headline = sponsors.clean_text(f.get("headline"), L["headline"], "Headline", required=True)
    body = sponsors.clean_text(f.get("body"), L["body"], "Supporting text")
    label = sponsors.clean_text(f.get("cta_label"), L["cta"], "Button label")
    url = sponsors.clean_url(f.get("cta_url"), "Button address")
    if bool(label) != bool(url):
        raise ValueError("A button needs both a label and an address, or neither.")
    starts, ends = sponsors.clean_dates(f.get("starts_on"), f.get("ends_on"))
    placements = [p for p in sponsors.PLACEMENTS if f.get(f"placement_{p}") == "on"]
    if not placements:
        raise ValueError("Choose at least one place for it to appear.")
    route_id = None
    if f.get("route_id"):
        try:
            rid = int(f["route_id"])
        except ValueError:
            raise ValueError("Choose a ride from the list.")
        if not Route.query.filter_by(id=rid, club_id=club.id).first():
            raise ValueError("Choose a ride from your club's list.")
        route_id = rid
    try:
        order = int(f.get("display_order") or 0)
    except ValueError:
        raise ValueError("Display order must be a whole number.")
    return {"sponsor_id": sp.id, "name": name, "headline": headline, "body": body, "cta_label": label, "cta_url": url,
            "starts_on": starts, "ends_on": ends, "placements": placements, "route_id": route_id,
            "display_order": max(-100, min(100, order)), "value_cents": sponsors.clean_value(f.get("value")),
            "is_default": f.get("is_default") == "on"}


def _make_default(club, keep: SponsorCampaign):
    for other in SponsorCampaign.query.filter(SponsorCampaign.club_id == club.id, SponsorCampaign.is_default.is_(True),
                                              SponsorCampaign.id != keep.id).all():
        other.is_default = False


def _campaign_form(club, camp, error=None, form=None, status=200):
    sps = Sponsor.query.filter_by(club_id=club.id, archived_at=None).order_by(Sponsor.name).all()
    today = sponsors.club_today(club.id)
    return render_template("campaign_form.html", club=club, camp=camp, sponsors=sps, routes=_route_choices(club), error=error,
                           form=form or {}, tz=sponsors.club_timezone(club.id).key, today=today, limits=sponsors.LIMITS,
                           placements=sponsors.PLACEMENTS, labels=sponsors.PLACEMENT_LABELS,
                           preselect=request.args.get("sponsor", type=int)), status


@bp.route("/sponsors/campaign/new", methods=["GET", "POST"])
@login_required
def campaign_new():
    club = _manage()
    if request.method == "POST":
        try:
            fields = _campaign_from_form(club, None)
        except ValueError as e:
            return _campaign_form(club, None, str(e), request.form, 400)
        publish = request.form.get("action") == "publish"
        c = SponsorCampaign(club_id=club.id, click_token=sponsors.new_token(), created_by=g.user.id,
                            status="published" if publish else "draft", **fields)
        db.session.add(c)
        db.session.flush()
        if c.is_default:
            _make_default(club, c)
        db.session.commit()
        flash("Campaign published." if publish else "Saved as a draft. Preview it, then publish when you're ready.", "ok")
        return _back("sponsors.campaign_preview", campaign_id=c.id)
    return _campaign_form(club, None)


@bp.route("/sponsors/campaign/<int:campaign_id>", methods=["GET", "POST"])
@login_required
def campaign_edit(campaign_id):
    club = _admin()
    c = _campaign(club, campaign_id)
    if request.method == "POST":
        _manage()
        try:
            fields = _campaign_from_form(club, c)
        except ValueError as e:
            return _campaign_form(club, c, str(e), request.form, 400)
        for k, v in fields.items():
            setattr(c, k, v)
        if request.form.get("action") == "publish" and c.status != "archived":
            c.status = "published"
        if c.is_default:
            _make_default(club, c)
        db.session.commit()
        flash("Campaign saved.", "ok")
        return _back("sponsors.campaign_preview", campaign_id=c.id)
    return _campaign_form(club, c)


def _set_status(campaign_id: int, status: str, message: str, *, needs_premium: bool):
    club = _manage() if needs_premium else _admin()
    c = _campaign(club, campaign_id)
    c.status = status
    db.session.commit()
    flash(message, "ok")
    return _back()


@bp.post("/sponsors/campaign/<int:campaign_id>/publish")
@login_required
def campaign_publish(campaign_id):
    return _set_status(campaign_id, "published", "Published. It shows on the dates you chose.", needs_premium=True)


@bp.post("/sponsors/campaign/<int:campaign_id>/pause")
@login_required
def campaign_pause(campaign_id):
    return _set_status(campaign_id, "paused", "Paused. It isn't shown anywhere until you resume it.", needs_premium=False)


@bp.post("/sponsors/campaign/<int:campaign_id>/resume")
@login_required
def campaign_resume(campaign_id):
    return _set_status(campaign_id, "published", "Resumed.", needs_premium=True)


@bp.post("/sponsors/campaign/<int:campaign_id>/archive")
@login_required
def campaign_archive(campaign_id):
    return _set_status(campaign_id, "archived", "Archived. It's kept for reports and never shown.", needs_premium=False)


@bp.post("/sponsors/campaign/<int:campaign_id>/duplicate")
@login_required
def campaign_duplicate(campaign_id):
    club = _manage()
    c = _campaign(club, campaign_id)
    today = sponsors.club_today(club.id)
    length = (c.ends_on - c.starts_on).days
    copy = SponsorCampaign(club_id=club.id, sponsor_id=c.sponsor_id, name=f"{c.name} (copy)"[:80], headline=c.headline, body=c.body,
                           cta_label=c.cta_label, cta_url=c.cta_url, starts_on=today, ends_on=today + timedelta(days=min(length, 365)),
                           status="draft", placements=list(c.placements or []), route_id=c.route_id, display_order=c.display_order,
                           is_default=False, value_cents=None, click_token=sponsors.new_token(), created_by=g.user.id)
    db.session.add(copy)
    db.session.commit()
    flash("Duplicated as a draft with fresh dates. Check the dates, then publish.", "ok")
    return _back("sponsors.campaign_edit", campaign_id=copy.id)


@bp.get("/sponsors/campaign/<int:campaign_id>/preview")
@login_required
def campaign_preview(campaign_id):
    club = _admin()
    c = _campaign(club, campaign_id)
    state = _premium(club)
    today = sponsors.club_today(club.id)
    panel = sponsors.panel_data(c, "preview", print_line="print" in (c.placements or []))
    return render_template("campaign_preview.html", club=club, camp=c, panel=panel, phase=sponsors.campaign_phase(c, today),
                           phase_text=sponsors.PHASE_TEXT, state=state, tz=sponsors.club_timezone(club.id).key,
                           sponsor=db.session.get(Sponsor, c.sponsor_id), notes=_notes(), preview_url=c.cta_url)


# ------------------------------------------------------------------ public: click redirect

@bp.get("/go/<token>")
def go(token):
    """Count an outbound click, then send the rider to the campaign's stored, validated address. The destination is never
    taken from the request, so this can't be used as an open redirect."""
    c = SponsorCampaign.query.filter_by(click_token=token).first() if 8 <= len(token) <= 40 else None
    where = request.args.get("p", "sheet")
    placement = "widget" if where == "widget" else "ride_sheet"
    if c is None or not c.cta_url or where not in ("sheet", "widget", "preview"):
        abort(404)
    live = [x for x in sponsors._live(c.club_id, placement, sponsors.utcnow()) if x.id == c.id]
    if not live:
        resp = Response(render_template("sponsor_ended.html"), status=410)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    try:
        safe = sponsors.clean_url(c.cta_url, "Address", required=True)
    except ValueError:
        abort(404)
    if where != "preview" and not sponsors.looks_automated(request) and ratelimit.allow("sponsor-click", request.remote_addr or "?", 60, 60):
        sponsors.count(c.club_id, c.id, placement, "click")
    resp = redirect(safe, code=302)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


# ------------------------------------------------------------------ website widget

@bp.get("/sponsors/embed")
@login_required
def embed_setup():
    club = _admin()
    emb = sponsors.active_embed(club.id)
    url = f"{_base_url()}/embed/{emb.token}/sponsor" if emb else None
    snippet = (f'<iframe src="{url}" title="Our club sponsor" width="100%" height="300" referrerpolicy="no-referrer" '
               f'style="border:0;max-width:760px;display:block"></iframe>') if url else None
    return render_template("sponsor_embed.html", club=club, emb=emb, url=url, snippet=snippet, state=_premium(club), notes=_notes())


@bp.post("/sponsors/embed/regenerate")
@login_required
def embed_regenerate():
    club = _manage()
    sponsors.regenerate_embed(club.id, g.user.id)
    db.session.commit()
    flash("New website widget address made. Any widget using the old address has stopped; paste the new code on your site.", "ok")
    return _back("sponsors.embed_setup")


@bp.post("/sponsors/embed/revoke")
@login_required
def embed_revoke():
    club = _admin()
    sponsors.revoke_embed(club.id)
    db.session.commit()
    flash("Website widget switched off. The old address no longer works.", "ok")
    return _back("sponsors.embed_setup")


@bp.get("/embed/<token>/sponsor")
def embed_public(token):
    """The sponsor strip for a club's own website. Public, no sign-in, no scripts, only approved public sponsor content."""
    emb = sponsors.embed_by_token(token)
    if emb is None:
        abort(404)
    now = sponsors.utcnow()
    c = sponsors.select_campaign(emb.club_id, "widget", now=now, rotation_key=sponsors.widget_rotation_key(token, emb.club_id, now))
    panel = sponsors.panel_data(c, "widget") if c else None
    if not sponsors.looks_automated(request):
        sponsors.count_widget_view(emb.club_id, panel, now)
    resp = Response(render_template("embed_public.html", panel=panel))
    resp.headers["Content-Security-Policy"] = ("default-src 'none'; img-src 'self'; style-src 'self'; base-uri 'none'; "
                                               "form-action 'none'; frame-ancestors *")
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Cache-Control"] = "public, max-age=300"       # a browser may reuse it for 5 minutes (those views aren't counted)
    resp.headers.pop("X-Frame-Options", None)
    return resp


# ------------------------------------------------------------------ reports

def _range():
    """Reports are for the club's admins only. Bad or missing dates fall back to the last 30 days."""
    club = _admin()
    today = sponsors.club_today(club.id)
    def parse(name):
        try:
            return date.fromisoformat(request.args.get(name, "")) if request.args.get(name) else None
        except ValueError:
            return None
    end = parse("end") or today
    start = parse("start") or end - timedelta(days=29)
    if end < start or (end - start).days > 800:
        end = today
        start = end - timedelta(days=29)
    return club, start, end


METRIC_LABEL = {"sheet_request": "Ride sheet requests", "widget_request": "Website widget requests",
                "delivery": "Panel deliveries (requests)", "click": "Recorded outbound clicks"}
LIMITATIONS = [
    "A request is not a person. One rider opening a sheet several times counts several times, and nothing here is a count of unique cyclists.",
    "A delivery means our server sent the sponsor panel with a page. It does not show that anyone saw it. These are not viewable impressions.",
    "Clicks are counted when a rider uses the sponsor button on a sheet or widget. They show interest, not purchases or sales.",
    "Link previews, prefetches and obvious bots are filtered out, but some automated visits may still be counted.",
    "A widget on your website may be kept by a browser for up to 5 minutes before it asks us again, so widget requests can be a little low. "
    "Ride sheets are always fetched fresh, but a copy a rider saved for offline use isn't counted when it is reopened.",
    "Days are in your club's timezone. Premium panels aren't shown (or counted) while premium is not active.",
]


@bp.get("/sponsors/reports")
@login_required
def reports():
    club, start, end = _range()
    rep = sponsors.report(club.id, start, end)
    return render_template("sponsor_reports.html", club=club, rep=rep, start=start, end=end, notes=_notes(),
                           limits=LIMITATIONS, tz=sponsors.club_timezone(club.id).key, state=_premium(club))


@bp.get("/sponsors/report")
@login_required
def report_print():
    club, start, end = _range()
    rep = sponsors.report(club.id, start, end)
    return render_template("sponsor_report_print.html", club=club, rep=rep, start=start, end=end, limits=LIMITATIONS,
                           tz=sponsors.club_timezone(club.id).key, price=sponsors.PRICE_LINE)


def _csv_cell(v):
    s = str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s       # a spreadsheet must never run a campaign name as a formula


@bp.get("/sponsors/reports.csv")
@login_required
def reports_csv():
    club, start, end = _range()
    camps = {c.id: c for c in SponsorCampaign.query.filter_by(club_id=club.id).all()}
    sps = {s.id: s for s in Sponsor.query.filter_by(club_id=club.id).all()}
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["date", "campaign", "sponsor", "placement", "metric", "count"])
    for r in sponsors.report(club.id, start, end)["raw"]:
        c = camps.get(r.campaign_id)
        w.writerow([r.day.isoformat(), _csv_cell(c.name) if c else "(all requests)",
                    _csv_cell(sps[c.sponsor_id].name) if c and c.sponsor_id in sps else "", r.placement,
                    METRIC_LABEL.get(r.kind, r.kind), r.count])
    resp = Response(out.getvalue(), mimetype="text/csv; charset=utf-8")
    resp.headers["Content-Disposition"] = f'attachment; filename="sponsor-report-{start.isoformat()}-to-{end.isoformat()}.csv"'
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ------------------------------------------------------------------ a ride leader picks a sponsor for one ride

@bp.put("/api/routes/<int:route_id>/sponsor")
@login_required
def ride_sponsor(route_id):
    """Ride leaders can choose among their club's approved campaigns, or publish without one. (The owner check on the route
    is done for every write by security.route_access.) Choosing never creates or edits a campaign."""
    from .plan import get_or_create_ride
    route = db.session.get(Route, route_id) or abort(404)
    if route.club_id is None:
        return jsonify(error="Join a club to use its sponsors."), 409
    body = request.get_json(silent=True) or {}
    mode = body.get("mode")
    ride = get_or_create_ride(route)
    if mode in ("auto", "none"):
        ride.sponsor_mode, ride.sponsor_campaign_id = mode, None
    elif mode == "campaign":
        try:
            cid = int(body.get("campaign_id"))
        except (TypeError, ValueError):
            return jsonify(error="Choose a sponsor campaign."), 400
        c = SponsorCampaign.query.filter_by(id=cid, club_id=route.club_id, status="published").first()
        if c is None or "ride_sheet" not in (c.placements or []) or not sponsors.premium_state(route.club_id)["serving"]:
            return jsonify(error="That campaign isn't available for ride sheets."), 400
        ride.sponsor_mode, ride.sponsor_campaign_id = "campaign", c.id
    else:
        return jsonify(error="Choose automatic, none or a campaign."), 400
    db.session.commit()
    return jsonify({"mode": ride.sponsor_mode, "campaign_id": ride.sponsor_campaign_id})


def sponsor_options(route):
    """Campaigns a leader may pick for this ride (approved, ride-sheet placement, not expired), for the Share tab."""
    if route.club_id is None or not sponsors.premium_state(route.club_id)["serving"]:
        return None
    today = sponsors.club_today(route.club_id)
    rows = (SponsorCampaign.query.join(Sponsor, Sponsor.id == SponsorCampaign.sponsor_id)
            .filter(SponsorCampaign.club_id == route.club_id, SponsorCampaign.status == "published", Sponsor.archived_at.is_(None))
            .order_by(SponsorCampaign.display_order, SponsorCampaign.id).all())
    return [{"id": c.id, "label": f"{c.name}"} for c in rows
            if "ride_sheet" in (c.placements or []) and sponsors.campaign_phase(c, today) in ("active", "scheduled")]


# ------------------------------------------------------------------ another run of the same ride

@bp.post("/routes/<int:src_id>/run-again")
@login_required
def run_again(src_id):
    """Make a new, dated ride from an earlier one. The earlier ride, its link and its history are not touched."""
    from .route_factory import run_again as clone
    from .security import can_see
    src = db.session.get(Route, src_id)
    if src is None or not can_see(src, g.user):
        abort(404)
    if src.source == "strava":
        flash("A route from Strava stays yours until you share it. Share it first, then run that copy again.", "error")
        return redirect(url_for("main.show", route_id=src.id))
    chosen = None
    if request.form.get("date"):
        try:
            chosen = date.fromisoformat(request.form["date"])
        except ValueError:
            flash("Pick a valid date.", "error")
            return redirect(url_for("main.show", route_id=src.id))
    new = clone(src, g.user, g.club.id if g.get("club") else None, chosen)
    db.session.commit()
    flash("A new run of this ride is ready. The earlier ride and its link are unchanged.", "ok")
    return redirect(url_for("main.show", route_id=new.id))
