"""Clubs, members and invite codes: the pages where the platform admin sets clubs up and club admins invite people.

- Platform admin: create, rename and delete clubs, and see every club.
- Club admin: make invite codes (shown once), revoke them, promote or remove members.
- Any member: see their club and who is in it, and switch between their clubs."""
from datetime import timezone

from flask import Blueprint, abort, current_app, flash, g, get_flashed_messages, redirect, render_template, request, session, url_for

from . import clubs, db
from .models import Club, Invite, Membership, Route, User
from .security import login_required

bp = Blueprint("clubadmin", __name__)


def _club(club_id: int) -> Club:
    """A club the signed-in person belongs to (the platform admin: any club); others get a 404, not a hint it exists."""
    club = db.session.get(Club, club_id) or abort(404)
    if not (clubs.is_platform_admin(g.user) or clubs.membership(g.user, club.id)):
        abort(404)
    return club


def _admin_of(club_id: int) -> Club:
    club = _club(club_id)
    if not clubs.is_club_admin(g.user, club.id):
        abort(403, description="Only a club admin can do that.")
    return club


def _back(club_id: int):
    return redirect(url_for("clubadmin.manage", club_id=club_id))


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@bp.get("/clubs")
@login_required
def index():
    rows = []
    for c in g.clubs:
        counts = clubs.club_counts(c.id)
        rows.append({"club": c, "counts": counts, "admin": clubs.is_club_admin(g.user, c.id)})
    return render_template("clubs.html", rows=rows, notes=get_flashed_messages(with_categories=True))


@bp.post("/clubs")
@login_required
def create():
    if not clubs.is_platform_admin(g.user):
        abort(403, description="Only the site admin can set up a new club.")
    try:
        club = clubs.create_club(request.form.get("name", ""), g.user)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("clubadmin.index"))
    db.session.commit()
    session["club_id"] = club.id
    flash(f"{club.name} is set up. Make an invite code below to bring its first people in.", "ok")
    return _back(club.id)


@bp.post("/clubs/switch")
@login_required
def switch():
    club_id = _int(request.form.get("club_id"))
    if club_id in {c.id for c in g.clubs}:
        session["club_id"] = club_id
    nxt = request.form.get("next", "")
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("main.index"))


@bp.get("/clubs/<int:club_id>")
@login_required
def manage(club_id):
    club = _club(club_id)
    admin = clubs.is_club_admin(g.user, club.id)
    members = (db.session.query(Membership, User).join(User, User.id == Membership.user_id)
               .filter(Membership.club_id == club.id).order_by(Membership.role, User.email).all())
    invites = (Invite.query.filter_by(club_id=club.id).order_by(Invite.created_at.desc()).limit(50).all()) if admin else []
    fresh = session.pop("new_invite", None)
    if fresh and fresh.get("club_id") != club.id:
        fresh = None
    base = (current_app.config["APP_BASE_URL"] or request.url_root).rstrip("/")
    if fresh:
        fresh["link"] = f"{base}{url_for('auth.login', code=fresh['code'])}"
    return render_template("club_manage.html", club=club, admin=admin, members=members, invites=invites, fresh=fresh,
                           counts=clubs.club_counts(club.id), platform_admin=clubs.is_platform_admin(g.user),
                           notes=get_flashed_messages(with_categories=True), now=clubs._now(), tz=timezone.utc)


@bp.post("/clubs/<int:club_id>/rename")
@login_required
def rename(club_id):
    club = _admin_of(club_id)
    try:
        clubs.rename_club(club, request.form.get("name", ""))
        db.session.commit()
        flash("Club renamed.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return _back(club.id)


@bp.post("/clubs/<int:club_id>/invites")
@login_required
def invite(club_id):
    club = _admin_of(club_id)
    f = request.form
    try:
        inv, code = clubs.create_invite(
            club.id, g.user.id, role=f.get("role", "member"), label=f.get("label"), email=f.get("email"),
            max_uses=_int(f.get("max_uses")) if f.get("max_uses", "").strip() else None,
            days=_int(f.get("days"), clubs.DEFAULT_INVITE_DAYS))
    except ValueError as e:
        flash(str(e), "error")
        return _back(club.id)
    db.session.commit()
    session["new_invite"] = {"club_id": club.id, "code": code, "role": inv.role, "email": inv.email,
                             "expires": inv.expires_at.strftime("%d %b %Y"), "max_uses": inv.max_uses}
    return _back(club.id)


@bp.post("/clubs/<int:club_id>/invites/<int:invite_id>/revoke")
@login_required
def revoke(club_id, invite_id):
    club = _admin_of(club_id)
    inv = db.session.get(Invite, invite_id)
    if inv is None or inv.club_id != club.id:
        abort(404)
    clubs.revoke(inv)
    db.session.commit()
    flash("That code no longer works.", "ok")
    return _back(club.id)


@bp.post("/clubs/<int:club_id>/members/<int:user_id>/role")
@login_required
def member_role(club_id, user_id):
    club = _admin_of(club_id)
    try:
        clubs.set_role(club.id, user_id, request.form.get("role", ""))
        db.session.commit()
        flash("Role updated.", "ok")
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "error")
    return _back(club.id)


@bp.post("/clubs/<int:club_id>/members/<int:user_id>/remove")
@login_required
def member_remove(club_id, user_id):
    club = _admin_of(club_id)
    try:
        clubs.remove_member(club.id, user_id)
        db.session.commit()
        flash("Removed from the club. They can no longer open its routes, stops or closures.", "ok")
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "error")
    return _back(club.id)


@bp.post("/clubs/<int:club_id>/leave")
@login_required
def leave(club_id):
    club = _club(club_id)
    try:
        clubs.remove_member(club.id, g.user.id)
        db.session.commit()
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "error")
        return _back(club.id)
    session.pop("club_id", None)
    return redirect(url_for("clubadmin.index") if clubs.has_any_club(g.user) else url_for("main.index"))


@bp.post("/clubs/<int:club_id>/delete")
@login_required
def delete(club_id):
    if not clubs.is_platform_admin(g.user):
        abort(403, description="Only the site admin can delete a club.")
    club = db.session.get(Club, club_id) or abort(404)
    if (request.form.get("confirm_name") or "").strip() != club.name:
        flash("Type the club's name exactly to delete it.", "error")
        return _back(club.id)
    name = club.name
    Route.query.filter_by(club_id=club.id).update({"visibility": "private"})    # unshared, not thrown open to everyone
    db.session.delete(club)           # its members' links, invites, stops and closures go with it; routes stay with their owners
    db.session.commit()
    session.pop("club_id", None)
    flash(f"{name} was deleted.", "ok")
    return redirect(url_for("clubadmin.index"))
