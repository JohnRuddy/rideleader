"""Card billing for Club Sponsor Publishing, through Stripe (a yearly subscription per club).

The rules, in the order they matter:

* A club is marked paid only from a Stripe event whose signature verifies against STRIPE_WEBHOOK_SECRET, or from our own
  server asking Stripe directly (never from a value in the browser). The redirect after Checkout changes nothing by itself.
* Premium runs to the end of the period Stripe has been *paid* for (`activate_until` takes the later of the old and new end,
  so replays and out-of-order events can't shorten or double it). A failed renewal simply doesn't extend it; a cancelled
  subscription keeps premium until the paid period ends; a full refund cancels it.
* The club is found from our own records (the Stripe customer we linked, or the id we put on the Checkout Session), never
  from anything the visitor typed.
* Invoices and receipts are Stripe's documents. We keep their headline facts and links so a club admin can always download them.

Stripe is reached only through a `StripeGateway`, so tests use a fake and never touch the network.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from flask import current_app
from sqlalchemy.exc import IntegrityError

from . import billing, db, sponsors
from .models import BillingEvent, BillingInvoice, ClubBilling, ClubPremium

log = logging.getLogger(__name__)

TOLERANCE_SECONDS = 300
LIVE_SUB_STATUSES = {"active", "trialing", "past_due"}
EVENTS_WE_USE = ["checkout.session.completed", "invoice.paid", "invoice.payment_failed", "invoice.finalized", "invoice.voided",
                 "invoice.marked_uncollectible", "customer.subscription.created", "customer.subscription.updated",
                 "customer.subscription.deleted", "charge.refunded"]


class WebhookVerificationError(Exception):
    pass


class RetryLater(Exception):
    """An event we can't place yet (its checkout hasn't been linked). Stripe will send it again."""


# ------------------------------------------------------------------ the gateway to Stripe

def _plain(obj):
    """A Stripe response as plain dicts and lists."""
    if hasattr(obj, "to_dict"):
        obj = obj.to_dict()
    return json.loads(json.dumps(obj, default=str))


class StripeGateway:
    """The only code that talks to Stripe. Replace it in tests with a fake (config['STRIPE_CLIENT'])."""

    def __init__(self, secret_key: str):
        self.key = secret_key

    def create_checkout(self, *, club_id: int, club_name: str, customer_id: str | None, email: str | None, success_url: str,
                        cancel_url: str, price_id: str, tax: bool, tax_behavior: str) -> dict:
        import stripe
        if price_id:
            item = {"price": price_id, "quantity": 1}
        else:
            item = {"quantity": 1, "price_data": {
                "currency": "eur", "unit_amount": sponsors.PRICE_CENTS, "recurring": {"interval": "year"}, "tax_behavior": tax_behavior,
                "product_data": {"name": "Club Sponsor Publishing", "description": f"Ride Leader Kit, one year, for {club_name}"}}}
        params = dict(
            api_key=self.key, mode="subscription", line_items=[item], client_reference_id=str(club_id),
            metadata={"club_id": str(club_id)}, subscription_data={"metadata": {"club_id": str(club_id)}},
            billing_address_collection="required", tax_id_collection={"enabled": True}, allow_promotion_codes=True,
            automatic_tax={"enabled": bool(tax)}, success_url=success_url, cancel_url=cancel_url)
        if customer_id:
            params.update(customer=customer_id, customer_update={"name": "auto", "address": "auto"})
        elif email:
            params.update(customer_email=email)
        return _plain(stripe.checkout.Session.create(**params))

    def retrieve_session(self, session_id: str) -> dict:
        import stripe
        return _plain(stripe.checkout.Session.retrieve(session_id, api_key=self.key))

    def retrieve_subscription(self, subscription_id: str) -> dict:
        import stripe
        return _plain(stripe.Subscription.retrieve(subscription_id, api_key=self.key))

    def list_invoices(self, customer_id: str, limit: int = 24) -> list[dict]:
        import stripe
        return _plain(stripe.Invoice.list(customer=customer_id, limit=limit, api_key=self.key)).get("data", [])

    def portal_url(self, customer_id: str, return_url: str) -> str:
        import stripe
        return _plain(stripe.billing_portal.Session.create(customer=customer_id, return_url=return_url, api_key=self.key))["url"]

    def receipt_url(self, invoice: dict) -> str | None:
        """The card receipt for a paid invoice, when Stripe will tell us. Best effort: the invoice's own page also carries it."""
        import stripe
        try:
            charge = invoice.get("charge")
            if not charge:
                pays = _plain(stripe.InvoicePayment.list(invoice=invoice["id"], expand=["data.payment.payment_intent"], api_key=self.key))
                for p in pays.get("data", []):
                    pi = ((p.get("payment") or {}).get("payment_intent")) or {}
                    charge = pi.get("latest_charge") or charge
            return _plain(stripe.Charge.retrieve(charge, api_key=self.key)).get("receipt_url") if charge else None
        except Exception as e:                                     # never let a missing receipt break billing
            log.info("no receipt url for %s: %s", invoice.get("id"), e)
            return None


def get_gateway(config) -> StripeGateway | None:
    if config.get("STRIPE_CLIENT"):
        return config["STRIPE_CLIENT"]
    if config.get("STRIPE_SECRET_KEY"):
        return StripeGateway(config["STRIPE_SECRET_KEY"])
    return None


def enabled(config=None) -> bool:
    return get_gateway(config or current_app.config) is not None


# ------------------------------------------------------------------ webhook signature

def verify_webhook(payload: bytes, signature: str | None, secret: str) -> dict:
    import stripe
    if not secret:
        raise WebhookVerificationError("Webhook secret not configured")
    if not signature:
        raise WebhookVerificationError("Missing Stripe-Signature header")
    try:
        stripe.WebhookSignature.verify_header(payload.decode("utf-8"), signature, secret, TOLERANCE_SECONDS)
        return json.loads(payload)
    except (stripe.SignatureVerificationError, UnicodeDecodeError, ValueError) as exc:
        raise WebhookVerificationError(str(exc)) from exc


# ------------------------------------------------------------------ small helpers

def _dt(epoch) -> datetime | None:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc) if epoch else None


def _club_id_from_meta(obj: dict) -> int | None:
    sources = [obj.get("metadata"), (obj.get("subscription_details") or {}).get("metadata"),
               ((obj.get("parent") or {}).get("subscription_details") or {}).get("metadata")]
    for m in sources:
        try:
            if m and m.get("club_id"):
                return int(m["club_id"])
        except (TypeError, ValueError):
            continue
    return None


def _subscription_id(obj: dict) -> str | None:
    sub = obj.get("subscription") or ((obj.get("parent") or {}).get("subscription_details") or {}).get("subscription")
    return sub if isinstance(sub, str) else (sub or {}).get("id") if sub else None


def _club_for(obj: dict, gateway=None) -> int | None:
    """Our own record first (the customer we linked); only then the id we put on the objects we created."""
    cid = obj.get("customer")
    if isinstance(cid, dict):
        cid = cid.get("id")
    if cid:
        row = ClubBilling.query.filter_by(customer_id=cid).first()
        if row:
            return row.club_id
    club_id = _club_id_from_meta(obj)
    if club_id is None:
        sub = _subscription_id(obj)
        if sub and gateway is not None:
            try:
                club_id = _club_id_from_meta(gateway.retrieve_subscription(sub))
            except Exception as e:
                log.info("could not look up subscription %s: %s", sub, e)
    return club_id


def _link(club_id: int, customer_id: str | None, subscription_id: str | None) -> ClubBilling | None:
    from .models import Club
    if db.session.get(Club, club_id) is None:
        return None
    row = ClubBilling.query.filter_by(club_id=club_id).first()
    if customer_id:
        other = ClubBilling.query.filter(ClubBilling.customer_id == customer_id, ClubBilling.club_id != club_id).first()
        if other:
            log.warning("customer %s is already linked to club %s; ignoring link to club %s", customer_id, other.club_id, club_id)
            return None
    if row is None:
        row = ClubBilling(club_id=club_id)
        db.session.add(row)
    if customer_id:
        row.customer_id = customer_id
    if subscription_id:
        row.subscription_id = subscription_id
    db.session.flush()
    return row


def _apply_subscription(club_id: int, sub: dict) -> None:
    row = ClubBilling.query.filter_by(club_id=club_id).first() or _link(club_id, sub.get("customer"), sub.get("id"))
    if row is None:
        return
    row.subscription_id = sub.get("id") or row.subscription_id
    row.sub_status = sub.get("status")
    row.cancel_at_period_end = bool(sub.get("cancel_at_period_end") or sub.get("cancel_at"))
    end = sub.get("current_period_end")
    if not end:
        items = ((sub.get("items") or {}).get("data")) or []
        end = max((i.get("current_period_end") or 0 for i in items), default=0)
    row.current_period_end = _dt(end) or row.current_period_end
    if row.sub_status in ("active", "trialing"):
        row.last_payment_failed_at = None
    db.session.flush()


def _invoice_period(inv: dict) -> tuple[datetime | None, datetime | None]:
    """The service period an invoice paid for (from its lines; the invoice's own dates are billing dates)."""
    lines = ((inv.get("lines") or {}).get("data")) or []
    starts = [l["period"]["start"] for l in lines if l.get("period")]
    ends = [l["period"]["end"] for l in lines if l.get("period")]
    if ends:
        return _dt(min(starts)), _dt(max(ends))
    return _dt(inv.get("period_start")), _dt(inv.get("period_end"))


def _tax(inv: dict) -> int | None:
    if inv.get("tax") is not None:
        return inv["tax"]
    taxes = inv.get("total_taxes")
    return sum(t.get("amount", 0) for t in taxes) if taxes else None


def upsert_invoice(club_id: int, inv: dict, gateway=None) -> BillingInvoice | None:
    if not inv.get("id"):
        return None
    row = BillingInvoice.query.filter_by(stripe_invoice_id=inv["id"]).first()
    if row is not None and row.club_id != club_id:
        log.warning("invoice %s belongs to club %s, not %s", inv["id"], row.club_id, club_id)
        return None
    if row is None:
        row = BillingInvoice(club_id=club_id, stripe_invoice_id=inv["id"])
        db.session.add(row)
    start, end = _invoice_period(inv)
    lines = ((inv.get("lines") or {}).get("data")) or []
    row.number = inv.get("number") or row.number
    row.status = inv.get("status") or row.status
    row.amount_due, row.amount_paid = inv.get("amount_due") or 0, inv.get("amount_paid") or 0
    row.tax = _tax(inv)
    row.currency = (inv.get("currency") or "eur").upper()
    row.description = ((lines[0].get("description") if lines else None) or inv.get("description") or "Club Sponsor Publishing")[:200]
    row.period_start, row.period_end = start or row.period_start, end or row.period_end
    row.hosted_url, row.pdf_url = inv.get("hosted_invoice_url") or row.hosted_url, inv.get("invoice_pdf") or row.pdf_url
    row.issued_at = _dt((inv.get("status_transitions") or {}).get("finalized_at") or inv.get("created")) or row.issued_at
    if row.status == "paid" and not row.receipt_url and gateway is not None:
        row.receipt_url = gateway.receipt_url(inv)
    db.session.flush()
    return row


def apply_invoice(club_id: int, inv: dict, gateway=None) -> None:
    """Record an invoice and, if it is paid, make premium run to the end of the period it paid for."""
    row = upsert_invoice(club_id, inv, gateway)
    if row is None or inv.get("status") != "paid":
        return
    p = ClubPremium.query.filter_by(club_id=club_id).first()
    if p is not None and p.status == "cancelled" and p.cancelled_at and (row.issued_at or sponsors.utcnow()) <= p.cancelled_at:
        return                                      # paid before it was cancelled (a refund): re-reading it must not switch it back on
    start, end = _invoice_period(inv)
    if end is not None:
        billing.activate_until(club_id, starts_at=start or sponsors.utcnow(), expires_at=end, source="billing",
                               reference=inv.get("number") or inv["id"], note="Paid by card through Stripe")


def sync_club(club_id: int, gateway) -> int:
    """Ask Stripe for the club's subscription and invoices and bring our copy up to date. Returns the number of invoices seen."""
    row = ClubBilling.query.filter_by(club_id=club_id).first()
    if row is None or not row.customer_id:
        return 0
    if row.subscription_id:
        _apply_subscription(club_id, gateway.retrieve_subscription(row.subscription_id))
    invoices = gateway.list_invoices(row.customer_id)
    for inv in invoices:
        apply_invoice(club_id, inv, gateway)
    return len(invoices)


# ------------------------------------------------------------------ webhook events

def handle_event(event: dict, gateway=None) -> str:
    """Apply a verified event once. Raises RetryLater if it can't be placed yet. Returns a short outcome."""
    event_id, etype = event.get("id"), event.get("type", "")
    if not event_id:
        return "ignored: no id"
    if db.session.get(BillingEvent, event_id) is not None:
        return "duplicate"
    obj = (event.get("data") or {}).get("object") or {}
    outcome = _dispatch(etype, obj, gateway)
    db.session.add(BillingEvent(event_id=event_id, type=etype))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return "duplicate"
    return outcome


def _dispatch(etype: str, obj: dict, gateway) -> str:
    if etype == "checkout.session.completed":
        if obj.get("mode") != "subscription":
            return "ignored: not a subscription"
        try:
            club_id = int(obj.get("client_reference_id") or _club_id_from_meta(obj) or 0)
        except ValueError:
            club_id = 0
        if not club_id or _link(club_id, obj.get("customer"), obj.get("subscription")) is None:
            return "ignored: unknown club"
        return "linked"
    if etype in ("invoice.paid", "invoice.payment_succeeded", "invoice.finalized", "invoice.voided", "invoice.marked_uncollectible"):
        club_id = _club_for(obj, gateway)
        if club_id is None:
            if _subscription_id(obj):
                raise RetryLater("invoice arrived before its checkout was linked")
            return "ignored: not a subscription invoice"
        if ClubBilling.query.filter_by(club_id=club_id).first() is None:
            cust = obj.get("customer")
            if _link(club_id, cust.get("id") if isinstance(cust, dict) else cust, _subscription_id(obj)) is None:
                return "ignored: could not link"
        apply_invoice(club_id, obj, gateway)
        return "invoice recorded"
    if etype == "invoice.payment_failed":
        club_id = _club_for(obj, gateway)
        row = ClubBilling.query.filter_by(club_id=club_id).first() if club_id else None
        if row is None:
            return "ignored: unknown club"
        row.last_payment_failed_at, row.sub_status = sponsors.utcnow(), "past_due"
        upsert_invoice(club_id, obj, None)
        return "payment failed noted"
    if etype in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
        club_id = _club_for(obj, gateway)
        if club_id is None:
            return "ignored: unknown club"
        _apply_subscription(club_id, {**obj, "status": "canceled" if etype.endswith("deleted") else obj.get("status")})
        return "subscription updated"
    if etype == "charge.refunded":
        club_id = _club_for(obj, gateway)
        if club_id is None:
            return "ignored: unknown club"
        paid, refunded = obj.get("amount") or 0, obj.get("amount_refunded") or 0
        inv = BillingInvoice.query.filter_by(stripe_invoice_id=obj["invoice"]).first() if obj.get("invoice") else None
        if inv is not None:
            inv.amount_refunded = refunded
        if paid and refunded >= paid:
            billing.cancel(club_id)
            return "refunded in full: premium cancelled"
        return "partial refund noted"
    return "ignored"
