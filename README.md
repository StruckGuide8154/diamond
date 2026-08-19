# Diamond Beauty

Diamond Beauty & Hair Clinic storefront and booking site, now served by a single Flask application (`server.py`).

## Architecture

- **Web server:** Flask serves the existing HTML/CSS/JS and all API routes.
- **Persistence:** Railway Redis via `REDIS_URL`. No Supabase.
- **Payments:** Stripe Checkout Sessions created server-side.
- **Inventory:** Redis-backed stock. Stock is reserved before Stripe Checkout opens and returned when an unpaid Checkout Session expires or an admin cancels it.
- **Admin:** `/admin.html` uses a server-side Flask session. The browser never receives Redis or Stripe secrets.
- **Hosting:** Railway. There is no GitHub Pages or rooted.cloud runtime dependency.

## Railway setup

1. Create or open the Railway project for this repo.
2. Add a Redis service.
3. On the app service, set `REDIS_URL` to the Redis service reference, normally `${{Redis.REDIS_URL}}`.
4. Add the environment variables below.
5. Deploy. `railway.json` starts Gunicorn with `server:app` and uses `/health` as the health check.
6. In Stripe Workbench/Webhooks, create a webhook destination: `https://YOUR-RAILWAY-DOMAIN/api/stripe/webhook`.
7. Subscribe it to `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `checkout.session.async_payment_failed`, and `checkout.session.expired`.
8. Put the webhook signing secret into `STRIPE_WEBHOOK_SECRET`.
9. Open `/admin.html` and set real stock before selling. New Redis databases default products to `DEFAULT_STOCK`, which defaults to `0`.

## Required environment variables

```text
REDIS_URL=${{Redis.REDIS_URL}}
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
ADMIN_PASSWORD=use-a-long-random-password
SECRET_KEY=use-another-long-random-value
```

Optional:

```text
ADMIN_EMAIL=admin@example.com
DEFAULT_STOCK=0
SESSION_COOKIE_SECURE=1
```

`REDIS_URL` is the Railway-native name. `server.py` also accepts `REDIT_URL`, lowercase `redit_url`, or `REDIS_PUBLIC_URL` as fallbacks, but the private Railway `REDIS_URL` is preferred when Redis and the app are in the same project.

## Payment and stock flow

1. The browser submits product IDs and quantities only.
2. `server.py` rebuilds the cart from its own canonical catalogue and prices.
3. Redis stock is checked and reserved atomically.
4. The server creates a Stripe-hosted Checkout Session and returns its URL.
5. Stripe webhooks are the source of truth for payment state.
6. A successful payment keeps the reserved stock consumed.
7. An expired or failed Checkout Session restores the reserved stock idempotently.

This prevents a customer from changing prices in DevTools and keeps abandoned Stripe sessions from permanently eating inventory.

## Local development

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Run Redis locally, then:

```bash
set REDIS_URL=redis://localhost:6379/0
set STRIPE_SECRET_KEY=sk_test_...
set STRIPE_WEBHOOK_SECRET=whsec_...
set ADMIN_PASSWORD=dev-password
set SECRET_KEY=dev-secret
python server.py
```

On macOS/Linux, use `export` instead of `set`.

For local webhook testing:

```bash
stripe listen --forward-to localhost:8080/api/stripe/webhook
```

Then open `http://localhost:8080/`.
