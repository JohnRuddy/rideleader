"""The one place a payment provider plugs in. Nothing in this file takes money or talks to a payment service.

Today premium is switched on by hand: a platform admin records that a club has paid (an invoice number, a bank
transfer, a card payment made elsewhere) and sets its period. That keeps the rule honest: a club is only ever
marked paid by a person (or, later, a verified provider event) saying so.

To add online billing later, implement `handle_provider_event` for the provider you choose (for example Stripe):
verify the provider's signature, find the club from your own record of the checkout (never from a value a browser
sends), and call `activate` or `cancel` below with the provider's reference. Keep `PRICE_CENTS` in `sponsors.py` the
single source of the price. Everything else (campaigns, serving, expiry, reports) already reads `premium_state`.
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


def cancel(club_id: int, *, now=None) -> ClubPremium | None:
    """Stop serving straight away (a refund or a mistake). Campaigns and reports are kept."""
    p = ClubPremium.query.filter_by(club_id=club_id).first()
    if p is not None:
        p.status, p.cancelled_at = "cancelled", now or sponsors.utcnow()
        db.session.flush()
    return p


def handle_provider_event(payload: bytes, signature: str) -> None:
    """Placeholder for a provider's webhook. Deliberately unimplemented: a real one must verify the signature first."""
    raise NotImplementedError("No payment provider is connected. Activate premium by hand from the Sponsors page (platform admin).")
