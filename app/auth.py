"""Passwordless sign-in: an emailed one-time link. No passwords are stored.

The emailed link opens a page with a button rather than signing in on GET, because mail security scanners
fetch links and would burn the one-time token before the person clicks it."""
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, current_app, g, redirect, render_template, request, session, url_for

from . import clubs, db, ratelimit
from .mailer import MailError, get_mailer, mail_is_real
from .models import Invite, LoginToken, User

bp = Blueprint("auth", __name__)

TOKEN_TTL = timedelta(minutes=15)
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _safe_next(target: str | None) -> str:
    """Only ever redirect to a path on this site."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("main.index")


def _client_ip() -> str:
    return request.remote_addr or "?"


def issue_login_link(email: str, nxt: str = "", invite: Invite | None = None) -> str:
    """Create a one-time sign-in token for `email` and return the full link. Does not send anything."""
    email = email.strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise ValueError("That doesn't look like an email address.")
    token = secrets.token_urlsafe(32)
    db.session.add(LoginToken(email=email, token_hash=_hash(token), expires_at=datetime.now(timezone.utc) + TOKEN_TTL,
                              invite_id=invite.id if invite else None))
    db.session.commit()
    base = (current_app.config["APP_BASE_URL"] or "http://localhost").rstrip("/")
    return f"{base}{url_for('auth.verify', token=token)}" + (f"?next={nxt}" if nxt else "")


@bp.get("/login")
def login():
    if g.user:
        return redirect(_safe_next(request.args.get("next")))
    return render_template("login.html", next=request.args.get("next", ""), code=request.args.get("code", ""))


def _bootstrap_open() -> bool:
    """With no users at all, the first person in becomes the platform admin without a code (development and tests;
    on a real server the first admin comes from the login-link command)."""
    return User.query.count() == 0 and current_app.config.get("RLK_ENV") != "production"


@bp.post("/login")
def login_request():
    email = (request.form.get("email") or "").strip().lower()
    nxt = request.form.get("next", "")
    code = (request.form.get("code") or "").strip()

    def again(msg, status=400):
        return render_template("login.html", error=msg, next=nxt, code=code, email=email), status

    if not EMAIL_RE.match(email) or len(email) > 254:
        return again("That doesn't look like an email address.")
    ident_ok = ratelimit.allow("login-ip", _client_ip(), 10, 900) and \
        ratelimit.allow("login-email", hashlib.sha256(email.encode()).hexdigest(), 5, 900)
    if not ident_ok:
        return again("Too many sign-in requests. Wait a few minutes and try again.", 429)
    if current_app.config.get("RLK_ENV") == "production" and not mail_is_real():
        current_app.logger.error("Sign-in requested but SMTP_HOST is not set; no email can be sent")
        return again("Sign-in email isn't set up on this server yet. Please tell the site owner.", 503)

    invite = None
    if code:
        # guessing codes is the one thing worth throttling hard
        if not ratelimit.allow("invite-ip", _client_ip(), 12, 900):
            return again("Too many invite code attempts. Wait a few minutes and try again.", 429)
        invite = clubs.lookup_invite(code, email)
        if invite is None:
            return again("That invite code isn't valid, has expired or has been used up. Ask your club admin for a new one.")

    user = User.query.filter_by(email=email).first()
    allowed = (not current_app.config.get("INVITE_REQUIRED", True) or invite is not None
               or (user is not None and clubs.has_any_club(user)) or _bootstrap_open())
    link = None
    if allowed:
        link = issue_login_link(email, nxt, invite)
        try:
            get_mailer().send(email, "Your Ride Leader Kit sign-in link",
                              f"Open this link to sign in (it works once and expires in 15 minutes):\n\n{link}\n\n"
                              "If you didn't ask for this, ignore this email.")
        except MailError:
            current_app.logger.error("Sign-in email to a user could not be sent")
            return again("We couldn't send the email just now. Please try again in a minute.", 503)
    # Not in any club and no code: the same page as everyone else and nothing sent, so addresses can't be probed.
    # Development convenience only: with no email service the link is shown. In production it goes by email and
    # nowhere else, because showing it would let anyone sign in as any address they type.
    show = link if (link and current_app.config.get("DEV_SHOW_LOGIN_LINK") and not current_app.config.get("SMTP_HOST")
                    and current_app.config.get("RLK_ENV") != "production") else None
    return render_template("login_sent.html", email=email, dev_link=show)


def _valid(token: str) -> LoginToken | None:
    row = LoginToken.query.filter_by(token_hash=_hash(token)).first()
    if row is None or row.used_at is not None or row.expires_at <= datetime.now(timezone.utc):
        return None
    return row


@bp.get("/login/<token>")
def verify(token):
    if _valid(token) is None:
        return render_template("login.html", error="That sign-in link has expired or was already used. Ask for a new one.",
                               next=""), 400
    return render_template("login_confirm.html", token=token, next=request.args.get("next", ""))


@bp.post("/login/<token>")
def verify_post(token):
    row = _valid(token)
    if row is None:
        return render_template("login.html", error="That sign-in link has expired or was already used. Ask for a new one.",
                               next=""), 400
    now = datetime.now(timezone.utc)
    invite = db.session.get(Invite, row.invite_id) if row.invite_id else None
    if invite is not None and not clubs.is_usable(invite, row.email):
        return render_template("login.html", error="That invite code has expired or been used up since you asked for the link. "
                               "Ask your club admin for a new one.", next="", code="", email=row.email), 400
    user = User.query.filter_by(email=row.email).first()
    if user is None:
        user = User(email=row.email, role="admin" if User.query.count() == 0 else "leader")   # the first person in runs the platform
        db.session.add(user)
        db.session.flush()
    row.used_at = now
    chosen_club = None
    if invite is not None:
        clubs.redeem(user, invite)
        chosen_club = invite.club_id
    user.last_login_at = now
    db.session.commit()
    session.clear()                                  # new session, new CSRF token: no session fixation
    session["uid"] = user.id
    if chosen_club:
        session["club_id"] = chosen_club
    session.permanent = True
    return redirect(_safe_next(request.args.get("next") or request.form.get("next")))


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("main.index"))
