"""Cache stocktake product photos in Redis so cards never depend on hotlinking.

The previous stock-image migration resolves genuine manufacturer images, but a
few remote hosts refuse direct browser embedding and render as blank white
cards. This pass downloads those same image bytes server-side, validates that
an actual raster image was returned, stores it using the shop's existing
/media/<id> mechanism, and points the product at that local URL.

Only products from the 14 Sep stocktake are considered. Existing catalogue
photography is not touched.
"""

import base64
import hashlib
import json
import os
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import redis

from catalogue_stocktake_2026_09_14 import PHOTO_PRODUCTS

MIGRATION_KEY = "catalogue:migration:stock-image-cache:2026-09-14:v1"
MAX_IMAGE_BYTES = 4 * 1024 * 1024
TARGET_IDS = {product["id"] for product in PHOTO_PRODUCTS}


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


def sniff_image(blob):
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if blob.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if blob[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif", ".gif"
    if len(blob) >= 12 and blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if len(blob) >= 12 and blob[4:8] == b"ftyp" and blob[8:12] in {b"avif", b"avis"}:
        return "image/avif", ".avif"
    return None, None


def download_image(url, source=""):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/152 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }
    if source.startswith("https://"):
        headers["Referer"] = source
    request = Request(url, headers=headers)
    with urlopen(request, timeout=20) as response:
        blob = response.read(MAX_IMAGE_BYTES + 1)
    if len(blob) > MAX_IMAGE_BYTES:
        raise ValueError("image exceeds 4 MB")
    if len(blob) < 1500:
        raise ValueError("image payload is suspiciously small")
    content_type, extension = sniff_image(blob)
    if not content_type:
        raise ValueError("remote URL did not return a supported image")
    return blob, content_type, extension


def cache_image(db, product_id, image_url, source, now):
    blob, content_type, extension = download_image(image_url, source)
    digest = hashlib.sha256(
        f"diamond-stock-cache-v1:{product_id}:{image_url}".encode("utf-8")
    ).hexdigest()[:32]
    parsed = urlparse(image_url)
    stem = os.path.basename(parsed.path).rsplit(".", 1)[0] or product_id
    filename = f"{stem[:80]}{extension}"

    pipe = db.pipeline()
    pipe.hset(
        f"media:{digest}",
        mapping={
            "data": base64.b64encode(blob).decode("ascii"),
            "content_type": content_type,
            "filename": filename,
            "bytes": len(blob),
            "created_at": now,
        },
    )
    pipe.zadd("media:index", {digest: now})
    pipe.execute()
    return f"/media/{digest}"


def main():
    db = redis.Redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=8,
        socket_timeout=12,
        health_check_interval=30,
        retry_on_timeout=True,
    )
    db.ping()

    if db.exists(MIGRATION_KEY):
        print("Stock images already cached locally")
        return

    now = int(time.time())
    cached = 0
    unresolved = []

    for product_id in TARGET_IDS:
        raw = db.get(f"product:{product_id}")
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            unresolved.append(product_id)
            continue

        image_url = str(record.get("image") or "")
        if image_url.startswith("/media/"):
            continue
        if not image_url.startswith("https://"):
            # The manufacturer-image resolver runs immediately before this pass.
            # If it still has a placeholder, leave it for the next deploy/retry.
            unresolved.append(product_id)
            continue

        try:
            local_url = cache_image(
                db, product_id, image_url, str(record.get("source") or ""), now
            )
        except Exception as exc:
            print(f"Could not cache {product_id}: {exc}")
            unresolved.append(product_id)
            continue

        record["image"] = local_url
        record["updated_at"] = now
        db.set(
            f"product:{product_id}",
            json.dumps(record, separators=(",", ":"), ensure_ascii=False),
        )
        cached += 1

    if unresolved:
        print(
            f"Stock image cache: {cached} fixed; {len(unresolved)} will retry: "
            + ", ".join(sorted(unresolved))
        )
        return

    db.set(MIGRATION_KEY, now)
    print(f"Stock image cache complete: {cached} products localised")


if __name__ == "__main__":
    main()
