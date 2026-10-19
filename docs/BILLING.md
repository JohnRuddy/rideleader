# Card billing for Club Sponsor Publishing (Stripe)

**€79.99 per club per year**, as a yearly Stripe subscription. Built and tested against a fake Stripe, so no money has moved
and nothing has been checked against Stripe's own servers yet: the first job is a test-mode run (below).

## What a club admin sees

Screenshots: [docs/sponsors/billing-subscription-phone.jpg](sponsors/billing-subscription-phone.jpg), [docs/sponsors/billing-invoices-phone.jpg](sponsors/billing-invoices-phone.jpg) (demo data, not real payments).

- **Sponsors → Activate by card**, or **Sponsors → Billing and invoices**.
- Checkout is Stripe's own page (card, business name, optional VAT number). We never see or store card numbers.
- Back on the **Billing and invoices** page: subscription status and renewal date, a **Manage billing** button (Stripe's portal: update
  card, download invoices, cancel), and a table of **invoices and receipts** with *Invoice (PDF)*, *Receipt* and *View online* links.
  Invoices and receipts are Stripe's documents; we keep their headline facts and links so they stay findable.
- If the card fails the page says so and keeps sponsor panels showing until the paid period ends. If they cancel, panels keep
  showing until the end of the period they paid for.

## How it works

| Step | What happens |
|---|---|
| Start | `POST /sponsors/billing/checkout` (club admins only). The club is the signed-in person's current club, never an id from the browser. Creates a Checkout Session (yearly price in EUR from `PRICE_CENTS`, or `STRIPE_PRICE_ID`), with the club id on the session and the subscription. |
| Link | `checkout.session.completed` links the Stripe customer and subscription to that club. |
| Pay | `invoice.paid` records the invoice and runs premium to the end of the period paid (`billing.activate_until`, never shortening a period already paid). |
| Return page | `/sponsors/billing/return` never trusts the redirect: it asks Stripe from the server for that session (it must belong to this club and be paid), then syncs. It also makes things work if the webhook is late. |
| Renew | the next `invoice.paid` extends by a year. A failed payment (`invoice.payment_failed`) doesn't extend and doesn't cut off the paid period. |
| Cancel | `customer.subscription.updated/deleted`: premium continues to the end of the paid period, then expires like any other. |
| Refund | `charge.refunded` for the full amount cancels premium at once; a partial refund is noted only. An old paid invoice that is re-read later cannot switch a refunded club back on; a new payment can. |
| Every event | signature verified (`STRIPE_WEBHOOK_SECRET`, 5 minute tolerance), then applied once (event ids are stored). An invoice that arrives before its checkout is linked gets a 503 so Stripe retries it. |

Our own customer record decides which club an event belongs to; metadata is only used when there is no record yet.
`/stripe/webhook` is the only CSRF-exempt Stripe endpoint and has no session: the signature is the only thing that makes a request believable.

## Settings (all optional until you turn it on)

| Variable | Purpose |
|---|---|
| `STRIPE_SECRET_KEY` | `sk_test_…` then `sk_live_…` (or a restricted key allowing Checkout Sessions, Customers, Subscriptions, Invoices, Charges, Billing Portal: read, plus write for Checkout and Portal). Empty = card payment off; the manual route still works. |
| `STRIPE_WEBHOOK_SECRET` | `whsec_…` for the endpoint below. |
| `STRIPE_PRICE_ID` | Optional. A Price you made in the dashboard. If empty, the app sends the €79.99 yearly price itself. |
| `STRIPE_AUTOMATIC_TAX` | `1` to let Stripe Tax work out VAT (needs Stripe Tax set up in the dashboard). |
| `STRIPE_TAX_BEHAVIOR` | `inclusive` (default: €79.99 is the final price) or `exclusive`. Decide with your accountant. |
| `PREMIUM_GRACE_DAYS` | Days panels keep showing after a period ends. `7` is sensible with card billing. |

Secrets go in `PROD_ENV` / the server `.env` by you; they are never put in the repository or printed. The compose files pass them through.

## Turning it on: test mode first

1. In Stripe (test mode): **Developers → Webhooks → Add endpoint**: `https://rideleader.cyclingtools.cc/stripe/webhook`, events:
   `checkout.session.completed, invoice.paid, invoice.payment_failed, invoice.finalized, invoice.voided, invoice.marked_uncollectible,
   customer.subscription.created, customer.subscription.updated, customer.subscription.deleted, charge.refunded`.
   (The site admin sees this list, and the exact address, at the bottom of the Billing page.)
2. **Settings → Billing → Customer portal**: activate it (allow cancel, update payment method, invoice history).
3. **Settings → Emails**: switch on emails to customers for successful payments, so Stripe emails the receipt.
4. Put `STRIPE_SECRET_KEY` (test), `STRIPE_WEBHOOK_SECRET` in the environment. Locally you can forward events with the Stripe CLI:
   `stripe listen --forward-to localhost:9431/stripe/webhook` (it prints a `whsec_…` to use).
5. Test card `4242 4242 4242 4242`, any future date, any CVC. Check: checkout → return page → premium active → invoice with PDF and
   receipt on the Billing page → Manage billing → cancel → "will not renew" → (Stripe test clocks, or the dashboard) renewal and a
   failed payment (`4000 0000 0000 0341`) → full refund cancels premium.
6. Only then swap in live keys and a live webhook endpoint. Use the same Stripe account as checkmysitemap if you like: a separate
   restricted key and a separate webhook endpoint keep the two apart.

## Honest limits

- Verified here: our logic and the shape of every request we make (tests use a fake gateway and the real signature check). **Not**
  verified: Stripe's servers accepting those requests, receipts for your API version, and the portal. The test-mode run above is how to
  confirm it. If a receipt link is missing, the *View online* link (Stripe's invoice page) still carries the invoice and receipt.
- Invoice numbers, VAT wording and the business details on invoices come from your Stripe settings (Settings → Business, Billing → Invoices).
- Disputes (chargebacks) are not handled automatically; check the Stripe dashboard.
- Manual activation by the site admin stays available, for bank transfers and exceptions. Those clubs have no Stripe invoice; the site
  admin issues it.

## Tests

`tests/test_billing_stripe.py`: price, off-by-default, checkout (own club only, no club id from the browser, existing customer reused,
double subscription refused, outage handled), signature failures (missing, wrong, stale, altered, no secret), CSRF-free signed delivery,
linking and its safeguards, paying, replays and duplicates, renewals and old-invoice replays, early arrival retried, our records beating
metadata, failed payment, cancellation, full and partial refund, no reactivation from an old refunded invoice, return-page rules, portal,
refresh, invoice and receipt links (own club only), site-admin-only set-up details, dashboard button, request shape for the real gateway
(yearly EUR price, club on the session, tax and customer fields) and no card data in the database.
