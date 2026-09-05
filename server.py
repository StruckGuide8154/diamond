"""Diamond Beauty storefront: a single Flask application.

Serves the site, the /admin dashboard and every API route. Product catalogue,
imagery, stock, orders and booking requests all live in Redis. Stripe Checkout
is created server-side using the APISEC secret key; the browser never receives
a secret.
"""

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import unicodedata
import uuid
from datetime import date
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

import redis
import stripe
from flask import Flask, Response, jsonify, redirect, request, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix

from catalogue_seed import SEED_PRODUCTS

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("diamond")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


def env(*names, default=""):
    """First non-empty value among the given environment variable names."""
    for name in names:
        value = os.getenv(name)
        if value:
            return value.strip()
    return default


def env_flag(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --- configuration -----------------------------------------------------------

ADMIN_USER = env("ADMINUSER", "ADMIN_USER", "ADMIN_EMAIL")
ADMIN_PASS = env("ADMINPASS", "ADMIN_PASS", "ADMIN_PASSWORD")
STRIPE_SECRET_KEY = env("APISEC", "STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = env("APIPUB", "STRIPE_PUBLISHABLE_KEY")
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET", "APIWEBHOOK")
REDIS_URL = env(
    "REDIS_URL", "REDIT_URL", "redit_url", "REDIS_PUBLIC_URL",
    default="redis://localhost:6379/0",
)
DEFAULT_STOCK = max(0, int(os.getenv("DEFAULT_STOCK", "0") or 0))

# Stripe rejects expires_at below 30 minutes from creation. Asking for exactly
# the minimum races network latency and container clock drift, so leave slack.
CHECKOUT_TTL_SECONDS = 35 * 60
COLLECTION_MAX_DAYS = 90
MAX_UPLOAD_BYTES = 4 * 1024 * 1024
MAX_JSON_BYTES = 128 * 1024
MAX_PRODUCTS = 400
MAX_CATEGORIES = 60
SESSION_IDLE_SECONDS = 60 * 60 * 8
LOGIN_MAX_ATTEMPTS = 8
LOGIN_WINDOW_SECONDS = 15 * 60

ALLOWED_IMAGE_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}
DEFAULT_CATEGORIES = (
    ("bandi", "Bandi"),
    ("skincare", "Skincare"),
    ("wellness", "Wellness"),
    ("haircare", "Haircare"),
    ("accessories", "Accessories"),
    ("other", "Other"),
)

stripe.api_key = STRIPE_SECRET_KEY

if not ADMIN_PASS:
    log.warning("ADMINPASS is not set - the admin dashboard is disabled.")

app = Flask(__name__, static_folder="assets", static_url_path="/assets")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

SECRET_KEY = env("SECRET_KEY")
if not SECRET_KEY:
    if ADMIN_PASS:
        # Stable across gunicorn workers and restarts so admin sessions survive,
        # but still secret. Setting SECRET_KEY explicitly is strongly preferred.
        SECRET_KEY = hashlib.sha256(f"diamond:{ADMIN_USER}:{ADMIN_PASS}".encode()).hexdigest()
        log.warning("SECRET_KEY is not set - deriving one from ADMINPASS. Set SECRET_KEY in production.")
    else:
        SECRET_KEY = secrets.token_hex(32)
app.secret_key = SECRET_KEY

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=env_flag("SESSION_COOKIE_SECURE", False),
    SESSION_COOKIE_NAME="diamond_session",
    PERMANENT_SESSION_LIFETIME=SESSION_IDLE_SECONDS,
    MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES + 512 * 1024,
    JSON_SORT_KEYS=False,
    SEND_FILE_MAX_AGE_DEFAULT=3600,
)

db = redis.Redis.from_url(
    REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=5,
    socket_timeout=5,
    health_check_interval=30,
    retry_on_timeout=True,
)
PAGES = {
    "": "index.html",
    "index": "index.html",
    "services": "services.html",
    "about": "about.html",
    "shop": "shop.html",
    "product": "product.html",
    "contact": "contact.html",
    "checkout": "checkout.html",
    "checkout-success": "checkout-success.html",
    "admin": "admin.html",
}


# --- small helpers -----------------------------------------------------------

def _json(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _load_json(value, default=None):
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def clean_text(value, limit):
    text = str(value if value is not None else "").replace("\x00", "").strip()
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    return text[:limit]


def valid_email(value):
    return bool(re.fullmatch(r"[^@\s]+@[^@\s.]+\.[^@\s]{2,}", value or ""))


def valid_uk_postcode(value):
    """Deliberately loose: correct shape, no attempt to prove the postcode exists."""
    compact = re.sub(r"\s+", "", (value or "").upper())
    return bool(re.fullmatch(r"[A-Z]{1,2}\d[A-Z\d]?\d[A-Z]{2}", compact))


def format_postcode(value):
    compact = re.sub(r"\s+", "", (value or "").upper())
    return f"{compact[:-3]} {compact[-3:]}" if len(compact) > 3 else compact


def parse_fulfilment(payload):
    """Validate the delivery/collection half of a checkout payload.

    Returns the fields to merge into the order record. Raises ValueError with a
    customer-facing message when something is missing or malformed.
    """
    method = clean_text(payload.get("fulfilment"), 20).lower()
    if method not in {"delivery", "collection"}:
        raise ValueError("Choose delivery or collection.")

    if method == "delivery":
        line1 = clean_text(payload.get("address1"), 120)
        city = clean_text(payload.get("city"), 80)
        postcode = clean_text(payload.get("postcode"), 12)
        if not line1:
            raise ValueError("A delivery address is required.")
        if not city:
            raise ValueError("A town or city is required for delivery.")
        if not valid_uk_postcode(postcode):
            raise ValueError("Enter a valid UK postcode for delivery.")
        return {
            "fulfilment": "delivery",
            "address1": line1,
            "address2": clean_text(payload.get("address2"), 120),
            "city": city,
            "county": clean_text(payload.get("county"), 80),
            "postcode": format_postcode(postcode),
            "collection_date": "",
            "collection_ack": False,
        }

    raw_date = clean_text(payload.get("collection_date"), 10)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
        raise ValueError("Choose a collection date.")
    try:
        chosen = date.fromisoformat(raw_date)
    except ValueError:
        raise ValueError("Choose a valid collection date.")
    today = date.today()
    if chosen < today:
        raise ValueError("The collection date cannot be in the past.")
    if (chosen - today).days > COLLECTION_MAX_DAYS:
        raise ValueError(f"Collection dates are limited to {COLLECTION_MAX_DAYS} days ahead.")
    if not payload.get("collection_ack"):
        raise ValueError("Please confirm you understand how collection works.")

    return {
        "fulfilment": "collection",
        "address1": "",
        "address2": "",
        "city": "",
        "county": "",
        "postcode": "",
        "collection_date": chosen.isoformat(),
        "collection_ack": True,
    }


def slugify(value, fallback="product"):
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (text or fallback)[:60]


def redis_ok():
    try:
        db.ping()
        return True
    except redis.RedisError:
        return False


def client_ip():
    return (request.remote_addr or "unknown")[:64]


def rate_limited(bucket, limit, window):
    """Fixed-window counter in Redis. Fails open if Redis is unavailable."""
    key = f"rl:{bucket}:{int(time.time() // window)}"
    try:
        pipe = db.pipeline()
        pipe.incr(key)
        pipe.expire(key, window)
        count = pipe.execute()[0]
        return int(count) > limit
    except redis.RedisError:
        return False


def json_body():
    if request.content_length and request.content_length > MAX_JSON_BYTES:
        return None
    return request.get_json(silent=True) or {}


# --- request guards ----------------------------------------------------------

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@app.before_request
def guard_request():
    # Stripe signs its own webhook; it is deliberately exempt from origin/CSRF.
    if request.path == "/api/stripe/webhook":
        return None

    if request.method not in SAFE_METHODS:
        origin = request.headers.get("Origin") or request.headers.get("Referer")
        if origin:
            parsed = urlparse(origin)
            if parsed.netloc and parsed.netloc != request.host:
                return jsonify(error="Cross-origin request rejected."), 403

    if request.path.startswith("/api/admin/") and request.method not in SAFE_METHODS:
        if request.path not in {"/api/admin/login", "/api/admin/logout"}:
            token = request.headers.get("X-CSRF-Token", "")
            expected = session.get("csrf_token", "")
            if not expected or not hmac.compare_digest(token, expected):
                return jsonify(error="Invalid or missing CSRF token."), 403

    if request.path.startswith("/api/") and request.path != "/api/stripe/webhook":
        if not redis_ok():
            log.warning("Redis unavailable for %s", request.path)
            return jsonify(error="Store data service is temporarily unavailable."), 503
    return None


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=(self)")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "base-uri 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "img-src 'self' data: https:; "
        "font-src 'self' data: https://fonts.gstatic.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "script-src 'self' 'unsafe-inline' https://js.stripe.com; "
        "connect-src 'self' https://api.stripe.com; "
        "frame-src https://js.stripe.com https://hooks.stripe.com; "
        "form-action 'self' https://checkout.stripe.com",
    )
    if request.is_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.path.startswith("/api/") or request.path == "/admin":
        response.headers["Cache-Control"] = "no-store"
    return response


@app.errorhandler(404)
def not_found(_err):
    if request.path.startswith("/api/"):
        return jsonify(error="Not found."), 404
    return send_from_directory(BASE_DIR, "index.html"), 404


@app.errorhandler(413)
def too_large(_err):
    return jsonify(error="Upload is too large."), 413


@app.errorhandler(500)
def server_error(_err):
    return jsonify(error="Something went wrong."), 500


# --- catalogue ---------------------------------------------------------------

def product_key(product_id):
    return f"product:{product_id}"


def category_key(category_id):
    return f"category:{category_id}"


def seed_categories():
    """Create the default category list and migrate BANDI products once."""
    seed_key = "categories:seeded"
    if not db.exists(seed_key):
        with db.lock(f"lock:{seed_key}", timeout=15, blocking_timeout=5):
            if not db.exists(seed_key):
                pipe = db.pipeline()
                for position, (category_id, name) in enumerate(DEFAULT_CATEGORIES):
                    pipe.setnx(category_key(category_id), _json({"id": category_id, "name": name}))
                    pipe.zadd("category:index", {category_id: position}, nx=True)
                pipe.set(seed_key, int(time.time()))
                pipe.execute()

    migration_key = "catalogue:migration:bandi-category:v1"
    if db.exists(migration_key):
        return
    with db.lock(f"lock:{migration_key}", timeout=15, blocking_timeout=5):
        if db.exists(migration_key):
            return
        now = int(time.time())
        migration = db.pipeline()
        for product_id in all_product_ids():
            record = get_product(product_id)
            if not record:
                continue
            if product_id.startswith("bandi-") or "bandi" in record.get("brand", "").lower():
                record["category"] = "bandi"
                record["updated_at"] = now
                migration.set(product_key(product_id), _json(record))
        migration.set(migration_key, now)
        migration.execute()


def list_categories():
    ids = db.zrange("category:index", 0, -1)
    if not ids:
        return []
    raw = db.mget([category_key(category_id) for category_id in ids])
    counts = {}
    for product_id in all_product_ids():
        record = get_product(product_id)
        if record:
            category_id = record.get("category", "other")
            counts[category_id] = counts.get(category_id, 0) + 1
    return [
        {**record, "product_count": counts.get(record["id"], 0)}
        for record in (_load_json(value) for value in raw)
        if record
    ]


def category_exists(category_id):
    return bool(category_id and db.exists(category_key(category_id)))


def category_name(category_id):
    record = _load_json(db.get(category_key(category_id)), {})
    return record.get("name") or category_id.replace("-", " ").title()


def seed_catalogue():
    """Populate the catalogue once, the first time this Redis database is used."""
    if db.exists("catalogue:seeded"):
        seed_categories()
        return
    if not db.setnx("catalogue:seeded", int(time.time())):
        seed_categories()
        return
    now = int(time.time())
    pipe = db.pipeline()
    for position, seed in enumerate(SEED_PRODUCTS):
        record = dict(seed)
        record.update({"active": True, "created_at": now, "updated_at": now, "position": position})
        pipe.set(product_key(record["id"]), _json(record))
        pipe.zadd("product:index", {record["id"]: position})
        pipe.setnx(f"stock:{record['id']}", DEFAULT_STOCK)
    pipe.execute()
    seed_categories()
    log.info("Seeded catalogue with %d products", len(SEED_PRODUCTS))


def stock_for(product_id):
    try:
        return max(0, int(db.get(f"stock:{product_id}") or 0))
    except (TypeError, ValueError):
        return 0


def get_product(product_id):
    if not product_id:
        return None
    return _load_json(db.get(product_key(product_id)))


def all_product_ids():
    return db.zrange("product:index", 0, -1)


def list_products(include_inactive=False):
    ids = all_product_ids()
    if not ids:
        return []
    raw = db.mget([product_key(pid) for pid in ids])
    stocks = db.mget([f"stock:{pid}" for pid in ids])
    products = []
    for record, stock in zip((_load_json(value) for value in raw), stocks):
        if not record:
            continue
        if not include_inactive and not record.get("active", True):
            continue
        products.append(public_product(record, stock))
    return products


def public_product(record, stock=None):
    if stock is None:
        stock = stock_for(record["id"])
    try:
        stock = max(0, int(stock or 0))
    except (TypeError, ValueError):
        stock = 0
    price_pence = int(record.get("price_pence") or 0)
    return {
        "id": record["id"],
        "name": record.get("name", ""),
        "brand": record.get("brand", ""),
        "description": record.get("description", ""),
        "category": record.get("category", "other"),
        "category_name": category_name(record.get("category", "other")),
        "tag": record.get("tag", ""),
        "image": record.get("image", ""),
        "source": record.get("source", ""),
        "price_pence": price_pence,
        "price": price_pence / 100,
        "stock": stock,
        "active": bool(record.get("active", True)),
        "position": int(record.get("position", 0)),
        "updated_at": record.get("updated_at"),
    }


IMAGE_PATH_RE = re.compile(r"^/(assets|media)/[A-Za-z0-9._/\-]{1,200}$")


def clean_image(value):
    """Accept a local asset/media path or an absolute https URL; nothing else."""
    image = clean_text(value, 500)
    if not image:
        return ""
    if IMAGE_PATH_RE.fullmatch(image):
        return image
    parsed = urlparse(image)
    if parsed.scheme == "https" and parsed.netloc:
        return image
    raise ValueError("Image must be an uploaded image or an https:// URL.")


def clean_source(value):
    source = clean_text(value, 500)
    if not source:
        return ""
    parsed = urlparse(source)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Source link must be an http(s) URL.")
    return source


def parse_price(value):
    """Accept pounds ("12.50") or an explicit pence integer."""
    if value is None or value == "":
        raise ValueError("A price is required.")
    try:
        pence = int(round(float(value) * 100))
    except (TypeError, ValueError):
        raise ValueError("Price must be a number.")
    if pence < 0 or pence > 1_000_000_00:
        raise ValueError("Price must be between 0 and 1,000,000.")
    return pence


def parse_stock(value):
    try:
        stock = int(value)
    except (TypeError, ValueError):
        raise ValueError("Stock must be a whole number.")
    if stock < 0 or stock > 1_000_000:
        raise ValueError("Stock must be between 0 and 1,000,000.")
    return stock


def product_from_payload(payload, existing=None):
    name = clean_text(payload.get("name"), 160)
    if not name:
        raise ValueError("A product name is required.")

    category = slugify(clean_text(payload.get("category"), 60), "other")
    if not category_exists(category):
        raise ValueError("Choose an existing category, or create one first.")

    now = int(time.time())
    record = dict(existing or {})
    record.update({
        "name": name,
        "brand": clean_text(payload.get("brand"), 120),
        "description": clean_text(payload.get("description"), 4000),
        "category": category,
        "tag": clean_text(payload.get("tag"), 40),
        "image": clean_image(payload.get("image")),
        "source": clean_source(payload.get("source")),
        "price_pence": parse_price(payload.get("price", payload.get("price_pence"))),
        "active": bool(payload.get("active", True)),
        "updated_at": now,
    })
    record.setdefault("created_at", now)
    return record


# --- generic records (orders, messages) --------------------------------------

def save_record(kind, record_id, data):
    ts = float(data.get("created_ts") or time.time())
    pipe = db.pipeline()
    pipe.set(f"{kind}:{record_id}", _json(data))
    pipe.zadd(f"{kind}:index", {record_id: ts})
    pipe.execute()


def get_record(kind, record_id):
    if not record_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(record_id)):
        return None
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


# --- authentication ----------------------------------------------------------

def is_admin():
    if not session.get("diamond_admin"):
        return False
    if session.get("admin_fingerprint") != admin_fingerprint():
        return False
    last_seen = float(session.get("last_seen") or 0)
    if time.time() - last_seen > SESSION_IDLE_SECONDS:
        session.clear()
        return False
    session["last_seen"] = time.time()
    return True


def admin_fingerprint():
    """Invalidate live sessions when the configured credentials change."""
    return hashlib.sha256(f"{ADMIN_USER}:{ADMIN_PASS}".encode()).hexdigest()[:32]


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_admin():
            return jsonify(error="Admin authentication required."), 401
        return fn(*args, **kwargs)
    return wrapper


# --- cart and inventory ------------------------------------------------------

def normalise_cart(raw_items):
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("Your bag is empty.")
    if len(raw_items) > 40:
        raise ValueError("Too many line items.")

    quantities = {}
    for raw in raw_items:
        product_id = clean_text((raw or {}).get("id"), 80)
        try:
            qty = int((raw or {}).get("qty", 1))
        except (TypeError, ValueError):
            raise ValueError("Invalid quantity.")
        if qty < 1 or qty > 25:
            raise ValueError("Quantity must be between 1 and 25.")
        quantities[product_id] = quantities.get(product_id, 0) + qty

    items = []
    for product_id, qty in quantities.items():
        record = get_product(product_id)
        if not record or not record.get("active", True):
            raise ValueError("One of the items in your bag is no longer available.")
        if qty > 25:
            raise ValueError("Quantity must be between 1 and 25.")
        price_pence = int(record.get("price_pence") or 0)
        items.append({
            "id": product_id,
            "name": record.get("name", ""),
            "brand": record.get("brand", ""),
            "qty": qty,
            "price_pence": price_pence,
            "line_total_pence": price_pence * qty,
        })
    if not items:
        raise ValueError("Your bag is empty.")
    return items


def reserve_inventory(items):
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


# --- pages -------------------------------------------------------------------

@app.get("/")
def home():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/admin")
def admin_page():
    return send_from_directory(BASE_DIR, "admin.html")


@app.get("/<page>")
def page(page):
    name = page[:-5] if page.endswith(".html") else page
    filename = PAGES.get(name)
    if not filename:
        return not_found(None)
    if name == "admin" and page.endswith(".html"):
        return redirect("/admin", code=301)
    return send_from_directory(BASE_DIR, filename)


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


@app.get("/health")
def health():
    """Liveness check for the platform.

    This deliberately stays 200 while Redis is unreachable: the process is up
    and serving, and failing the platform health check would take the whole
    deployment down (and roll it back) over a dependency that may simply be
    starting up. Redis state is reported in the body, and /ready is the strict
    check for anything that needs the store to be usable.
    """
    return jsonify(
        ok=True,
        redis=redis_ok(),
        stripe=bool(stripe.api_key),
        admin_configured=bool(ADMIN_PASS),
    )


@app.get("/ready")
def ready():
    """Readiness check: 503 until Redis answers."""
    ok = redis_ok()
    return jsonify(
        ok=ok,
        redis=ok,
        stripe=bool(stripe.api_key),
        admin_configured=bool(ADMIN_PASS),
    ), (200 if ok else 503)


@app.get("/media/<media_id>")
def media(media_id):
    if not re.fullmatch(r"[a-f0-9]{32}", media_id or ""):
        return not_found(None)
    meta = db.hgetall(f"media:{media_id}")
    if not meta:
        return not_found(None)
    try:
        blob = base64.b64decode(meta.get("data") or "")
    except (binascii.Error, ValueError):
        return not_found(None)
    content_type = meta.get("content_type", "application/octet-stream")
    if content_type not in ALLOWED_IMAGE_TYPES:
        content_type = "application/octet-stream"
    response = Response(blob, mimetype=content_type)
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    response.headers["Content-Disposition"] = "inline"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# --- public API --------------------------------------------------------------

@app.get("/api/config")
def public_config():
    return jsonify(
        stripe_publishable_key=STRIPE_PUBLISHABLE_KEY,
        stripe_ready=bool(stripe.api_key),
        currency="gbp",
    )


@app.get("/api/products")
def products():
    seed_catalogue()
    return jsonify(products=list_products())


@app.get("/api/categories")
def categories():
    seed_catalogue()
    return jsonify(categories=list_categories())


@app.get("/api/products/<product_id>")
def product_detail(product_id):
    record = get_product(clean_text(product_id, 80))
    if not record:
        return jsonify(error="Product not found."), 404
    if not record.get("active", True) and not is_admin():
        return jsonify(error="Product not found."), 404
    return jsonify(product=public_product(record))


@app.post("/api/messages")
def create_message():
    if rate_limited(f"messages:{client_ip()}", 12, 3600):
        return jsonify(error="Too many messages. Please try again later."), 429

    payload = json_body()
    if payload is None:
        return jsonify(error="Request body is too large."), 413
    name = clean_text(payload.get("name"), 120)
    email = clean_text(payload.get("email"), 200)
    if not name or not valid_email(email):
        return jsonify(error="Name and a valid email are required."), 400

    message_id = uuid.uuid4().hex
    now = int(time.time())
    save_record("message", message_id, {
        "id": message_id,
        "name": name,
        "email": email,
        "phone": clean_text(payload.get("phone"), 60),
        "treatment": clean_text(payload.get("treatment"), 120),
        "message": clean_text(payload.get("message"), 3000),
        "status": "new",
        "created_at": now,
        "created_ts": now,
    })
    return jsonify(ok=True, id=message_id), 201


@app.post("/api/checkout")
def create_checkout():
    if not stripe.api_key:
        return jsonify(error="Stripe is not configured on the server."), 503
    if rate_limited(f"checkout:{client_ip()}", 20, 3600):
        return jsonify(error="Too many checkout attempts. Please try again later."), 429

    payload = json_body()
    if payload is None:
        return jsonify(error="Request body is too large."), 413
    try:
        items = normalise_cart(payload.get("items"))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    name = clean_text(payload.get("name"), 120)
    email = clean_text(payload.get("email"), 200)
    if not name or not valid_email(email):
        return jsonify(error="Name and a valid email are required."), 400

    try:
        fulfilment = parse_fulfilment(payload)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    total_pence = sum(item["line_total_pence"] for item in items)
    if total_pence < 30:
        return jsonify(error="Order total is below the minimum card payment."), 400

    shortages = reserve_inventory(items)
    if shortages:
        return jsonify(error="Some items no longer have enough stock.", shortages=shortages), 409

    order_id = uuid.uuid4().hex
    now = int(time.time())
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
    order.update(fulfilment)
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
                            "name": item["name"][:250] or "Diamond Beauty product",
                            "metadata": {"product_id": item["id"]},
                        },
                        "unit_amount": item["price_pence"],
                    },
                    "quantity": item["qty"],
                }
                for item in items
            ],
            metadata={
                "order_id": order_id,
                "customer_name": name[:100],
                "fulfilment": fulfilment["fulfilment"],
                "collection_date": fulfilment["collection_date"],
                "ship_to": ", ".join(part for part in (
                    fulfilment["address1"], fulfilment["address2"],
                    fulfilment["city"], fulfilment["county"], fulfilment["postcode"],
                ) if part)[:500],
            },
            success_url=f"{base_url}/checkout-success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base_url}/checkout?cancelled=1",
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
    return jsonify(url=checkout.url, session_id=checkout.id, order_id=order_id)


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
        fulfilment=order.get("fulfilment", "delivery"),
        collection_date=order.get("collection_date", ""),
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


# --- admin: session ----------------------------------------------------------

def start_admin_session(username):
    session.clear()
    session["diamond_admin"] = True
    session["admin_user"] = username
    session["admin_fingerprint"] = admin_fingerprint()
    session["csrf_token"] = secrets.token_urlsafe(32)
    session["last_seen"] = time.time()
    session.permanent = True
    return session["csrf_token"]


@app.post("/api/admin/login")
def admin_login():
    if not ADMIN_PASS:
        return jsonify(error="ADMINPASS is not configured on the server."), 503
    if rate_limited(f"login:{client_ip()}", LOGIN_MAX_ATTEMPTS, LOGIN_WINDOW_SECONDS):
        return jsonify(error="Too many sign-in attempts. Please wait and try again."), 429

    payload = json_body()
    if payload is None:
        return jsonify(error="Request body is too large."), 413
    supplied_user = clean_text(payload.get("username") or payload.get("user") or payload.get("email"), 200)
    supplied_pass = str(payload.get("password") or "")[:512]

    user_ok = hmac.compare_digest(supplied_user.casefold(), ADMIN_USER.casefold()) if ADMIN_USER else True
    pass_ok = hmac.compare_digest(supplied_pass, ADMIN_PASS)
    if not (user_ok and pass_ok):
        time.sleep(0.3)
        log.warning("Failed admin sign-in from %s", client_ip())
        return jsonify(error="Invalid sign-in."), 401

    token = start_admin_session(supplied_user)
    return jsonify(ok=True, csrf_token=token, user=supplied_user)


@app.post("/api/admin/logout")
def admin_logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/admin/session")
def admin_session():
    if not ADMIN_PASS:
        return jsonify(authenticated=False, configured=False), 503
    if not is_admin():
        return jsonify(authenticated=False, configured=True)
    return jsonify(
        authenticated=True,
        configured=True,
        user=session.get("admin_user", ""),
        csrf_token=session.get("csrf_token", ""),
        stripe_ready=bool(stripe.api_key),
        stripe_publishable_key=STRIPE_PUBLISHABLE_KEY,
        stripe_webhook_ready=bool(STRIPE_WEBHOOK_SECRET),
    )


# --- admin: categories and catalogue ----------------------------------------

@app.get("/api/admin/categories")
@admin_required
def admin_categories():
    seed_catalogue()
    return jsonify(categories=list_categories())


@app.post("/api/admin/categories")
@admin_required
def admin_create_category():
    seed_catalogue()
    payload = json_body()
    if payload is None:
        return jsonify(error="Request body is too large."), 413
    name = clean_text(payload.get("name"), 80)
    if not name:
        return jsonify(error="A category name is required."), 400
    if db.zcard("category:index") >= MAX_CATEGORIES:
        return jsonify(error=f"The catalogue is limited to {MAX_CATEGORIES} categories."), 409

    category_id = slugify(payload.get("id") or name, "category")
    if category_exists(category_id):
        return jsonify(error="A category with this name already exists."), 409
    record = {"id": category_id, "name": name}
    position = int(db.zcard("category:index"))
    pipe = db.pipeline()
    pipe.set(category_key(category_id), _json(record))
    pipe.zadd("category:index", {category_id: position})
    pipe.execute()
    return jsonify(category={**record, "product_count": 0}), 201


@app.put("/api/admin/categories/<category_id>")
@admin_required
def admin_update_category(category_id):
    seed_catalogue()
    category_id = slugify(clean_text(category_id, 60), "")
    record = _load_json(db.get(category_key(category_id)))
    if not record:
        return jsonify(error="Category not found."), 404
    payload = json_body()
    if payload is None:
        return jsonify(error="Request body is too large."), 413
    name = clean_text(payload.get("name"), 80)
    if not name:
        return jsonify(error="A category name is required."), 400
    record["name"] = name
    db.set(category_key(category_id), _json(record))
    count = next((c["product_count"] for c in list_categories() if c["id"] == category_id), 0)
    return jsonify(category={**record, "product_count": count})


@app.delete("/api/admin/categories/<category_id>")
@admin_required
def admin_delete_category(category_id):
    seed_catalogue()
    category_id = slugify(clean_text(category_id, 60), "")
    if not category_exists(category_id):
        return jsonify(error="Category not found."), 404
    products_using_category = [
        product_id for product_id in all_product_ids()
        if (get_product(product_id) or {}).get("category", "other") == category_id
    ]
    if products_using_category:
        return jsonify(
            error=f"Move or delete the {len(products_using_category)} product(s) in this category first."
        ), 409
    pipe = db.pipeline()
    pipe.delete(category_key(category_id))
    pipe.zrem("category:index", category_id)
    pipe.execute()
    return jsonify(ok=True)

@app.get("/api/admin/products")
@admin_required
def admin_products():
    seed_catalogue()
    return jsonify(products=list_products(include_inactive=True))


@app.post("/api/admin/products")
@admin_required
def admin_create_product():
    seed_catalogue()
    payload = json_body()
    if payload is None:
        return jsonify(error="Request body is too large."), 413

    if db.zcard("product:index") >= MAX_PRODUCTS:
        return jsonify(error=f"The catalogue is limited to {MAX_PRODUCTS} products."), 409

    try:
        record = product_from_payload(payload)
        stock = parse_stock(payload.get("stock", 0))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    requested_id = slugify(payload.get("id") or record["name"])
    product_id = requested_id
    suffix = 2
    while db.exists(product_key(product_id)):
        product_id = f"{requested_id}-{suffix}"[:70]
        suffix += 1
        if suffix > 50:
            product_id = f"{requested_id}-{secrets.token_hex(3)}"[:70]
            break

    record["id"] = product_id
    position = int(db.zcard("product:index"))
    record["position"] = position

    pipe = db.pipeline()
    pipe.set(product_key(product_id), _json(record))
    pipe.zadd("product:index", {product_id: position})
    pipe.set(f"stock:{product_id}", stock)
    pipe.execute()
    return jsonify(product=public_product(record, stock)), 201


@app.put("/api/admin/products/<product_id>")
@admin_required
def admin_update_product(product_id):
    seed_catalogue()
    product_id = clean_text(product_id, 80)
    existing = get_product(product_id)
    if not existing:
        return jsonify(error="Product not found."), 404

    payload = json_body()
    if payload is None:
        return jsonify(error="Request body is too large."), 413
    try:
        record = product_from_payload(payload, existing)
        stock = parse_stock(payload.get("stock", stock_for(product_id)))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    record["id"] = product_id
    pipe = db.pipeline()
    pipe.set(product_key(product_id), _json(record))
    pipe.set(f"stock:{product_id}", stock)
    pipe.execute()
    return jsonify(product=public_product(record, stock))


@app.patch("/api/admin/products/<product_id>")
@admin_required
def admin_patch_product(product_id):
    """Partial update used for quick stock edits and publish/unpublish toggles."""
    product_id = clean_text(product_id, 80)
    existing = get_product(product_id)
    if not existing:
        return jsonify(error="Product not found."), 404

    payload = json_body()
    if payload is None:
        return jsonify(error="Request body is too large."), 413

    stock = None
    if "stock" in payload:
        try:
            stock = parse_stock(payload.get("stock"))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
    if "active" in payload:
        existing["active"] = bool(payload.get("active"))
    if "position" in payload:
        try:
            existing["position"] = max(0, int(payload.get("position")))
        except (TypeError, ValueError):
            return jsonify(error="Position must be a whole number."), 400

    existing["updated_at"] = int(time.time())
    pipe = db.pipeline()
    pipe.set(product_key(product_id), _json(existing))
    pipe.zadd("product:index", {product_id: existing.get("position", 0)})
    if stock is not None:
        pipe.set(f"stock:{product_id}", stock)
    pipe.execute()
    return jsonify(product=public_product(existing, stock))


@app.delete("/api/admin/products/<product_id>")
@admin_required
def admin_delete_product(product_id):
    product_id = clean_text(product_id, 80)
    if not get_product(product_id):
        return jsonify(error="Product not found."), 404
    pipe = db.pipeline()
    pipe.delete(product_key(product_id))
    pipe.delete(f"stock:{product_id}")
    pipe.zrem("product:index", product_id)
    pipe.execute()
    return jsonify(ok=True)


@app.post("/api/admin/products/reorder")
@admin_required
def admin_reorder_products():
    payload = json_body()
    if payload is None:
        return jsonify(error="Request body is too large."), 413
    order = payload.get("order")
    if not isinstance(order, list) or not order:
        return jsonify(error="An ordered list of product ids is required."), 400

    known = set(all_product_ids())
    pipe = db.pipeline()
    for position, raw_id in enumerate(order[:MAX_PRODUCTS]):
        pid = clean_text(raw_id, 80)
        if pid not in known:
            continue
        record = get_product(pid)
        if not record:
            continue
        record["position"] = position
        pipe.set(product_key(pid), _json(record))
        pipe.zadd("product:index", {pid: position})
    pipe.execute()
    return jsonify(products=list_products(include_inactive=True))


# --- admin: media ------------------------------------------------------------

@app.get("/api/admin/media")
@admin_required
def admin_list_media():
    ids = db.zrevrange("media:index", 0, 199)
    items = []
    for media_id in ids:
        meta = db.hgetall(f"media:{media_id}")
        if not meta:
            db.zrem("media:index", media_id)
            continue
        items.append({
            "id": media_id,
            "url": f"/media/{media_id}",
            "filename": meta.get("filename", ""),
            "content_type": meta.get("content_type", ""),
            "bytes": int(meta.get("bytes") or 0),
            "created_at": int(meta.get("created_at") or 0),
        })
    return jsonify(media=items)


@app.post("/api/admin/media")
@admin_required
def admin_upload_media():
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify(error="Choose an image file to upload."), 400

    content_type = (upload.mimetype or "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_IMAGE_TYPES:
        return jsonify(error="Images must be PNG, JPEG, WebP, GIF or AVIF."), 415

    blob = upload.read(MAX_UPLOAD_BYTES + 1)
    if not blob:
        return jsonify(error="The uploaded file is empty."), 400
    if len(blob) > MAX_UPLOAD_BYTES:
        return jsonify(error="Images must be 4 MB or smaller."), 413
    if not sniff_image(blob, content_type):
        return jsonify(error="That file does not look like a real image."), 415

    media_id = uuid.uuid4().hex
    now = int(time.time())
    filename = clean_text(Path(upload.filename).name, 120)
    pipe = db.pipeline()
    pipe.hset(f"media:{media_id}", mapping={
        "data": base64.b64encode(blob).decode("ascii"),
        "content_type": content_type,
        "filename": filename,
        "bytes": len(blob),
        "created_at": now,
    })
    pipe.zadd("media:index", {media_id: now})
    pipe.execute()
    return jsonify(id=media_id, url=f"/media/{media_id}", bytes=len(blob), filename=filename), 201


def sniff_image(blob, content_type):
    """Cheap magic-number check so a mislabelled file cannot be stored as an image."""
    signatures = {
        "image/png": [b"\x89PNG\r\n\x1a\n"],
        "image/jpeg": [b"\xff\xd8\xff"],
        "image/gif": [b"GIF87a", b"GIF89a"],
    }
    if content_type in signatures:
        return any(blob.startswith(sig) for sig in signatures[content_type])
    if content_type == "image/webp":
        return blob[:4] == b"RIFF" and blob[8:12] == b"WEBP"
    if content_type == "image/avif":
        return blob[4:8] == b"ftyp"
    return False


@app.delete("/api/admin/media/<media_id>")
@admin_required
def admin_delete_media(media_id):
    if not re.fullmatch(r"[a-f0-9]{32}", media_id or ""):
        return jsonify(error="Not found."), 404
    pipe = db.pipeline()
    pipe.delete(f"media:{media_id}")
    pipe.zrem("media:index", media_id)
    pipe.execute()
    return jsonify(ok=True)


# --- admin: inventory, orders, messages --------------------------------------

@app.get("/api/admin/inventory")
@admin_required
def admin_inventory():
    seed_catalogue()
    return jsonify(products=list_products(include_inactive=True))


@app.put("/api/admin/inventory/<product_id>")
@admin_required
def admin_set_inventory(product_id):
    product_id = clean_text(product_id, 80)
    record = get_product(product_id)
    if not record:
        return jsonify(error="Unknown product."), 404
    payload = json_body()
    if payload is None:
        return jsonify(error="Request body is too large."), 413
    try:
        stock = parse_stock(payload.get("stock"))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    db.set(f"stock:{product_id}", stock)
    return jsonify(product=public_product(record, stock))


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
    payload = json_body()
    if payload is None:
        return jsonify(error="Request body is too large."), 413
    status = clean_text(payload.get("status"), 60)
    allowed = {
        "message": {"new", "handled"},
        "order": {
            "fulfilled", "cancelled", "review",
            "packing", "ready_for_collection", "collected", "dispatched",
        },
    }
    if status not in allowed[singular]:
        return jsonify(error="Invalid status."), 400

    current = get_record(singular, record_id)
    if not current:
        return jsonify(error="Record not found."), 404

    if singular == "order":
        # Every forward step implies the money has actually landed.
        progress = {"packing", "ready_for_collection", "collected", "dispatched", "fulfilled"}
        if status in progress and current.get("payment_status") != "paid":
            return jsonify(error="Only Stripe-paid orders can be moved through fulfilment."), 409
        if status == "cancelled" and current.get("payment_status") == "paid":
            return jsonify(error="Paid orders must be refunded in Stripe before cancellation."), 409
        method = current.get("fulfilment") or "delivery"
        if status in {"ready_for_collection", "collected"} and method != "collection":
            return jsonify(error="That status only applies to collection orders."), 409
        if status == "dispatched" and method != "delivery":
            return jsonify(error="That status only applies to delivery orders."), 409

    changes = {"status": status, "updated_at": int(time.time())}
    if singular == "order" and status == "ready_for_collection":
        changes["ready_at"] = int(time.time())
    record = update_record(singular, record_id, changes)
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


try:
    seed_catalogue()
except redis.RedisError:
    log.warning("Redis is not reachable at start-up; the catalogue will seed on first request.")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="127.0.0.1" if env_flag("FLASK_DEBUG") else "0.0.0.0", port=port, debug=env_flag("FLASK_DEBUG"))
