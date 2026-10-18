"""Card billing pages and the Stripe webhook. See stripe_billing.py for the rules. Only a club's admins see its billing."""
from flask import Blueprint, abort, current_app, flash, g, get_flashed_messages, redirect, render_template, request, url_for

from . import db, sponsors, stripe_billing as sb
from .models import BillingInvoice, ClubBilling
from .security import login_required
from .sponsor_views import _admin

bp = Blueprint("billing", __name__)


def _base_url() -> str:
    return (current_app.config.get("APP_BASE_URL") or request.url_root).rstrip("/")


def _gateway():
    return sb.get_gateway(current_app.config)


def _back(**kw):
    return redirect(url_for("billing.page", **kw))


@bp.get("/sponsors/billing")
@login_required
def page():
    club = _admin()
    row = ClubBilling.query.filter_by(club_id=club.id).first()
    invoices = (BillingInvoice.query.filter_by(club_id=club.id)
                .order_by(BillingInvoice.issued_at.desc().nullslast(), BillingInvoice.id.desc()).all())
    state = sponsors.premium_state(club.id)
    subscribed = bool(row and row.sub_status in sb.LIVE_SUB_STATUSES and not row.cancel_at_period_end)
    from .clubs import is_platform_admin
    return render_template("billing.html", club=club, row=row, invoices=invoices, state=state, price=sponsors.PRICE_LINE,
                           stripe_on=_gateway() is not None, subscribed=subscribed, notes=get_flashed_messages(with_categories=True),
                           platform_admin=is_platform_admin(g.user), webhook_url=f"{_base_url()}/stripe/webhook",
                           webhook_events=sb.EVENTS_WE_USE, webhook_ready=bool(current_app.config.get("STRIPE_WEBHOOK_SECRET")),
                           tax_on=bool(current_app.config.get("STRIPE_AUTOMATIC_TAX")))


@bp.post("/sponsors/billing/checkout")
@login_required
def checkout():
    """Start a yearly subscription for the signed-in person's current club (never a club id from the browser)."""
    club = _admin()
    gw = _gateway()
    if gw is None:
        flash("Card payment isn't switched on yet. Ask the site admin to activate Club Sponsor Publishing for your club.", "error")
        return _back()
    row = ClubBilling.query.filter_by(club_id=club.id).first()
    if row and row.sub_status in sb.LIVE_SUB_STATUSES and not row.cancel_at_period_end:
        flash("This club already has an active subscription. Use Manage billing to change or cancel it.", "error")
        return _back()
    cfg = current_app.config
    try:
        session = gw.create_checkout(
            club_id=club.id, club_name=club.name, customer_id=row.customer_id if row else None, email=g.user.email,
            success_url=f"{_base_url()}/sponsors/billing/return?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{_base_url()}/sponsors/billing?checkout=cancelled", price_id=cfg.get("STRIPE_PRICE_ID", ""),
            tax=bool(cfg.get("STRIPE_AUTOMATIC_TAX")), tax_behavior=cfg.get("STRIPE_TAX_BEHAVIOR", "inclusive"))
    except Exception:
        current_app.logger.exception("stripe checkout failed")
        flash("We couldn't reach the payment service. Nothing was charged. Please try again in a moment.", "error")
        return _back()
    return redirect(session["url"], code=303)


@bp.get("/sponsors/billing/return")
@login_required
def checkout_return():
    """Where Stripe sends the person back. This page never marks anything paid by itself: it asks Stripe, from the server,
    what happened to a session that belongs to this club."""
    club = _admin()
    gw = _gateway()
    sid = request.args.get("session_id", "")
    if gw is None or not sid.startswith("cs_"):
        return _back()
    try:
        session = gw.retrieve_session(sid)
        if str(session.get("client_reference_id")) != str(club.id) or session.get("mode") != "subscription":
            abort(404)
        if session.get("payment_status") not in ("paid", "no_payment_required"):
            flash("The payment wasn't completed, so nothing was charged and nothing changed.", "error")
            return _back()
        sb._link(club.id, session.get("customer"), session.get("subscription"))
        sb.sync_club(club.id, gw)
        db.session.commit()
    except Exception as e:
        if getattr(e, "code", None) == 404:
            raise
        db.session.rollback()
        current_app.logger.exception("stripe return sync failed")
        flash("Thanks. Your payment is being confirmed; refresh this page in a minute.", "ok")
        return _back()
    if sponsors.premium_state(club.id)["serving"]:
        flash("Payment received. Club Sponsor Publishing is on, and your invoice and receipt are below.", "ok")
    else:
        flash("Payment received. It can take a minute to switch on; refresh this page if it doesn't show as active.", "ok")
    return _back()


@bp.post("/sponsors/billing/portal")
@login_required
def portal():
    """Stripe's own page to update a card, download invoices or cancel."""
    club = _admin()
    gw = _gateway()
    row = ClubBilling.query.filter_by(club_id=club.id).first()
    if gw is None or row is None or not row.customer_id:
        flash("There's no card billing set up for this club yet.", "error")
        return _back()
    try:
        url = gw.portal_url(row.customer_id, f"{_base_url()}/sponsors/billing")
    except Exception:
        current_app.logger.exception("stripe portal failed")
        flash("We couldn't open the billing portal. Please try again in a moment.", "error")
        return _back()
    return redirect(url, code=303)


@bp.post("/sponsors/billing/refresh")
@login_required
def refresh():
    """Re-read this club's subscription and invoices from Stripe."""
    club = _admin()
    gw = _gateway()
    if gw is None:
        return _back()
    try:
        n = sb.sync_club(club.id, gw)
        db.session.commit()
        flash(f"Up to date: {n} invoice{'' if n == 1 else 's'} found.", "ok")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("stripe refresh failed")
        flash("We couldn't reach the payment service. Please try again in a moment.", "error")
    return _back()


@bp.post("/stripe/webhook")
def webhook():
    """Stripe calls this. The signature is the only thing that makes a request believable (there is no browser session)."""
    secret = current_app.config.get("STRIPE_WEBHOOK_SECRET", "")
    try:
        event = sb.verify_webhook(request.get_data(), request.headers.get("Stripe-Signature"), secret)
    except sb.WebhookVerificationError:
        return {"error": "bad signature"}, 400
    try:
        outcome = sb.handle_event(event, _gateway())
    except sb.RetryLater:
        db.session.rollback()
        return {"status": "retry"}, 503
    except Exception:
        db.session.rollback()
        current_app.logger.exception("stripe event %s failed", event.get("id"))
        return {"status": "error"}, 500
    return {"status": "ok", "outcome": outcome}, 200
