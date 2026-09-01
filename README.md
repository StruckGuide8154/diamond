# Diamond Beauty

Diamond Beauty & Hair Clinic storefront, booking form and admin dashboard, served
by a single Flask application (`server.py`). There is no GitHub Pages build and no
static product list: everything the site shows is served and controlled by Flask.

## Run it

```bash
python -m pip install -r requirements.txt
export REDIS_URL=redis://localhost:6379/0
export ADMINUSER=admin
export ADMINPASS=a-long-random-password
export SECRET_KEY=another-long-random-value
export APISEC=sk_test_...        # Stripe secret key
export APIPUB=pk_test_...        # Stripe publishable key
python server.py                 # http://localhost:8080
```

In production, run it under Gunicorn (this is what `Procfile` and `railway.json` do):

```bash
gunicorn server:app --bind 0.0.0.0:$PORT
```

## Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `REDIS_URL` | yes | Redis connection. Products, images, stock, orders and booking requests live here. |
| `ADMINUSER` | yes | Username for `/admin`. |
| `ADMINPASS` | yes | Password for `/admin`. Without it the dashboard is disabled. |
| `APISEC` | yes to sell | Stripe **secret** key. Used server-side only. |
| `APIPUB` | optional | Stripe **publishable** key, exposed at `/api/config`. |
| `STRIPE_WEBHOOK_SECRET` | yes to sell | Verifies Stripe webhook signatures. |
| `SECRET_KEY` | recommended | Signs the admin session cookie. Derived from `ADMINPASS` if unset. |
| `SESSION_COOKIE_SECURE` | recommended | Set to `1` when served over HTTPS. |
| `DEFAULT_STOCK` | optional | Stock given to the seeded catalogue on a fresh database. Defaults to `0`. |

Legacy names still work as fallbacks: `ADMIN_USER`/`ADMIN_EMAIL`, `ADMIN_PASSWORD`,
`STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `REDIT_URL`, `REDIS_PUBLIC_URL`.

## The admin dashboard

`/admin`, signed in with `ADMINUSER` / `ADMINPASS`.

- **Products** — add, edit, duplicate-safe slugs, reorder, publish/unpublish and
  delete products. Every field the site renders is editable: name, brand,
  category, badge, price, stock, description, manufacturer link and image. A live
  preview of the boutique card sits beside the form, and **Preview** opens the
  real product page. Unpublished products are hidden from the site but visible to
  a signed-in admin, so a draft can be reviewed before it goes live.
- **Images** — upload PNG, JPEG, WebP, GIF or AVIF (up to 4 MB). Uploads are
  stored in Redis and served from `/media/<id>`, so they survive redeploys on
  hosts with an ephemeral filesystem. An https:// image URL can be pasted instead.
- **Stock control** — set stock per product in one list.
- **Orders** — Stripe payment state, fulfilment, cancellation (which returns the
  reserved stock) and deletion.
- **Booking requests** — everything submitted through the contact form.

Products appear on the home page (`data-products="8"`), across the boutique at
`/shop`, and each has its own page at `/product?id=<id>`. Category filter buttons
are generated from whatever categories are in use.

## Security

- Admin sign-in uses constant-time comparison, a rate limit of 8 attempts per IP
  per 15 minutes, an 8-hour idle timeout, and sessions that are invalidated when
  `ADMINUSER`/`ADMINPASS` change.
- Admin writes require a per-session CSRF token (`X-CSRF-Token`); all
  state-changing requests are additionally origin-checked. The Stripe webhook is
  exempt and verified by signature instead.
- Session cookies are HttpOnly, SameSite=Lax and (with `SESSION_COOKIE_SECURE=1`)
  Secure.
- Responses carry CSP, `X-Content-Type-Options`, `X-Frame-Options: DENY`,
  Referrer-Policy, Permissions-Policy and HSTS over HTTPS.
- Uploads are type-checked by MIME **and** magic number, capped at 4 MB, and
  served with `nosniff`. SVG uploads are rejected because they can carry script.
- Product images and links must be a local path or an `https://` (source: `http(s)://`) URL.
- All catalogue copy is HTML-escaped before it reaches the page.
- Prices, stock and availability are re-derived on the server at checkout, so a
  tampered cart cannot change what is charged.

## Payment and stock flow

1. The browser submits product IDs and quantities only.
2. `server.py` rebuilds the cart from Redis, using its own prices.
3. Stock is checked and reserved under a Redis lock.
4. A Stripe-hosted Checkout Session is created and its URL returned.
5. Stripe webhooks are the source of truth for payment state.
6. A paid session keeps the reserved stock consumed; an expired, failed or
   cancelled one returns it, idempotently.

Point a Stripe webhook at `https://YOUR-DOMAIN/api/stripe/webhook` and subscribe
to `checkout.session.completed`, `checkout.session.async_payment_succeeded`,
`checkout.session.async_payment_failed` and `checkout.session.expired`.

Locally: `stripe listen --forward-to localhost:8080/api/stripe/webhook`.

## Routes

| Route | Description |
| --- | --- |
| `/`, `/services`, `/about`, `/shop`, `/product`, `/contact`, `/checkout`, `/checkout-success` | Site pages |
| `/admin` | Admin dashboard |
| `/media/<id>` | Uploaded product images |
| `/health` | Liveness. Always 200 while the app is up; the body reports Redis, Stripe and admin state |
| `/ready` | Readiness. 503 until Redis answers |
| `/api/products`, `/api/products/<id>`, `/api/config` | Public catalogue |
| `/api/messages`, `/api/checkout`, `/api/checkout/status` | Public storefront actions |
| `/api/stripe/webhook` | Stripe events |
| `/api/admin/...` | Session, products, media, inventory, orders, messages |
