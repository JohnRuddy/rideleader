"""Clubs, membership and invite codes.

Who may do what:
- The platform admin (User.role == 'admin') creates and renames clubs, and can see and manage every club.
- A club admin (Membership.role == 'admin') invites people to their club, removes them and promotes other members.
- A member shares routes, stops and closures with the rest of that club.

Signing in the first time needs an invite code that belongs to a club; redeeming it makes the person a member. The
platform admin signs in without one. A person with no club (removed, or never invited) cannot stay signed in.
Codes are shown once when they are made: only a hash is stored."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from flask import g
from sqlalchemy import text

from . import db
from .models import Club, Closure, ClubStop, Invite, Membership, Route, User

SAMPLE_CLUB_NAME = "Velo Club"
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"        # no 0/O/1/I: people read these out over the phone
CODE_LEN = 10
DEFAULT_INVITE_DAYS = 30
ROLES = ("member", "admin")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ the sample club

def seed_default_club() -> None:
    """Make sure a sample club exists, and (once) move the single-club data of earlier versions into it."""
    club = db.session.get(Club, 1)
    if club is None:
        if Club.query.count() > 0:
            return                      # clubs exist and the sample one was deleted on purpose: leave it gone
        club = Club(id=1, name=SAMPLE_CLUB_NAME, settings={}, created_at=_now())
        db.session.add(club)
        db.session.flush()
    if not club.name:
        club.name = SAMPLE_CLUB_NAME
    if club.created_at is None:
        club.created_at = _now()
    # the club table may already hold id 1 from before sequences were involved: keep new clubs from colliding with it
    db.session.execute(text("SELECT setval(pg_get_serial_sequence('club', 'id'), GREATEST((SELECT MAX(id) FROM club), 1))"))
    settings = dict(club.settings or {})
    if not settings.get("_migrated_to_clubs"):
        db.session.execute(text("UPDATE route SET club_id = 1 WHERE club_id IS NULL AND owner_id IS NOT NULL"))
        db.session.execute(text("UPDATE club_stop SET club_id = 1 WHERE club_id IS NULL"))
        db.session.execute(text("UPDATE closure SET club_id = 1 WHERE club_id IS NULL"))
        # everyone who already has an account was let in before invite codes existed: keep them in, in the sample club
        for person in User.query.all():
            if Membership.query.filter_by(user_id=person.id, club_id=1).first() is None:
                db.session.add(Membership(user_id=person.id, club_id=1, role="admin" if person.role == "admin" else "member"))
        settings["_migrated_to_clubs"] = True
        club.settings = settings
    db.session.commit()


# ------------------------------------------------------------------ who belongs where

def is_platform_admin(user) -> bool:
    return bool(user and user.role == "admin")


def clubs_for(user) -> list[Club]:
    """The clubs a person can act in. The platform admin can act in every club."""
    if user is None:
        return []
    if is_platform_admin(user):
        return Club.query.order_by(Club.name, Club.id).all()
    return (Club.query.join(Membership, Membership.club_id == Club.id)
            .filter(Membership.user_id == user.id).order_by(Club.name, Club.id).all())


def membership(user, club_id: int) -> Membership | None:
    if user is None or club_id is None:
        return None
    return Membership.query.filter_by(user_id=user.id, club_id=club_id).first()


def has_any_club(user) -> bool:
    return bool(user) and (is_platform_admin(user) or Membership.query.filter_by(user_id=user.id).first() is not None)


def is_club_admin(user, club_id: int | None) -> bool:
    if is_platform_admin(user):
        return True
    m = membership(user, club_id)
    return bool(m and m.role == "admin")


def current_club_id() -> int | None:
    club = g.get("club")
    return club.id if club else None


def scoped(query, model):
    """Restrict a club-owned table (stops, closures) to the current club. With no club (older data, tests) it
    shows the rows that belong to none."""
    cid = current_club_id()
    return query.filter(model.club_id == cid) if cid else query.filter(model.club_id.is_(None))


# ------------------------------------------------------------------ membership changes

def add_member(club_id: int, user: User, role: str = "member", invited_by: int | None = None) -> Membership:
    m = membership(user, club_id)
    if m is None:
        m = Membership(user_id=user.id, club_id=club_id, role=role if role in ROLES else "member", invited_by=invited_by)
        db.session.add(m)
    elif role == "admin" and m.role != "admin":
        m.role = "admin"                                 # an admin code upgrades; a member code never demotes
    return m


def admin_count(club_id: int) -> int:
    return Membership.query.filter_by(club_id=club_id, role="admin").count()


def set_role(club_id: int, user_id: int, role: str) -> None:
    m = Membership.query.filter_by(club_id=club_id, user_id=user_id).first()
    if m is None or role not in ROLES:
        raise ValueError("That person isn't in this club.")
    if m.role == "admin" and role != "admin" and admin_count(club_id) <= 1:
        raise ValueError("A club needs at least one admin. Make someone else an admin first.")
    m.role = role


def remove_member(club_id: int, user_id: int) -> None:
    m = Membership.query.filter_by(club_id=club_id, user_id=user_id).first()
    if m is None:
        raise ValueError("That person isn't in this club.")
    if m.role == "admin" and admin_count(club_id) <= 1:
        raise ValueError("A club needs at least one admin. Make someone else an admin before removing this one.")
    db.session.delete(m)


# ------------------------------------------------------------------ invite codes

def new_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
    return f"{raw[:5]}-{raw[5:]}"


def normalise(code: str | None) -> str:
    return "".join(c for c in (code or "").upper() if c.isalnum())


def hash_code(code: str) -> str:
    return hashlib.sha256(normalise(code).encode()).hexdigest()


def create_invite(club_id: int, created_by: int | None, *, role: str = "member", label: str | None = None,
                  email: str | None = None, max_uses: int | None = None, days: int = DEFAULT_INVITE_DAYS) -> tuple[Invite, str]:
    if role not in ROLES:
        raise ValueError("Choose member or admin.")
    if not 1 <= days <= 365:
        raise ValueError("A code can last from 1 to 365 days.")
    if max_uses is not None and not 1 <= max_uses <= 10_000:
        raise ValueError("Uses must be between 1 and 10,000.")
    code = new_code()
    inv = Invite(club_id=club_id, code_hash=hash_code(code), hint=normalise(code)[-3:], label=(label or "").strip()[:80] or None,
                 role=role, email=(email or "").strip().lower() or None, max_uses=max_uses,
                 expires_at=_now() + timedelta(days=days), created_by=created_by)
    db.session.add(inv)
    db.session.flush()
    return inv, code


def is_usable(inv: Invite, email: str | None = None) -> bool:
    if inv.revoked_at is not None or inv.expires_at <= _now():
        return False
    if inv.max_uses is not None and inv.uses >= inv.max_uses:
        return False
    if inv.email and (email or "").strip().lower() != inv.email:
        return False
    return True


def lookup_invite(code: str | None, email: str | None = None) -> Invite | None:
    """The invite for a code, if it exists and can still be used by this email address; otherwise None."""
    if len(normalise(code)) != CODE_LEN:
        return None
    inv = Invite.query.filter_by(code_hash=hash_code(code)).first()
    return inv if inv and is_usable(inv, email) else None


def redeem(user: User, inv: Invite) -> Membership:
    """Join the invite's club. Counts as a use only when it actually adds someone."""
    already = membership(user, inv.club_id) is not None
    m = add_member(inv.club_id, user, inv.role, invited_by=inv.created_by)
    if not already:
        inv.uses += 1
    return m


def revoke(inv: Invite) -> None:
    inv.revoked_at = _now()


# ------------------------------------------------------------------ clubs themselves

def create_club(name: str, creator: User | None = None) -> Club:
    name = (name or "").strip()
    if not 2 <= len(name) <= 120:
        raise ValueError("Give the club a name of 2 to 120 characters.")
    if Club.query.filter(db.func.lower(Club.name) == name.lower()).first():
        raise ValueError("There is already a club with that name.")
    club = Club(name=name, settings={}, created_at=_now())
    db.session.add(club)
    db.session.flush()
    if creator is not None:
        add_member(club.id, creator, "admin")
    return club


def rename_club(club: Club, name: str) -> None:
    name = (name or "").strip()
    if not 2 <= len(name) <= 120:
        raise ValueError("Give the club a name of 2 to 120 characters.")
    clash = Club.query.filter(db.func.lower(Club.name) == name.lower(), Club.id != club.id).first()
    if clash:
        raise ValueError("There is already a club with that name.")
    club.name = name


def club_counts(club_id: int) -> dict:
    return {"members": Membership.query.filter_by(club_id=club_id).count(),
            "routes": Route.query.filter_by(club_id=club_id).count(),
            "stops": ClubStop.query.filter_by(club_id=club_id, active=True).count(),
            "closures": Closure.query.filter_by(club_id=club_id).count()}
