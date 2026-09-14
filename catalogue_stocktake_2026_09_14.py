"""One-shot catalogue stocktake from the 14 Sep 2026 clinic shelf audit.

Adds missing products without overwriting any existing catalogue records/images,
then sets every current product's stock to 12 exactly once.  Safe to run on every
boot because the Redis migration key makes it idempotent.
"""

import json
import os
import time

import redis

STOCK_LEVEL = 12
MIGRATION_KEY = "catalogue:migration:stocktake:2026-09-14:v1"


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

# Existing catalogue records are deliberately left untouched so their current
# product photography, copy and prices stay exactly as they are.  These records
# are only used when an item is missing from Redis.
PHOTO_PRODUCTS = [
    # NOW Foods
    {"id":"now-b50","brand":"NOW Foods","name":"B-50, 100 Tablets","price_pence":1700,"category":"wellness","image":"/assets/images/products/now-stock.svg","description":"B-complex vitamin formula in tablet form.","source":"https://www.nowfoods.com/products/supplements/b-50-tablets"},
    {"id":"now-creatine-caps","brand":"NOW Sports","name":"Creatine Monohydrate 750 mg, 120 Veg Capsules","price_pence":1650,"category":"wellness","image":"/assets/images/products/now-stock.svg","description":"Creatine monohydrate in convenient vegetable capsules.","source":"https://www.nowfoods.com/products/sports-nutrition/creatine-monohydrate-750-mg-veg-capsules"},
    {"id":"now-magnesium-glycinate","brand":"NOW Foods","name":"Magnesium Glycinate, 180 Tablets","price_pence":1500,"category":"wellness","image":"/assets/images/products/now-stock.svg","description":"Magnesium glycinate tablets for everyday supplementation.","source":"https://www.nowfoods.com/products/supplements/magnesium-glycinate-tablets"},

    # BANDI Medical Expert - Anti Dark Spot
    {"id":"bandi-dark-toning-spf50","brand":"BANDI Medical Expert","name":"Toning cream SPF50","price_pence":2250,"category":"bandi","image":"/assets/images/products/bandi-medical-stock.svg","description":"Daily toning cream with high SPF protection for discolouration-prone skin.","source":"https://bandi-cosmetics.co.uk/collections/medical-expert-anti-dark-spot"},
    {"id":"bandi-dark-emulsion","brand":"BANDI Medical Expert","name":"Deeply brightening emulsion","price_pence":2100,"category":"bandi","image":"/assets/images/products/bandi-medical-stock.svg","description":"Brightening emulsion for uneven tone and discolouration.","source":"https://bandi-cosmetics.co.uk/collections/medical-expert-anti-dark-spot"},
    {"id":"bandi-dark-peel","brand":"BANDI Medical Expert","name":"Acid-enzymatic peel deeply brightening","price_pence":1900,"category":"bandi","image":"/assets/images/products/bandi-medical-stock.svg","description":"Acid-enzyme peel for a brighter, more even-looking complexion.","source":"https://bandi-cosmetics.co.uk/collections/medical-expert-anti-dark-spot"},
    {"id":"bandi-dark-ampoule","brand":"BANDI Medical Expert","name":"Intensive Brightening Ampoule for Discolouration","price_pence":1900,"category":"bandi","image":"/assets/images/products/bandi-medical-stock.svg","description":"Concentrated brightening ampoule for areas of discolouration.","source":"https://bandi-cosmetics.co.uk/collections/medical-expert-anti-dark-spot"},

    # BANDI Medical Expert - Anti Glycation
    {"id":"bandi-glycation-firming-spf50","brand":"BANDI Medical Expert","name":"Firming face cream antioxidant SPF 50","price_pence":2500,"category":"bandi","image":"/assets/images/products/bandi-medical-stock.svg","description":"Firming antioxidant day cream with SPF 50.","source":"https://bandi-cosmetics.co.uk/collections/medical-expert-anti-glycation/products/firming-face-cream-antioxidant"},
    {"id":"bandi-glycation-mask","brand":"BANDI Medical Expert","name":"Glow & repair mask 2 in 1 instant effect","price_pence":2500,"category":"bandi","image":"/assets/images/products/bandi-medical-stock.svg","description":"Two-in-one glow and repair mask for tired-looking skin.","source":"https://bandi-cosmetics.co.uk/collections/medical-expert-anti-glycation"},
    {"id":"bandi-glycation-ampoule","brand":"BANDI Medical Expert","name":"Concentrated ampoule reducing signs of fatigue","price_pence":1300,"category":"bandi","image":"/assets/images/products/bandi-medical-stock.svg","description":"Concentrated ampoule designed to reduce visible signs of fatigue.","source":"https://bandi-cosmetics.co.uk/collections/medical-expert-anti-glycation"},

    # BANDI Medical Expert - Anti Irritate / Anti Dry / Anti Acne / Anti Rouge
    {"id":"bandi-irritate-sos-treatment","brand":"BANDI Medical Expert","name":"SOS Intensive soothing treatment","price_pence":2100,"category":"bandi","image":"/assets/images/products/bandi-medical-stock.svg","description":"Intensive soothing treatment for sensitive and irritated skin.","source":"https://bandi-cosmetics.co.uk/collections/medical-expert-anti-irritate"},
    {"id":"bandi-irritate-tonic","brand":"BANDI Medical Expert","name":"SOS Tonic mist microbial","price_pence":900,"category":"bandi","image":"/assets/images/products/bandi-medical-stock.svg","description":"Soothing tonic mist with microbiome-supporting care.","source":"https://bandi-cosmetics.co.uk/collections/medical-expert-anti-irritate"},
    {"id":"bandi-dry-acid-peel","brand":"BANDI Medical Expert","name":"Anti Dry acid peel","price_pence":1320,"category":"bandi","image":"/assets/images/products/bandi-medical-stock.svg","description":"Moisturising acid peel for dry and very dry skin.","source":"https://bandi-cosmetics.co.uk/collections/medical-expert-anti-dry"},
    {"id":"bandi-dry-treatment-cream","brand":"BANDI Medical Expert","name":"Nourishing and moisturising treatment cream","price_pence":2000,"category":"bandi","image":"/assets/images/products/bandi-medical-stock.svg","description":"Nourishing treatment cream for dry skin.","source":"https://bandi-cosmetics.co.uk/collections/medical-expert-anti-dry"},
    {"id":"bandi-dry-eye-mask","brand":"BANDI Medical Expert","name":"Nourishing and moisturising under-eye cream mask","price_pence":1450,"category":"bandi","image":"/assets/images/products/bandi-medical-stock.svg","description":"Rich moisturising cream-mask for the under-eye area.","source":"https://bandi-cosmetics.co.uk/collections/medical-expert-anti-dry"},
    {"id":"bandi-acne-ampoule","brand":"BANDI Medical Expert","name":"Concentrated anti-acne ampoule","price_pence":1300,"category":"bandi","image":"/assets/images/products/bandi-medical-stock.svg","description":"Concentrated anti-acne ampoule with salicylic acid and tea tree oil.","source":"https://bandi-cosmetics.co.uk/products/concentrated-anti-acne-ampoule"},
    {"id":"bandi-acne-treatment","brand":"BANDI Medical Expert","name":"Anti-acne treatment cream","price_pence":1100,"category":"bandi","image":"/assets/images/products/bandi-medical-stock.svg","description":"Night treatment cream for blemish-prone skin.","source":"https://bandi-cosmetics.co.uk/collections/medical-expert-anti-acne"},
    {"id":"bandi-rouge-treatment","brand":"BANDI Medical Expert","name":"Anti-rouge capillary treatment cream","price_pence":1750,"category":"bandi","image":"/assets/images/products/bandi-medical-stock.svg","description":"Treatment cream for redness-prone and capillary skin.","source":"https://bandi-cosmetics.co.uk/collections/medical-expert-anti-rouge"},
    {"id":"bandi-rouge-cc","brand":"BANDI Medical Expert","name":"Anti-rouge CC capillary","price_pence":1650,"category":"bandi","image":"/assets/images/products/bandi-medical-stock.svg","description":"CC corrector for redness-prone capillary skin.","source":"https://bandi-cosmetics.co.uk/collections/medical-expert-anti-rouge"},
    {"id":"bandi-rouge-peel","brand":"BANDI Medical Expert","name":"Anti-rouge acid peel","price_pence":1320,"category":"bandi","image":"/assets/images/products/bandi-medical-stock.svg","description":"Acid peel formulated for capillary and redness-prone skin.","source":"https://bandi-cosmetics.co.uk/collections/medical-expert-anti-rouge"},
    {"id":"bandi-rouge-ampoule","brand":"BANDI Medical Expert","name":"Concentrated capillary ampoule","price_pence":1650,"category":"bandi","image":"/assets/images/products/bandi-medical-stock.svg","description":"Concentrated ampoule for capillary skin.","source":"https://bandi-cosmetics.co.uk/collections/medical-expert-anti-rouge"},

    # BANDI Professional / home-care lines
    {"id":"bandi-pro-lactobionic-urea","brand":"BANDI Professional","name":"Cream with lactobionic acid and urea moisturising","price_pence":2150,"category":"bandi","image":"/assets/images/products/bandi-professional-stock.svg","description":"Moisturising PRO Care cream with lactobionic acid and urea.","source":"https://bandi-cosmetics.co.uk/collections/pro-care"},
    {"id":"bandi-pro-tranexamic-azeloglycine","brand":"BANDI Professional","name":"Cream with tranexamic acid and azeloglycine for capillaries","price_pence":2150,"category":"bandi","image":"/assets/images/products/bandi-professional-stock.svg","description":"PRO Care cream with tranexamic acid and azeloglycine.","source":"https://bandi-cosmetics.co.uk/collections/pro-care"},
    {"id":"bandi-pro-mandelic-pha","brand":"BANDI Professional","name":"Cream with mandelic acid and polyhydroxy acids gently exfoliating","price_pence":2150,"category":"bandi","image":"/assets/images/products/bandi-professional-stock.svg","description":"Gentle exfoliating PRO Care cream with mandelic and polyhydroxy acids.","source":"https://bandi-cosmetics.co.uk/collections/pro-care"},
    {"id":"bandi-more-moist-toner","brand":"BANDI Professional","name":"Hydroactive Milky Toner with Collagen Bank","price_pence":900,"category":"bandi","image":"/assets/images/products/bandi-professional-stock.svg","description":"Hydroactive milky toner from the More Than Moist line.","source":"https://bandi-cosmetics.co.uk/products/hydroactive-milky-toner-with-collagen-bank"},
    {"id":"bandi-more-moist-mask","brand":"BANDI Professional","name":"Hydroactive Mask - Cream with Collagen Bank","price_pence":1900,"category":"bandi","image":"/assets/images/products/bandi-professional-stock.svg","description":"Hydroactive cream-mask from the More Than Moist line.","source":"https://bandi-cosmetics.co.uk/products/hydroactive-mask-cream-with-collagen-bank"},
    {"id":"bandi-pure-witch-hazel-gel","brand":"BANDI Professional","name":"Cleansing gel with witch hazel","price_pence":1200,"category":"bandi","image":"/assets/images/products/bandi-professional-stock.svg","description":"Pure Care cleansing gel with witch hazel.","source":"https://bandi-cosmetics.co.uk/collections/pure-care"},
    {"id":"bandi-pure-gentle-foam","brand":"BANDI Professional","name":"Gentle cleansing foam","price_pence":1200,"category":"bandi","image":"/assets/images/products/bandi-professional-stock.svg","description":"Gentle Pure Care cleansing foam.","source":"https://bandi-cosmetics.co.uk/collections/pure-care"},
    {"id":"bandi-pure-micellar-sensitive","brand":"BANDI Professional","name":"Micellar lotion for sensitive skin","price_pence":1000,"category":"bandi","image":"/assets/images/products/bandi-professional-stock.svg","description":"Micellar cleansing lotion for sensitive skin.","source":"https://bandi-cosmetics.co.uk/collections/pure-care"},
    {"id":"bandi-body-butter","brand":"BANDI Professional","name":"Body Butter","price_pence":1500,"category":"bandi","image":"/assets/images/products/bandi-professional-stock.svg","description":"Rich body butter from BANDI Professional Body Care.","source":"https://bandi-cosmetics.co.uk/collections/body-care-professional"},
    {"id":"bandi-hydro-eye-cream-gel","brand":"BANDI Professional","name":"Moisturising eye cream-gel","price_pence":1300,"category":"bandi","image":"/assets/images/products/bandi-professional-stock.svg","description":"Hydro Care moisturising eye cream-gel.","source":"https://bandi-cosmetics.co.uk/collections/hydro-care"},
    {"id":"bandi-veno-anti-redness","brand":"BANDI Professional","name":"Anti-redness cream-gel","price_pence":1500,"category":"bandi","image":"/assets/images/products/bandi-professional-stock.svg","description":"Veno Care cream-gel for redness-prone skin.","source":"https://bandi-cosmetics.co.uk/collections/veno-care"},
    {"id":"bandi-more-pause-eye","brand":"BANDI Professional","name":"Lifting Eye Cream","price_pence":2500,"category":"bandi","image":"/assets/images/products/bandi-professional-stock.svg","description":"Lifting eye cream from the More Than Pause line.","source":"https://bandi-cosmetics.co.uk/collections/more-than-pause"},
    {"id":"bandi-antigravity-eye","brand":"BANDI Medical Expert","name":"Lifting Eye Cream Firming and Smoothing","price_pence":2500,"category":"bandi","image":"/assets/images/products/bandi-medical-stock.svg","description":"Firming and smoothing lifting eye cream from Anti Gravity.","source":"https://bandi-cosmetics.co.uk/collections/medical-expert-anti-gravity"},
    {"id":"bandi-boost-caffeine-eye","brand":"BANDI Professional","name":"Caffeine eye cream for dark circles and puffiness","price_pence":950,"category":"bandi","image":"/assets/images/products/bandi-professional-stock.svg","description":"Caffeine eye cream for dark circles and puffiness.","source":"https://bandi-cosmetics.co.uk/collections/boost-care"},
    {"id":"bandi-boost-express-c-mask","brand":"BANDI Professional","name":"Express mask with new generation vitamin C","price_pence":550,"category":"bandi","image":"/assets/images/products/bandi-professional-stock.svg","description":"Express vitamin C mask from Boost Care.","source":"https://bandi-cosmetics.co.uk/collections/boost-care"},
    {"id":"bandi-men-spf30","brand":"BANDI Professional","name":"Face cream SPF30 UVB and UVA protection","price_pence":2000,"category":"bandi","image":"/assets/images/products/bandi-professional-stock.svg","description":"4MEN Care daily face cream with SPF30 UVA/UVB protection.","source":"https://bandi-cosmetics.co.uk/collections/4men-care"},

    # BANDI Tricho-Esthetic
    {"id":"bandi-tricho-peeling","brand":"BANDI Professional","name":"Tricho-peeling scalp cleansing","price_pence":1200,"category":"haircare","image":"/assets/images/products/bandi-tricho-stock.svg","description":"Specialist scalp-cleansing tricho-peeling.","source":"https://bandi-cosmetics.co.uk/collections/tricho-esthetic"},
    {"id":"bandi-tricho-shampoo-volume","brand":"BANDI Professional","name":"Tricho-shampoo volumising","price_pence":1350,"category":"haircare","image":"/assets/images/products/bandi-tricho-stock.svg","description":"Volumising Tricho-Esthetic shampoo.","source":"https://bandi-cosmetics.co.uk/collections/tricho-esthetic"},
    {"id":"bandi-tricho-conditioner-volume","brand":"BANDI Professional","name":"Tricho-conditioner volumising","price_pence":1650,"category":"haircare","image":"/assets/images/products/bandi-tricho-stock.svg","description":"Volumising Tricho-Esthetic conditioner.","source":"https://bandi-cosmetics.co.uk/collections/tricho-esthetic"},
    {"id":"bandi-tricho-oily-extract","brand":"BANDI Professional","name":"Tricho-extract oily scalp and hair","price_pence":1450,"category":"haircare","image":"/assets/images/products/bandi-tricho-stock.svg","description":"Tricho extract for oily scalp and hair.","source":"https://bandi-cosmetics.co.uk/collections/tricho-esthetic"},
    {"id":"bandi-tricho-dandruff-shampoo","brand":"BANDI Professional","name":"Tricho-shampoo micellar anti-dandruff","price_pence":1200,"category":"haircare","image":"/assets/images/products/bandi-tricho-stock.svg","description":"Micellar anti-dandruff Tricho shampoo.","source":"https://bandi-cosmetics.co.uk/collections/tricho-esthetic"},
    {"id":"bandi-tricho-moisture-extract","brand":"BANDI Professional","name":"Tricho-extract regenerating moisturising","price_pence":1450,"category":"haircare","image":"/assets/images/products/bandi-tricho-stock.svg","description":"Regenerating and moisturising Tricho extract for dry scalp and hair.","source":"https://bandi-cosmetics.co.uk/collections/tricho-esthetic"},
]


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

    if db.exists(MIGRATION_KEY):
        print("September catalogue stocktake already applied")
        return

    with db.lock(f"lock:{MIGRATION_KEY}", timeout=60, blocking_timeout=15):
        if db.exists(MIGRATION_KEY):
            print("September catalogue stocktake already applied")
            return

        now = int(time.time())
        existing_ids = list(db.zrange("product:index", 0, -1))
        next_position = len(existing_ids)
        added = 0
        pipe = db.pipeline()

        # Ensure categories needed by the photographed stock exist.
        categories = (("bandi", "Bandi"), ("wellness", "Wellness"), ("haircare", "Haircare"))
        for position, (category_id, name) in enumerate(categories):
            pipe.setnx(f"category:{category_id}", json.dumps({"id": category_id, "name": name}, separators=(",", ":")))
            pipe.zadd("category:index", {category_id: position}, nx=True)

        for product in PHOTO_PRODUCTS:
            key = f"product:{product['id']}"
            if db.exists(key):
                continue
            record = dict(product)
            record.update({
                "tag": "",
                "active": True,
                "created_at": now,
                "updated_at": now,
                "position": next_position,
            })
            pipe.set(key, json.dumps(record, separators=(",", ":"), ensure_ascii=False))
            pipe.zadd("product:index", {product["id"]: next_position})
            next_position += 1
            added += 1

        pipe.execute()

        # Stocktake instruction: every product currently in the shop is 12.
        all_ids = list(db.zrange("product:index", 0, -1))
        stock_pipe = db.pipeline()
        for product_id in all_ids:
            stock_pipe.set(f"stock:{product_id}", STOCK_LEVEL)
        stock_pipe.set(MIGRATION_KEY, now)
        stock_pipe.execute()

        print(f"September catalogue stocktake applied: {added} products added; {len(all_ids)} products set to stock {STOCK_LEVEL}")


if __name__ == "__main__":
    main()
