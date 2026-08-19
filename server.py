import hmac
import json
import logging
import os
import secrets
import time
import uuid
from functools import wraps
from pathlib import Path

import redis
import stripe
from flask import Flask, jsonify, request, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("diamond")

app = Flask(__name__, static_folder="assets", static_url_path="/assets")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "0").lower() in {"1", "true", "yes"},
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
    MAX_CONTENT_LENGTH=64 * 1024,
)

REDIS_URL = (
    os.getenv("REDIS_URL")
    or os.getenv("REDIT_URL")
    or os.getenv("redit_url")
    or os.getenv("REDIS_PUBLIC_URL")
    or "redis://localhost:6379/0"
)
db = redis.Redis.from_url(
    REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=5,
    socket_timeout=5,
    health_check_interval=30,
)

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
DEFAULT_STOCK = max(0, int(os.getenv("DEFAULT_STOCK", "0")))
CHECKOUT_TTL_SECONDS = 30 * 60

PRODUCTS = {
    "bandi-hyal": {"name": "Moisturising Concentrate with Hyaluronic Acid", "brand": "BANDI Professional", "price_pence": 1300},
    "bandi-butter": {"name": "Emollient Cleansing Butter 2-in-1", "brand": "BANDI Professional", "price_pence": 1200},
    "bandi-emulsion": {"name": "Deeply Moisturising Emulsion", "brand": "BANDI Professional", "price_pence": 1750},
    "bandi-peptide": {"name": "Rejuvenating Peptide Cream", "brand": "BANDI Professional", "price_pence": 2700},
    "bandi-spf": {"name": "pre-D3 Advanced Moisturising Cream SPF 50", "brand": "BANDI Professional", "price_pence": 2100},
    "now-collagen": {"name": "Collagen Peptides Powder, 227 g", "brand": "NOW Foods", "price_pence": 1895},
    "now-hyaluronic": {"name": "Hyaluronic Acid with MSM, 60 Veg Capsules", "brand": "NOW Foods", "price_pence": 1595},
    "now-biotin": {"name": "Biotin 5,000 mcg, 60 Veg Capsules", "brand": "NOW Foods", "price_pence": 895},
    "now-d3k2": {"name": "Vitamin D3 & K2, 120 Capsules", "brand": "NOW Foods", "price_pence": 1195},
    "now-c1000": {"name": "C-1000, 60 Tablets", "brand": "NOW Foods", "price_pence": 995},
    "now-omega": {"name": "Omega-3 Fish Oil 1,000 mg, 100 Softgels", "brand": "NOW Foods", "price_pence": 1095},
    "now-czd": {"name": "C-1000 Zinc & D-3, 100 Veg Capsules", "brand": "NOW Foods", "price_pence": 1695},
    "now-folic": {"name": "Folic Acid with Vitamin B-12, 250 Tablets", "brand": "NOW Foods", "price_pence": 895},
}

ALLOWED_PAGES = {
    "index.html",
    "services.html",
    "about.html",
    "shop.html",
    "product.html",
    "contact.html",
    "checkout.html",
    "checkout-success.html",
    "admin.html",
}


def _json(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _load_json(value, default=None):
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def ensure_inventory():
    pipe = db.pipeline()
    for product_id in PRODUCTS:
        pipe.setnx(f"stock:{product_id}", DEFAULT_STOCK)
    pipe.execute()


def stock_for(product_id):
    value = db.get(f"stock:{product_id}")
    return int(value or 0)


def public_product(product_id, product):
    return {
        "id": product_id,
        "name": product["name"],
        "brand": product["brand"],
        "price": product["price_pence"] / 100,
        "price_pence": product["price_pence"],
        "stock": stock_for(product_id),
    }


def save_record(kind, record_id, data):
    ts = float(data.get("created_ts") or time.time())
    pipe = db.pipeline()
    pipe.set(f"{kind}:{record_id}", _json(data))
    pipe.zadd(f"{kind}:index", {record_id: ts})
    pipe.execute()


def get_record(kind, record_id):
    return _load_json(db.get(f"{kind}:{record_id}"))


def list_records(kind, limit=250):
    ids = db.zrevrange(f"{kind}:index", 0, max(0, limit - 1))
    if not ids:
        return []
    values = db.mget([f"{kind}:{record_id}" for record_id in ids])
    return [item for item in (_load_json(value) for value in values) if item]


def delete_record(kind, record_id):
    pipe = db.pipeline()
    pipe.delete(f"{kind}:{record_id}")
    pipe.zrem(f"{kind}:index", record_id)
    pipe.execute()


def update_record(kind, record_id, changes):
    key = f"{kind}:{record_id}"
    with db.lock(f"lock:{key}", timeout=8, blocking_timeout=3):
        data = get_record(kind, record_id)
        if not data:
            return None
        data.update(changes)
        db.set(key, _json(data))
        return data


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("diamond_admin"):
            return jsonify(error="admin authentication required"), 401
        return fn(*args, **kwargs)
    return wrapper


def clean_text(value, limit):
    return str(value or "").strip()[:limit]


def normalise_cart(raw_items):
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("Your bag is empty.")
    if len(raw_items) > 40:
        raise ValueError("Too many line items.")

    items = []
    seen = {}
    for raw in raw_items:
        product_id = clean_text((raw or {}).get("id"), 80)
        if product_id not in PRODUCTS:
            raise ValueError(f"Unknown product: {product_id or 'missing id'}")
        try:
            qty = int((raw or {}).get("qty", 1))
        except (TypeError, ValueError):
            raise ValueError("Invalid quantity.")
        if qty < 1 or qty > 25:
            raise ValueError("Quantity must be between 1 and 25.")
        seen[product_id] = seen.get(product_id, 0) + qty

    for product_id, qty in seen.items():
        product = PRODUCTS[product_id]
        items.append({
            "id": product_id,
            "name": product["name"],
            "brand": product["brand"],
            "qty": qty,
            "price_pence": product["price_pence"],
            "line_total_pence": product["price_pence"] * qty,
        })
    return items


def reserve_inventory(items):
    ensure_inventory()
    lock = db.lock("inventory:checkout", timeout=12, blocking_timeout=5)
    if not lock.acquire(blocking=True):
        raise RuntimeError("Inventory is busy. Please try again.")
    try:
        current = {item["id"]: stock_for(item["id"]) for item in items}
        shortages = [
            {"id": item["id"], "name": item["name"], "requested": item["qty"], "available": current[item["id"]]}
            for item in items
            if current[item["id"]] < item["qty"]
        ]
        if shortages:
            return shortages
        pipe = db.pipeline()
        for item in items:
            pipe.decrby(f"stock:{item['id']}", item["qty"])
        pipe.execute()
        return []
    finally:
        try:
            lock.release()
        except redis.exceptions.LockError:
            pass


def restore_inventory(order):
    if not order or order.get("inventory_restored") or order.get("payment_status") == "paid":
        return order

    with db.lock("inventory:checkout", timeout=12, blocking_timeout=5):
        fresh = get_record("order", order["id"])
        if not fresh or fresh.get("inventory_restored") or fresh.get("payment_status") == "paid":
            return fresh or order
        pipe = db.pipeline()
        for item in fresh.get("items", []):
            pipe.incrby(f"stock:{item['id']}", int(item["qty"]))
        fresh["inventory_restored"] = True
        fresh["updated_at"] = int(time.time())
        pipe.set(f"order:{fresh['id']}", _json(fresh))
        pipe.execute()
        return fresh


def mark_paid(order_id, stripe_session):
    with db.lock(f"lock:order:{order_id}", timeout=8, blocking_timeout=3):
        order = get_record("order", order_id)
        if not order:
            return None
        if order.get("inventory_restored"):
            order["status"] = "review"
            order["payment_status"] = "paid_after_release"
        else:
            order["status"] = "paid"
            order["payment_status"] = "paid"
        order["stripe_session_id"] = stripe_session.get("id") or order.get("stripe_session_id")
        order["stripe_payment_intent"] = stripe_session.get("payment_intent")
        order["paid_at"] = int(time.time())
        order["updated_at"] = int(time.time())
        db.set(f"order:{order_id}", _json(order))
        return order


def order_id_from_stripe_session(stripe_session):
    metadata = stripe_session.get("metadata") or {}
    return metadata.get("order_id") or stripe_session.get("client_reference_id")


@app.before_request
def bootstrap():
    if request.path.startswith("/api/") and request.path != "/api/stripe/webhook":
        try:
            ensure_inventory()
        except redis.RedisError:
            log.exception("Redis unavailable")
            return jsonify(error="Store data service is temporarily unavailable."), 503


@app.get("/")
def home():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/<path:filename>")
def page(filename):
    if filename not in ALLOWED_PAGES:
        return jsonify(error="not found"), 404
    return send_from_directory(BASE_DIR, filename)


@app.get("/health")
def health():
    try:
        db.ping()
        redis_ok = True
    except redis.RedisError:
        redis_ok = False
    return jsonify(ok=redis_ok, redis=redis_ok, stripe=bool(stripe.api_key)), (200 if redis_ok else 503)


@app.get("/api/products")
def products():
    return jsonify(products=[public_product(product_id, product) for product_id, product in PRODUCTS.items()])


@app.post("/api/messages")
def create_message():
    payload = request.get_json(silent=True) or {}
    name = clean_text(payload.get("name"), 120)
    email = clean_text(payload.get("email"), 200)
    if not name or not email or "@" not in email:
        return jsonify(error="Name and a valid email are required."), 400

    message_id = uuid.uuid4().hex
    now = int(time.time())
    record = {
        "id": message_id,
        "name": name,
        "email": email,
        "phone": clean_text(payload.get("phone"), 60),
        "treatment": clean_text(payload.get("treatment"), 120),
        "message": clean_text(payload.get("message"), 3000),
        "status": "new",
        "created_at": now,
        "created_ts": now,
    }
    save_record("message", message_id, record)
    return jsonify(ok=True, id=message_id), 201


@app.post("/api/checkout")
def create_checkout():
    if not stripe.api_key:
        return jsonify(error="Stripe is not configured on the server."), 503

    payload = request.get_json(silent=True) or {}
    try:
        items = normalise_cart(payload.get("items"))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    name = clean_text(payload.get("name"), 120)
    email = clean_text(payload.get("email"), 200)
    if not name or not email or "@" not in email:
        return jsonify(error="Name and a valid email are required."), 400

    shortages = reserve_inventory(items)
    if shortages:
        return jsonify(error="Some items no longer have enough stock.", shortages=shortages), 409

    order_id = uuid.uuid4().hex
    now = int(time.time())
    total_pence = sum(item["line_total_pence"] for item in items)
    order = {
        "id": order_id,
        "name": name,
        "email": email,
        "phone": clean_text(payload.get("phone"), 60),
        "notes": clean_text(payload.get("notes"), 2000),
        "items": items,
        "total_pence": total_pence,
        "total": total_pence / 100,
        "currency": "gbp",
        "status": "awaiting_payment",
        "payment_status": "pending",
        "inventory_restored": False,
        "created_at": now,
        "created_ts": now,
        "updated_at": now,
    }
    save_record("order", order_id, order)

    base_url = request.url_root.rstrip("/")
    try:
        checkout = stripe.checkout.Session.create(
            mode="payment",
            client_reference_id=order_id,
            customer_email=email,
            line_items=[
                {
                    "price_data": {
                        "currency": "gbp",
                        "product_data": {
                            "name": item["name"],
                            "metadata": {"product_id": item["id"]},
                        },
                        "unit_amount": item["price_pence"],
                    },
                    "quantity": item["qty"],
                }
                for item in items
            ],
            metadata={"order_id": order_id, "customer_name": name[:100]},
            success_url=f"{base_url}/checkout-success.html?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base_url}/checkout.html?cancelled=1",
            expires_at=int(time.time()) + CHECKOUT_TTL_SECONDS,
            allow_promotion_codes=False,
        )
    except Exception:
        log.exception("Stripe checkout session creation failed")
        update_record("order", order_id, {"status": "payment_setup_failed", "payment_status": "failed"})
        restore_inventory(get_record("order", order_id))
        return jsonify(error="Payment could not be started. Please try again."), 502

    update_record("order", order_id, {"stripe_session_id": checkout.id, "updated_at": int(time.time())})
    db.set(f"stripe_session:{checkout.id}", order_id, ex=60 * 60 * 24 * 7)
    return jsonify(url=checkout.url, order_id=order_id)


@app.get("/api/checkout/status")
def checkout_status():
    session_id = clean_text(request.args.get("session_id"), 200)
    if not session_id:
        return jsonify(error="session_id is required"), 400

    order_id = db.get(f"stripe_session:{session_id}")
    order = get_record("order", order_id) if order_id else None
    if not order:
        return jsonify(error="Order not found."), 404
    return jsonify(
        id=order["id"],
        status=order.get("status"),
        payment_status=order.get("payment_status"),
        total=order.get("total"),
        currency=order.get("currency", "gbp"),
    )


@app.post("/api/stripe/webhook")
def stripe_webhook():
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify(error="Stripe webhook secret is not configured."), 503

    payload = request.get_data(cache=False)
    signature = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        return jsonify(error="invalid webhook signature"), 400

    event_type = event["type"]
    stripe_session = event["data"]["object"]
    order_id = order_id_from_stripe_session(stripe_session)

    if order_id:
        if event_type in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
            payment_status = stripe_session.get("payment_status")
            if event_type == "checkout.session.async_payment_succeeded" or payment_status == "paid":
                mark_paid(order_id, stripe_session)
            else:
                update_record("order", order_id, {
                    "status": "processing_payment",
                    "payment_status": payment_status or "processing",
                    "updated_at": int(time.time()),
                })
        elif event_type in {"checkout.session.expired", "checkout.session.async_payment_failed"}:
            order = update_record("order", order_id, {
                "status": "expired" if event_type.endswith("expired") else "payment_failed",
                "payment_status": "expired" if event_type.endswith("expired") else "failed",
                "updated_at": int(time.time()),
            })
            restore_inventory(order)

    return jsonify(received=True)


@app.post("/api/admin/login")
def admin_login():
    if not ADMIN_PASSWORD:
        return jsonify(error="ADMIN_PASSWORD is not configured."), 503

    payload = request.get_json(silent=True) or {}
    supplied_email = clean_text(payload.get("email"), 200).lower()
    supplied_password = str(payload.get("password") or "")

    email_ok = not ADMIN_EMAIL or hmac.compare_digest(supplied_email, ADMIN_EMAIL)
    password_ok = hmac.compare_digest(supplied_password, ADMIN_PASSWORD)
    if not (email_ok and password_ok):
        time.sleep(0.25)
        return jsonify(error="Invalid sign-in."), 401

    session.clear()
    session["diamond_admin"] = True
    session["admin_email"] = supplied_email
    session.permanent = True
    return jsonify(ok=True)


@app.post("/api/admin/logout")
def admin_logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/admin/session")
def admin_session():
    return jsonify(authenticated=bool(session.get("diamond_admin")))


@app.get("/api/admin/messages")
@admin_required
def admin_messages():
    return jsonify(records=list_records("message"))


@app.get("/api/admin/orders")
@admin_required
def admin_orders():
    return jsonify(records=list_records("order"))


@app.patch("/api/admin/<kind>/<record_id>")
@admin_required
def admin_update(kind, record_id):
    if kind not in {"messages", "orders"}:
        return jsonify(error="Invalid record type."), 404
    singular = "message" if kind == "messages" else "order"
    payload = request.get_json(silent=True) or {}
    status = clean_text(payload.get("status"), 60)
    allowed = {
        "message": {"new", "handled"},
        "order": {"fulfilled", "cancelled", "review"},
    }
    if status not in allowed[singular]:
        return jsonify(error="Invalid status."), 400

    current = get_record(singular, record_id)
    if not current:
        return jsonify(error="Record not found."), 404

    if singular == "order":
        if status == "fulfilled" and current.get("payment_status") != "paid":
            return jsonify(error="Only Stripe-paid orders can be marked fulfilled."), 409
        if status == "cancelled" and current.get("payment_status") == "paid":
            return jsonify(error="Paid orders must be refunded in Stripe before cancellation."), 409

    record = update_record(singular, record_id, {"status": status, "updated_at": int(time.time())})
    if singular == "order" and status == "cancelled":
        record = restore_inventory(record)
    return jsonify(record=record)


@app.delete("/api/admin/<kind>/<record_id>")
@admin_required
def admin_delete(kind, record_id):
    if kind not in {"messages", "orders"}:
        return jsonify(error="Invalid record type."), 404
    singular = "message" if kind == "messages" else "order"
    record = get_record(singular, record_id)
    if not record:
        return jsonify(error="Record not found."), 404
    if singular == "order" and record.get("payment_status") == "pending" and not record.get("inventory_restored"):
        return jsonify(error="Pending payment orders cannot be deleted until they expire or are cancelled."), 409
    delete_record(singular, record_id)
    return jsonify(ok=True)


@app.get("/api/admin/inventory")
@admin_required
def admin_inventory():
    ensure_inventory()
    return jsonify(products=[public_product(product_id, product) for product_id, product in PRODUCTS.items()])


@app.put("/api/admin/inventory/<product_id>")
@admin_required
def admin_set_inventory(product_id):
    if product_id not in PRODUCTS:
        return jsonify(error="Unknown product."), 404
    payload = request.get_json(silent=True) or {}
    try:
        stock = int(payload.get("stock"))
    except (TypeError, ValueError):
        return jsonify(error="Stock must be a whole number."), 400
    if stock < 0 or stock > 1_000_000:
        return jsonify(error="Stock must be between 0 and 1,000,000."), 400
    db.set(f"stock:{product_id}", stock)
    return jsonify(product=public_product(product_id, PRODUCTS[product_id]))


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG") == "1")
