"""Passwordless sign-in: an emailed one-time link. No passwords are stored.

The emailed link opens a page with a button rather than signing in on GET, because mail security scanners
fetch links and would burn the one-time token before the person clicks it."""
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, current_app, g, redirect, render_template, request, session, url_for

from . import db, ratelimit
from .mailer import MailError, get_mailer, mail_is_real
from .models import LoginToken, User

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


def issue_login_link(email: str, nxt: str = "") -> str:
    """Create a one-time sign-in token for `email` and return the full link. Does not send anything."""
    email = email.strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise ValueError("That doesn't look like an email address.")
    token = secrets.token_urlsafe(32)
    db.session.add(LoginToken(email=email, token_hash=_hash(token), expires_at=datetime.now(timezone.utc) + TOKEN_TTL))
    db.session.commit()
    base = (current_app.config["APP_BASE_URL"] or "http://localhost").rstrip("/")
    return f"{base}{url_for('auth.verify', token=token)}" + (f"?next={nxt}" if nxt else "")


@bp.get("/login")
def login():
    if g.user:
        return redirect(_safe_next(request.args.get("next")))
    return render_template("login.html", next=request.args.get("next", ""))


@bp.post("/login")
def login_request():
    email = (request.form.get("email") or "").strip().lower()
    nxt = request.form.get("next", "")
    if not EMAIL_RE.match(email) or len(email) > 254:
        return render_template("login.html", error="That doesn't look like an email address.", next=nxt), 400
    ident_ok = ratelimit.allow("login-ip", _client_ip(), 10, 900) and \
        ratelimit.allow("login-email", hashlib.sha256(email.encode()).hexdigest(), 5, 900)
    if not ident_ok:
        return render_template("login.html", error="Too many sign-in requests. Wait a few minutes and try again.", next=nxt), 429
    if current_app.config.get("RLK_ENV") == "production" and not mail_is_real():
        current_app.logger.error("Sign-in requested but SMTP_HOST is not set; no email can be sent")
        return render_template("login.html", error="Sign-in email isn't set up on this server yet. Please tell the site owner.",
                               next=nxt), 503
    token = secrets.token_urlsafe(32)
    db.session.add(LoginToken(email=email, token_hash=_hash(token), expires_at=datetime.now(timezone.utc) + TOKEN_TTL))
    db.session.commit()
    base = (current_app.config["APP_BASE_URL"] or request.url_root).rstrip("/")
    link = f"{base}{url_for('auth.verify', token=token)}" + (f"?next={nxt}" if nxt else "")
    try:
        get_mailer().send(email, "Your Ride Leader Kit sign-in link",
                          f"Open this link to sign in (it works once and expires in 15 minutes):\n\n{link}\n\n"
                          "If you didn't ask for this, ignore this email.")
    except MailError:
        current_app.logger.error("Sign-in email to a user could not be sent")
        return render_template("login.html", error="We couldn't send the email just now. Please try again in a minute.",
                               next=nxt), 503
    # the same page whether or not the address is known: no account enumeration
    # Development convenience only. In production the link goes by email and nowhere else: showing it on the page
    # would let anyone sign in as any address they type.
    show = link if (current_app.config.get("DEV_SHOW_LOGIN_LINK") and not current_app.config.get("SMTP_HOST")
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
    row.used_at = now
    user = User.query.filter_by(email=row.email).first()
    if user is None:
        user = User(email=row.email, role="admin" if User.query.count() == 0 else "leader")   # the first person in runs the club
        db.session.add(user)
    user.last_login_at = now
    db.session.commit()
    session.clear()                                  # new session, new CSRF token: no session fixation
    session["uid"] = user.id
    session.permanent = True
    return redirect(_safe_next(request.args.get("next") or request.form.get("next")))


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("main.index"))
