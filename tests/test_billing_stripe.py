"""Card billing for Club Sponsor Publishing: checkout, the signed webhook, renewals, refunds, invoices and receipts.
Stripe is replaced by a fake gateway, so nothing here touches the network or takes money."""
import hashlib
import hmac
import json
import time
from datetime import timedelta

import pytest

from app import billing, db, sponsors
from app import stripe_billing as sb
from app.models import BillingEvent, BillingInvoice, Club, ClubBilling, ClubPremium
from tests.test_auth_security import other_client, sign_in
from tests.test_clubs import member, new_club, user

SECRET = "whsec_test_secret_for_tests"
NOWTS = int(time.time())
DAY = 86400


class FakeStripe:
    def __init__(self):
        self.calls = []
        self.sessions = {}
        self.subs = {}
        self.invoices = {}
        self.fail = False

    def create_checkout(self, **kw):
        if self.fail:
            raise RuntimeError("stripe is down")
        self.calls.append(("checkout", kw))
        return {"id": "cs_test_1", "url": "https://checkout.stripe.test/c/pay/cs_test_1"}

    def retrieve_session(self, sid):
        self.calls.append(("session", sid))
        return self.sessions[sid]

    def retrieve_subscription(self, sid):
        return self.subs[sid]

    def list_invoices(self, customer_id, limit=24):
        return self.invoices.get(customer_id, [])

    def portal_url(self, customer_id, return_url):
        self.calls.append(("portal", customer_id, return_url))
        return "https://billing.stripe.test/p/session/abc"

    def receipt_url(self, invoice):
        return f"https://pay.stripe.test/receipts/{invoice['id']}"


def invoice(iid="in_1", customer="cus_A", sub="sub_A", start=None, end=None, status="paid", amount=7999, number="RLK-0001",
            created=None, tax=1496):
    start = start if start is not None else NOWTS - DAY
    end = end if end is not None else start + 365 * DAY
    return {"id": iid, "object": "invoice", "customer": customer, "status": status, "number": number, "currency": "eur",
            "amount_due": amount, "amount_paid": amount if status == "paid" else 0, "total_taxes": [{"amount": tax}],
            "hosted_invoice_url": f"https://invoice.stripe.test/i/{iid}", "invoice_pdf": f"https://pay.stripe.test/invoice/{iid}/pdf",
            "created": created if created is not None else start, "status_transitions": {"finalized_at": created if created is not None else start},
            "parent": {"subscription_details": {"subscription": sub, "metadata": {}}},
            "lines": {"data": [{"description": "Club Sponsor Publishing", "period": {"start": start, "end": end}}]}}


def event(eid, etype, obj):
    return {"id": eid, "object": "event", "type": etype, "data": {"object": obj}}


def signed(payload: dict, secret=SECRET, ts=None):
    body = json.dumps(payload)
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return body, {"Stripe-Signature": f"t={ts},v1={sig}", "Content-Type": "application/json"}


def send(client, payload, **kw):
    body, headers = signed(payload, **kw)
    return client.post("/stripe/webhook", data=body, headers=headers)


@pytest.fixture
def bw(client):
    """Club A (no premium yet) with an admin, a member and the site admin; club B with its own admin; a fake Stripe."""
    fake = FakeStripe()
    client.application.config.update(STRIPE_CLIENT=fake, STRIPE_WEBHOOK_SECRET=SECRET, APP_BASE_URL="https://rl.example.test",
                                     STRIPE_PRICE_ID="", STRIPE_AUTOMATIC_TAX=False, PREMIUM_GRACE_DAYS=0)
    a = Club.query.first()
    b = new_club("Other Wheelers")
    w = type("W", (), {})()
    w.fake, w.client, w.a, w.b = fake, client, a, b
    w.site = user("site@example.com", "admin")
    w.admin_a, w.plain_a, w.admin_b = user("admin-a@example.com"), user("plain-a@example.com"), user("admin-b@example.com")
    member(w.admin_a, a.id, "admin")
    member(w.plain_a, a.id)
    member(w.admin_b, b.id, "admin")
    return w


def login(w, u, club=None):
    c = other_client(w.client)
    sign_in(c, u)
    with c.session_transaction() as s:
        s["club_id"] = (club or w.a).id
    return c


def link(w, club=None, customer="cus_A", sub="sub_A"):
    club = club or w.a
    r = send(w.client, event(f"evt_link_{customer}", "checkout.session.completed",
                             {"id": "cs_1", "mode": "subscription", "customer": customer, "subscription": sub,
                              "client_reference_id": str(club.id), "payment_status": "paid"}))
    assert r.status_code == 200 and r.get_json()["outcome"] == "linked"


def premium(club):
    return ClubPremium.query.filter_by(club_id=club.id).first()


# ===================== the price =====================

def test_the_price_is_79_99_everywhere(bw):
    assert sponsors.PRICE_CENTS == 7999 and sponsors.PRICE_LABEL == "€79.99"
    c = login(bw, bw.admin_a)
    for path in ("/sponsors", "/sponsors/billing"):
        page = c.get(path).get_data(as_text=True)
        assert "€79.99 per club per year" in page and "19.99" not in page
    assert "19.99" not in sponsors.VALUE_STATEMENT and "€79.99 per club per year" in sponsors.VALUE_STATEMENT


# ===================== without Stripe configured =====================

def test_with_no_stripe_the_manual_route_still_works_and_the_pages_say_so(bw):
    bw.client.application.config.update(STRIPE_CLIENT=None, STRIPE_SECRET_KEY="")
    c = login(bw, bw.admin_a)
    page = c.get("/sponsors").get_data(as_text=True)
    assert "Card payment isn't switched on yet" in page and "by card" not in page
    assert "isn't switched on yet" in c.get("/sponsors/billing").get_data(as_text=True)
    r = c.post("/sponsors/billing/checkout")
    assert r.status_code == 302 and premium(bw.a) is None
    billing.activate(bw.a.id, by_user_id=bw.site.id, months=12, reference="BANK-1")
    db.session.commit()
    assert sponsors.premium_state(bw.a.id)["serving"]


# ===================== checkout =====================

def test_checkout_starts_a_yearly_subscription_for_the_signed_in_clubs_own_id(bw):
    c = login(bw, bw.admin_a)
    r = c.post("/sponsors/billing/checkout", data={"club_id": bw.b.id})            # a browser-supplied club id is ignored
    assert r.status_code == 303 and r.headers["Location"] == "https://checkout.stripe.test/c/pay/cs_test_1"
    kind, kw = bw.fake.calls[-1]
    assert kind == "checkout" and kw["club_id"] == bw.a.id and kw["club_name"] == bw.a.name
    assert kw["email"] == "admin-a@example.com" and kw["customer_id"] is None
    assert kw["success_url"] == "https://rl.example.test/sponsors/billing/return?session_id={CHECKOUT_SESSION_ID}"
    assert kw["cancel_url"].startswith("https://rl.example.test/sponsors/billing")
    assert premium(bw.a) is None                                                  # nothing is marked paid by starting checkout


def test_only_club_admins_can_start_checkout_open_the_portal_or_see_billing(bw):
    for who in (bw.plain_a,):
        c = login(bw, who)
        assert c.post("/sponsors/billing/checkout").status_code == 403
        assert c.post("/sponsors/billing/portal").status_code == 403
        assert c.post("/sponsors/billing/refresh").status_code == 403
        assert c.get("/sponsors/billing").status_code == 403
    assert other_client(bw.client).get("/sponsors/billing").status_code in (302, 401)
    assert not [x for x in bw.fake.calls if x[0] == "checkout"]


def test_checkout_is_refused_while_a_subscription_is_active_and_allowed_after_it_ends(bw):
    link(bw)
    row = ClubBilling.query.filter_by(club_id=bw.a.id).one()
    row.sub_status = "active"
    db.session.commit()
    c = login(bw, bw.admin_a)
    assert c.post("/sponsors/billing/checkout").status_code == 302
    assert not [x for x in bw.fake.calls if x[0] == "checkout"]
    row.cancel_at_period_end = True
    db.session.commit()
    r = c.post("/sponsors/billing/checkout")
    assert r.status_code == 303 and bw.fake.calls[-1][1]["customer_id"] == "cus_A"       # reuses the same Stripe customer


def test_a_stripe_outage_during_checkout_is_a_calm_message_not_an_error(bw):
    bw.fake.fail = True
    r = login(bw, bw.admin_a).post("/sponsors/billing/checkout", follow_redirects=True)
    assert r.status_code == 200 and "Nothing was charged" in r.get_data(as_text=True)
    assert premium(bw.a) is None


# ===================== the webhook: who may call it =====================

def test_the_webhook_needs_a_valid_fresh_signature_and_no_browser_session(bw):
    ev = event("evt_x", "invoice.paid", invoice())
    body = json.dumps(ev)
    assert bw.client.post("/stripe/webhook", data=body).status_code == 400                                   # no signature
    assert bw.client.post("/stripe/webhook", data=body, headers={"Stripe-Signature": "t=1,v1=abc"}).status_code == 400
    b2, h2 = signed(ev, secret="whsec_wrong")
    assert bw.client.post("/stripe/webhook", data=b2, headers=h2).status_code == 400                         # wrong secret
    b3, h3 = signed(ev, ts=int(time.time()) - 3600)
    assert bw.client.post("/stripe/webhook", data=b3, headers=h3).status_code == 400                         # too old (replay)
    b4, h4 = signed(ev)
    assert bw.client.post("/stripe/webhook", data=b4 + " ", headers=h4).status_code == 400                   # body altered
    bw.client.application.config["STRIPE_WEBHOOK_SECRET"] = ""
    b5, h5 = signed(ev)
    assert bw.client.post("/stripe/webhook", data=b5, headers=h5).status_code == 400                         # no secret set: refuse all
    assert BillingEvent.query.count() == 0 and premium(bw.a) is None


def test_a_valid_signature_is_accepted_without_csrf_or_sign_in(bw):
    r = send(other_client(bw.client), event("evt_ping", "ping", {}))
    assert r.status_code == 200 and r.get_json()["outcome"] == "ignored"


# ===================== the webhook: linking and paying =====================

def test_checkout_completed_links_the_customer_to_the_club_we_named(bw):
    link(bw)
    row = ClubBilling.query.filter_by(club_id=bw.a.id).one()
    assert row.customer_id == "cus_A" and row.subscription_id == "sub_A"
    assert premium(bw.a) is None                                                  # linking alone doesn't grant premium


def test_checkout_events_for_unknown_clubs_or_other_modes_are_ignored(bw):
    r = send(bw.client, event("e1", "checkout.session.completed", {"mode": "subscription", "customer": "cus_Z", "client_reference_id": "99999"}))
    assert r.get_json()["outcome"] == "ignored: unknown club"
    r = send(bw.client, event("e2", "checkout.session.completed", {"mode": "payment", "customer": "cus_Y", "client_reference_id": str(bw.a.id)}))
    assert r.get_json()["outcome"] == "ignored: not a subscription"
    assert ClubBilling.query.count() == 0


def test_a_customer_already_linked_to_one_club_cannot_be_linked_to_another(bw):
    link(bw, bw.a, "cus_A")
    send(bw.client, event("e3", "checkout.session.completed", {"mode": "subscription", "customer": "cus_A", "subscription": "sub_X",
                                                               "client_reference_id": str(bw.b.id)}))
    assert ClubBilling.query.filter_by(club_id=bw.b.id).count() == 0
    assert ClubBilling.query.filter_by(club_id=bw.a.id).one().subscription_id == "sub_A"


def test_a_paid_invoice_switches_premium_on_until_the_end_of_the_period_paid(bw):
    link(bw)
    inv = invoice()
    r = send(bw.client, event("evt_paid", "invoice.paid", inv))
    assert r.status_code == 200 and r.get_json()["outcome"] == "invoice recorded"
    p = premium(bw.a)
    assert p.status == "active" and p.source == "billing" and p.external_ref == "RLK-0001" and p.price_cents == 7999
    assert int(p.expires_at.timestamp()) == inv["lines"]["data"][0]["period"]["end"]
    st = sponsors.premium_state(bw.a.id)
    assert st["state"] == "active" and st["serving"] and st["days_left"] >= 363
    row = BillingInvoice.query.filter_by(club_id=bw.a.id).one()
    assert row.number == "RLK-0001" and row.status == "paid" and row.amount_paid == 7999 and row.tax == 1496 and row.currency == "EUR"
    assert row.pdf_url.endswith("/pdf") and row.hosted_url and row.receipt_url == "https://pay.stripe.test/receipts/in_1"


def test_repeated_and_overlapping_events_never_double_or_stretch_the_period(bw):
    link(bw)
    inv = invoice()
    send(bw.client, event("evt_a", "invoice.paid", inv))
    first = premium(bw.a).expires_at
    assert send(bw.client, event("evt_a", "invoice.paid", inv)).get_json()["outcome"] == "duplicate"          # same event again
    send(bw.client, event("evt_b", "invoice.payment_succeeded", inv))                                       # same invoice, another event
    send(bw.client, event("evt_c", "invoice.finalized", inv))
    assert premium(bw.a).expires_at == first
    assert BillingInvoice.query.count() == 1 and BillingEvent.query.filter_by(event_id="evt_a").count() == 1


def test_a_renewal_extends_and_an_old_replayed_invoice_cannot_shorten(bw):
    link(bw)
    y1 = invoice("in_1", start=NOWTS - DAY, number="RLK-1")
    send(bw.client, event("e1", "invoice.paid", y1))
    y2_start = y1["lines"]["data"][0]["period"]["end"]
    y2 = invoice("in_2", start=y2_start, number="RLK-2", created=NOWTS)
    send(bw.client, event("e2", "invoice.paid", y2))
    assert int(premium(bw.a).expires_at.timestamp()) == y2_start + 365 * DAY
    send(bw.client, event("e3", "invoice.paid", invoice("in_1", start=NOWTS - DAY, number="RLK-1")))        # an old one again
    assert int(premium(bw.a).expires_at.timestamp()) == y2_start + 365 * DAY
    assert BillingInvoice.query.count() == 2


def test_an_invoice_that_arrives_before_its_checkout_is_retried_not_lost(bw):
    inv = invoice()
    r = send(bw.client, event("evt_early", "invoice.paid", inv))
    assert r.status_code == 503 and premium(bw.a) is None and BillingEvent.query.count() == 0
    bw.fake.subs["sub_A"] = {"id": "sub_A", "metadata": {"club_id": str(bw.a.id)}}        # Stripe can tell us whose subscription it is
    r = send(bw.client, event("evt_early", "invoice.paid", inv))
    assert r.status_code == 200 and premium(bw.a).status == "active"


def test_our_own_customer_record_beats_a_club_id_in_metadata(bw):
    link(bw, bw.a, "cus_A")
    inv = invoice(customer="cus_A")
    inv["parent"]["subscription_details"]["metadata"] = {"club_id": str(bw.b.id)}                          # tries to point at club B
    send(bw.client, event("e1", "invoice.paid", inv))
    assert premium(bw.a).status == "active" and premium(bw.b) is None


def test_invoices_for_things_that_are_not_ours_are_ignored(bw):
    inv = invoice(customer="cus_stranger")
    inv["parent"] = None
    r = send(bw.client, event("e1", "invoice.paid", inv))
    assert r.status_code == 200 and r.get_json()["outcome"].startswith("ignored") and BillingInvoice.query.count() == 0


# ===================== failed payments, cancellation, refunds =====================

def test_a_failed_renewal_does_not_cut_off_a_paid_period_and_is_shown_to_the_admin(bw):
    link(bw)
    send(bw.client, event("e1", "invoice.paid", invoice("in_1")))
    failed = invoice("in_2", status="open", number=None)
    r = send(bw.client, event("e2", "invoice.payment_failed", failed))
    assert r.get_json()["outcome"] == "payment failed noted"
    assert sponsors.premium_state(bw.a.id)["serving"]
    page = login(bw, bw.admin_a).get("/sponsors/billing").get_data(as_text=True)
    assert "last payment didn't go through" in page and "Manage billing" in page


def test_cancelling_the_subscription_keeps_premium_to_the_end_of_the_paid_period(bw):
    link(bw)
    inv = invoice()
    send(bw.client, event("e1", "invoice.paid", inv))
    sub = {"id": "sub_A", "customer": "cus_A", "status": "active", "cancel_at_period_end": True, "current_period_end": inv["lines"]["data"][0]["period"]["end"]}
    send(bw.client, event("e2", "customer.subscription.updated", sub))
    page = login(bw, bw.admin_a).get("/sponsors/billing").get_data(as_text=True)
    assert "will not renew" in page and "Renews automatically" not in page
    send(bw.client, event("e3", "customer.subscription.deleted", {**sub, "status": "canceled"}))
    assert ClubBilling.query.one().sub_status == "canceled" and sponsors.premium_state(bw.a.id)["serving"]
    p = premium(bw.a)
    p.expires_at = sponsors.utcnow() - timedelta(minutes=1)                       # the paid period then runs out
    db.session.commit()
    assert sponsors.premium_state(bw.a.id)["state"] == "expired"


def test_a_full_refund_cancels_premium_a_partial_one_does_not(bw):
    link(bw)
    send(bw.client, event("e1", "invoice.paid", invoice()))
    r = send(bw.client, event("e2", "charge.refunded", {"id": "ch_1", "customer": "cus_A", "amount": 7999, "amount_refunded": 2000}))
    assert r.get_json()["outcome"] == "partial refund noted" and sponsors.premium_state(bw.a.id)["serving"]
    r = send(bw.client, event("e3", "charge.refunded", {"id": "ch_1", "customer": "cus_A", "amount": 7999, "amount_refunded": 7999}))
    assert "cancelled" in r.get_json()["outcome"]
    assert sponsors.premium_state(bw.a.id)["state"] == "cancelled" and not sponsors.premium_state(bw.a.id)["serving"]


def test_re_reading_a_refunded_invoice_does_not_switch_premium_back_on_but_a_new_payment_does(bw):
    link(bw)
    old = invoice("in_old", created=NOWTS - 2 * DAY)
    send(bw.client, event("e1", "invoice.paid", old))
    send(bw.client, event("e2", "charge.refunded", {"id": "ch_1", "customer": "cus_A", "amount": 7999, "amount_refunded": 7999}))
    bw.fake.invoices["cus_A"] = [old]
    bw.fake.subs["sub_A"] = {"id": "sub_A", "customer": "cus_A", "status": "canceled"}
    c = login(bw, bw.admin_a)
    c.post("/sponsors/billing/refresh")
    assert sponsors.premium_state(bw.a.id)["state"] == "cancelled"
    later = int(time.time()) + 60                                                # NOWTS was fixed at import, long before a full run gets here
    fresh = invoice("in_new", start=later, number="RLK-9", created=later)
    send(bw.client, event("e3", "invoice.paid", fresh))
    assert sponsors.premium_state(bw.a.id)["state"] == "active"


def test_activate_until_never_shortens_a_running_period_and_revives_a_cancelled_club(bw):
    now = sponsors.utcnow()
    billing.activate_until(bw.a.id, starts_at=now, expires_at=now + timedelta(days=300))
    billing.activate_until(bw.a.id, starts_at=now, expires_at=now + timedelta(days=100))
    db.session.commit()
    assert premium(bw.a).expires_at >= now + timedelta(days=299)
    billing.cancel(bw.a.id)
    billing.activate_until(bw.a.id, starts_at=now, expires_at=now + timedelta(days=365))
    db.session.commit()
    assert sponsors.premium_state(bw.a.id)["state"] == "active"


# ===================== returning from checkout =====================

def paid_session(club, customer="cus_A", sub="sub_A"):
    return {"id": "cs_ok", "mode": "subscription", "client_reference_id": str(club.id), "payment_status": "paid", "customer": customer,
            "subscription": sub}


def test_the_return_page_asks_stripe_and_switches_premium_on_even_if_the_webhook_is_late(bw):
    bw.fake.sessions["cs_ok"] = paid_session(bw.a)
    bw.fake.subs["sub_A"] = {"id": "sub_A", "customer": "cus_A", "status": "active", "current_period_end": NOWTS + 360 * DAY}
    bw.fake.invoices["cus_A"] = [invoice()]
    r = login(bw, bw.admin_a).get("/sponsors/billing/return?session_id=cs_ok", follow_redirects=True)
    assert r.status_code == 200 and "Payment received" in r.get_data(as_text=True)
    assert sponsors.premium_state(bw.a.id)["serving"] and BillingInvoice.query.count() == 1
    row = ClubBilling.query.one()
    assert row.customer_id == "cus_A" and row.sub_status == "active"


def test_the_return_page_without_a_paid_session_of_this_club_changes_nothing(bw):
    c = login(bw, bw.admin_a)
    assert c.get("/sponsors/billing/return").status_code == 302                                           # no session id
    assert c.get("/sponsors/billing/return?session_id=junk").status_code == 302
    bw.fake.sessions["cs_other"] = paid_session(bw.b)
    assert c.get("/sponsors/billing/return?session_id=cs_other").status_code == 404                        # another club's session
    bw.fake.sessions["cs_unpaid"] = {**paid_session(bw.a), "payment_status": "unpaid"}
    r = c.get("/sponsors/billing/return?session_id=cs_unpaid", follow_redirects=True)
    assert "wasn&#39;t completed" in r.get_data(as_text=True)
    assert premium(bw.a) is None and premium(bw.b) is None and ClubBilling.query.count() == 0


# ===================== the portal and refresh =====================

def test_the_portal_opens_for_the_clubs_own_customer_only(bw):
    c = login(bw, bw.admin_a)
    assert c.post("/sponsors/billing/portal").status_code == 302                                           # nothing linked yet
    assert not [x for x in bw.fake.calls if x[0] == "portal"]
    link(bw, bw.a, "cus_A")
    link(bw, bw.b, "cus_B", "sub_B")
    r = c.post("/sponsors/billing/portal", data={"customer": "cus_B"})
    assert r.status_code == 303 and r.headers["Location"].startswith("https://billing.stripe.test/")
    assert bw.fake.calls[-1] == ("portal", "cus_A", "https://rl.example.test/sponsors/billing")


def test_refresh_pulls_the_invoices_into_the_page(bw):
    link(bw)
    bw.fake.subs["sub_A"] = {"id": "sub_A", "customer": "cus_A", "status": "active", "current_period_end": NOWTS + 300 * DAY}
    bw.fake.invoices["cus_A"] = [invoice("in_1", number="RLK-1"), invoice("in_2", number="RLK-2", start=NOWTS + 500 * DAY)]
    r = login(bw, bw.admin_a).post("/sponsors/billing/refresh", follow_redirects=True)
    assert "2 invoices found" in r.get_data(as_text=True) and BillingInvoice.query.count() == 2


# ===================== invoices and receipts to download =====================

def test_the_billing_page_lists_invoices_with_download_links_for_this_club_only(bw):
    link(bw, bw.a, "cus_A", "sub_A")
    link(bw, bw.b, "cus_B", "sub_B")
    send(bw.client, event("e1", "invoice.paid", invoice("in_a", "cus_A", "sub_A", number="RLK-A1")))
    send(bw.client, event("e2", "invoice.paid", invoice("in_b", "cus_B", "sub_B", number="RLK-B1")))
    page = login(bw, bw.admin_a).get("/sponsors/billing").get_data(as_text=True)
    assert "RLK-A1" in page and "RLK-B1" not in page and "in_b" not in page
    assert "https://pay.stripe.test/invoice/in_a/pdf" in page and "https://pay.stripe.test/receipts/in_a" in page
    assert "https://invoice.stripe.test/i/in_a" in page
    assert page.count('rel="noopener noreferrer" target="_blank"') >= 3
    assert "Invoice (PDF)" in page and "Receipt" in page and "€79.99" in page and "incl. €14.96 tax" in page
    other = login(bw, bw.admin_b, bw.b).get("/sponsors/billing").get_data(as_text=True)
    assert "RLK-B1" in other and "RLK-A1" not in other


def test_the_billing_page_has_an_honest_empty_state_and_shows_refunds(bw):
    page = login(bw, bw.admin_a).get("/sponsors/billing").get_data(as_text=True)
    assert "No invoices yet" in page
    link(bw)
    send(bw.client, event("e1", "invoice.paid", invoice()))
    send(bw.client, event("e2", "charge.refunded", {"id": "ch", "customer": "cus_A", "amount": 7999, "amount_refunded": 2000, "invoice": "in_1"}))
    page = login(bw, bw.admin_a).get("/sponsors/billing").get_data(as_text=True)
    assert "€20.00 refunded" in page


def test_stripe_setup_details_are_for_the_site_admin_only(bw):
    admin_page = login(bw, bw.admin_a).get("/sponsors/billing").get_data(as_text=True)
    assert "Stripe set-up" not in admin_page and "/stripe/webhook" not in admin_page and SECRET not in admin_page
    site_page = login(bw, bw.site).get("/sponsors/billing").get_data(as_text=True)
    assert "Stripe set-up" in site_page and "https://rl.example.test/stripe/webhook" in site_page
    assert "invoice.paid" in site_page and SECRET not in site_page


# ===================== the dashboard =====================

def test_the_dashboard_offers_card_payment_until_a_subscription_is_active(bw):
    c = login(bw, bw.admin_a)
    page = c.get("/sponsors").get_data(as_text=True)
    assert "Activate by card: €79.99 per club per year" in page and "Billing and invoices" in page
    link(bw)
    row = ClubBilling.query.one()
    row.sub_status = "active"
    db.session.commit()
    assert "by card" not in c.get("/sponsors").get_data(as_text=True).split("Where and when")[0]


def test_a_member_still_cannot_use_any_of_it(bw):
    c = login(bw, bw.plain_a)
    assert c.get("/sponsors").status_code == 403


# ===================== the real gateway builds correct requests =====================

def test_the_real_gateway_asks_stripe_for_a_yearly_eur_subscription_with_the_club_on_it(monkeypatch):
    import stripe
    seen = {}
    monkeypatch.setattr(stripe.checkout.Session, "create", lambda **kw: seen.update(kw) or {"id": "cs_1", "url": "https://x.test/pay"})
    gw = sb.StripeGateway("sk_test_dummy")
    out = gw.create_checkout(club_id=7, club_name="Velo Club", customer_id=None, email="a@example.com", success_url="https://s", cancel_url="https://c",
                             price_id="", tax=True, tax_behavior="inclusive")
    assert out["url"] == "https://x.test/pay"
    item = seen["line_items"][0]
    assert item["price_data"]["unit_amount"] == 7999 and item["price_data"]["currency"] == "eur"
    assert item["price_data"]["recurring"] == {"interval": "year"} and item["price_data"]["tax_behavior"] == "inclusive"
    assert seen["mode"] == "subscription" and seen["client_reference_id"] == "7" and seen["metadata"] == {"club_id": "7"}
    assert seen["subscription_data"] == {"metadata": {"club_id": "7"}} and seen["automatic_tax"] == {"enabled": True}
    assert seen["customer_email"] == "a@example.com" and "customer" not in seen and seen["api_key"] == "sk_test_dummy"
    assert seen["tax_id_collection"] == {"enabled": True} and seen["billing_address_collection"] == "required"
    seen.clear()
    gw.create_checkout(club_id=7, club_name="Velo Club", customer_id="cus_9", email="a@example.com", success_url="https://s", cancel_url="https://c",
                       price_id="price_123", tax=False, tax_behavior="inclusive")
    assert seen["customer"] == "cus_9" and "customer_email" not in seen and seen["customer_update"] == {"name": "auto", "address": "auto"}
    assert seen["line_items"][0] == {"price": "price_123", "quantity": 1} and seen["automatic_tax"] == {"enabled": False}


def test_the_stripe_response_helper_handles_sdk_objects():
    class Obj:
        def to_dict(self):
            return {"a": {"b": [1, 2]}}
    assert sb._plain(Obj()) == {"a": {"b": [1, 2]}} and sb._plain({"x": 1}) == {"x": 1}


def test_gateway_selection_follows_configuration(client):
    cfg = {"STRIPE_SECRET_KEY": ""}
    assert sb.get_gateway(cfg) is None
    assert isinstance(sb.get_gateway({"STRIPE_SECRET_KEY": "sk_test_x"}), sb.StripeGateway)
    sentinel = object()
    assert sb.get_gateway({"STRIPE_CLIENT": sentinel}) is sentinel


def test_no_card_data_is_ever_stored(client):
    cols = {c.name for t in (ClubBilling, BillingInvoice, BillingEvent) for c in t.__table__.columns}
    assert not any(w in n for n in cols for w in ("card", "pan", "cvc", "cvv", "secret", "token"))
