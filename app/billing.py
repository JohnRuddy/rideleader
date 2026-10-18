"""How a club's premium is switched on and off. This file never talks to a payment service.

Two things write it, and nothing else does:

* the site admin, by hand (`activate` below): records that a club paid some other way (bank transfer, invoice) and sets its period;
* verified Stripe events, or our own server asking Stripe (`app/stripe_billing.py` -> `activate_until`): premium runs to the
  end of the period Stripe has been paid for.

Either way a club is only ever marked paid by a person, or by a signature-verified provider event, saying so. Everything else
(campaigns, serving, expiry, reports) reads `sponsors.premium_state`. `PRICE_CENTS` in `sponsors.py` is the single source of the price.
"""
from datetime import timedelta

from . import db, sponsors
from .models import ClubPremium


def activate(club_id: int, *, by_user_id: int | None, months: int = 12, source: str = "manual", reference: str | None = None,
             note: str | None = None, now=None) -> ClubPremium:
    """Start (or extend) a club's premium by `months`. Extends from the current end if it is still running."""
    now = now or sponsors.utcnow()
    p = ClubPremium.query.filter_by(club_id=club_id).first()
    days = int(round(months * 30.4375))
    if p is None:
        p = ClubPremium(club_id=club_id, starts_at=now, expires_at=now + timedelta(days=days))
        db.session.add(p)
    else:
        running = p.status == "active" and p.expires_at > now
        p.starts_at = p.starts_at if running else now
        p.expires_at = (p.expires_at if running else now) + timedelta(days=days)
    p.status, p.cancelled_at = "active", None
    p.plan, p.price_cents, p.currency = "club_sponsor_annual", sponsors.PRICE_CENTS, "EUR"
    p.source, p.external_ref, p.note, p.activated_by = source, (reference or None), (note or None), by_user_id
    db.session.flush()
    return p


def activate_until(club_id: int, *, starts_at, expires_at, source: str = "billing", reference: str | None = None,
                   note: str | None = None, now=None) -> ClubPremium:
    """Make premium run to `expires_at` (the end of a period that has been paid for). Never shortens a period already paid
    for, so replaying an old event or receiving events out of order changes nothing. A cancelled club that pays again is
    switched back on."""
    now = now or sponsors.utcnow()
    starts_at = min(starts_at, now)                    # paid for: it starts now, not at some later moment on the invoice
    p = ClubPremium.query.filter_by(club_id=club_id).first()
    if p is None:
        p = ClubPremium(club_id=club_id, starts_at=starts_at, expires_at=expires_at)
        db.session.add(p)
    else:
        running = p.status == "active" and p.expires_at > now
        if running:
            p.expires_at = max(p.expires_at, expires_at)
        else:
            p.starts_at, p.expires_at = starts_at, expires_at
    p.status, p.cancelled_at = "active", None
    p.plan, p.currency = "club_sponsor_annual", "EUR"
    p.price_cents = sponsors.PRICE_CENTS
    p.source, p.external_ref, p.note = source, (reference or None), (note or None)
    db.session.flush()
    return p


def cancel(club_id: int, *, now=None) -> ClubPremium | None:
    """Stop serving straight away (a refund or a mistake). Campaigns and reports are kept."""
    p = ClubPremium.query.filter_by(club_id=club_id).first()
    if p is not None:
        p.status, p.cancelled_at = "cancelled", now or sponsors.utcnow()
        db.session.flush()
    return p
