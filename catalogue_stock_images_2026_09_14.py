"""Replace the Sep 2026 stocktake placeholder art with real manufacturer images.

Only products added by the stocktake that still point at one of the generic
*-stock.svg placeholders are touched. Existing catalogue photography is never
replaced. BANDI images are resolved from BANDI's own Shopify catalogue; the
three NOW products use the exact size-specific official NOW product images.
"""

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

import redis

IMAGE_MIGRATION_KEY = "catalogue:migration:stock-images:2026-09-14:v2"
BANDI_BASE = "https://bandi-cosmetics.co.uk"
PLACEHOLDER_IMAGES = {
    "/assets/images/products/now-stock.svg",
    "/assets/images/products/bandi-medical-stock.svg",
    "/assets/images/products/bandi-professional-stock.svg",
    "/assets/images/products/bandi-tricho-stock.svg",
}

NOW_IMAGES = {
    "now-b50": {
        "image": "https://www.nowfoods.com/sites/default/files/styles/cloudzoom_image/public/2022-08/0426_mainimage.png?itok=ktG3TByu",
        "source": "https://www.nowfoods.com/products/supplements/vitamin-b-50-tablets",
    },
    "now-creatine-caps": {
        "image": "https://www.nowfoods.com/sites/default/files/styles/cloudzoom_image/public/2025-10/2035_v11.png?itok=rel6ojjV",
        "source": "https://www.nowfoods.com/products/sports-nutrition/creatine-monohydrate-750-mg-veg-capsules",
    },
    "now-magnesium-glycinate": {
        "image": "https://www.nowfoods.com/sites/default/files/styles/cloudzoom_image/public/2026-03/1289_v5.png?itok=q6olKSnz",
        "source": "https://www.nowfoods.com/products/supplements/magnesium-glycinate-tablets",
    },
}


def env(*names, default=""):
    for name in names:
        value = os.getenv(name)
        if value:
            return value.strip()
    return default


REDIS_URL = env(
    "REDIS_URL", "REDIT_URL", "redit_url", "REDIS_PUBLIC_URL",
    default="redis://localhost:6379/0",
)


def fetch_json(url, timeout=12):
    request = Request(
        url,
        headers={
            "User-Agent": "DiamondBeautyCatalogue/1.0 (+product image sync)",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def normalise(value):
    return " ".join(re.findall(r"[a-z0-9]+", (value or "").lower()))


def score_title(target, candidate):
    target_norm = normalise(target)
    candidate_norm = normalise(candidate)
    if not target_norm or not candidate_norm:
        return 0.0
    if target_norm == candidate_norm:
        return 10.0

    ratio = SequenceMatcher(None, target_norm, candidate_norm).ratio()
    target_tokens = set(target_norm.split())
    candidate_tokens = set(candidate_norm.split())
    overlap = len(target_tokens & candidate_tokens) / max(1, min(len(target_tokens), len(candidate_tokens)))
    containment = 1.0 if target_norm in candidate_norm or candidate_norm in target_norm else 0.0
    return ratio * 0.62 + overlap * 0.33 + containment * 0.55


def absolute_image(value):
    if not value:
        return ""
    if isinstance(value, dict):
        value = value.get("src") or value.get("url") or ""
    value = str(value)
    if value.startswith("//"):
        return "https:" + value
    return urljoin(BANDI_BASE + "/", value)


def exact_bandi_product_from_source(source):
    if "/products/" not in source:
        return None
    handle = source.split("/products/", 1)[1].split("?", 1)[0].strip("/")
    if not handle:
        return None
    product_url = f"{BANDI_BASE}/products/{handle}"
    data = fetch_json(product_url + ".js")
    image = absolute_image(data.get("featured_image"))
    if not image:
        images = data.get("images") or []
        image = absolute_image(images[0]) if images else ""
    return (image, product_url) if image else None


def collection_handle_from_source(source):
    if "/collections/" not in source:
        return ""
    rest = source.split("/collections/", 1)[1]
    return rest.split("/", 1)[0].split("?", 1)[0].strip("/")


def bandi_from_collection(name, collection_handle):
    if not collection_handle:
        return None
    url = f"{BANDI_BASE}/collections/{collection_handle}/products.json?limit=250"
    data = fetch_json(url)
    products = data.get("products") or []
    if not products:
        return None
    best = max(products, key=lambda product: score_title(name, product.get("title", "")))
    images = best.get("images") or []
    image = absolute_image(images[0]) if images else ""
    handle = best.get("handle") or ""
    if not image or not handle:
        return None
    return image, f"{BANDI_BASE}/products/{handle}"


def bandi_predictive_search(name):
    params = urlencode({
        "q": name,
        "resources[type]": "product",
        "resources[limit]": "10",
        "resources[options][unavailable_products]": "last",
    })
    data = fetch_json(f"{BANDI_BASE}/search/suggest.json?{params}")
    products = (((data.get("resources") or {}).get("results") or {}).get("products") or [])
    if not products:
        return None
    best = max(products, key=lambda product: score_title(name, product.get("title", "")))
    featured = best.get("featured_image") or best.get("image")
    image = absolute_image(featured)
    product_url = urljoin(BANDI_BASE + "/", best.get("url") or "")
    return (image, product_url) if image and product_url else None


def resolve_bandi(record):
    name = record.get("name", "")
    source = record.get("source", "")

    try:
        resolved = exact_bandi_product_from_source(source)
        if resolved:
            return resolved
    except Exception:
        pass

    collection = collection_handle_from_source(source)
    if collection:
        try:
            resolved = bandi_from_collection(name, collection)
            if resolved:
                return resolved
        except Exception:
            pass

    return bandi_predictive_search(name)


def resolve_one(product_id, record):
    if product_id in NOW_IMAGES:
        official = NOW_IMAGES[product_id]
        return product_id, official["image"], official["source"]

    brand = (record.get("brand") or "").lower()
    if "bandi" in brand:
        image, source = resolve_bandi(record)
        return product_id, image, source

    return product_id, "", ""


def main():
    db = redis.Redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=8,
        socket_timeout=8,
        health_check_interval=30,
        retry_on_timeout=True,
    )
    db.ping()

    if db.exists(IMAGE_MIGRATION_KEY):
        print("Official stock images already synced")
        return

    candidates = []
    for product_id in db.zrange("product:index", 0, -1):
        raw = db.get(f"product:{product_id}")
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if record.get("image") in PLACEHOLDER_IMAGES:
            candidates.append((product_id, record))

    if not candidates:
        db.set(IMAGE_MIGRATION_KEY, int(time.time()))
        print("Official stock image sync: no placeholders remain")
        return

    resolved = []
    unresolved = []
    workers = min(8, max(1, len(candidates)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(resolve_one, product_id, record): (product_id, record)
            for product_id, record in candidates
        }
        for future in as_completed(future_map):
            product_id, record = future_map[future]
            try:
                _, image, source = future.result()
            except Exception as exc:
                unresolved.append((product_id, str(exc)))
                continue
            if not image.startswith("https://"):
                unresolved.append((product_id, "no official https image resolved"))
                continue
            resolved.append((product_id, record, image, source))

    now = int(time.time())
    pipe = db.pipeline()
    for product_id, record, image, source in resolved:
        record["image"] = image
        if source:
            record["source"] = source
        record["updated_at"] = now
        pipe.set(f"product:{product_id}", json.dumps(record, separators=(",", ":"), ensure_ascii=False))
    pipe.execute()

    if unresolved:
        ids = ", ".join(product_id for product_id, _ in unresolved)
        print(f"Official stock image sync: {len(resolved)} updated; retry needed for {len(unresolved)}: {ids}")
        return

    db.set(IMAGE_MIGRATION_KEY, now)
    print(f"Official stock image sync complete: {len(resolved)} products updated")


if __name__ == "__main__":
    main()
