#!/usr/bin/env python3
"""
Zelda 40th Anniversary stock tracker.

Runs on GitHub Actions every ~10 minutes. Checks each UK retailer's product
page and sends a push notification to your phone (via ntfy) the moment
something comes into stock. It remembers what it has already told you in
state.json, so you get one alert per restock rather than one every run.
"""

import json
import os
import random
import re
import time
from datetime import datetime, timezone
from html import unescape

from curl_cffi import requests

# =====================================================================
#  EDIT THIS LIST TO ADD OR REMOVE RETAILERS
#  Copy a line, paste the product page link, change the retailer name.
#  max_price: if a page is in stock ABOVE this (e.g. a marketplace
#  seller on Amazon), you get a quiet "over your cap" note instead of
#  an urgent alert.
# =====================================================================
PRODUCTS = [
    # --- Working from GitHub ------------------------------------------
    {"item": "Console", "retailer": "EE", "max_price": 450,
     "url": "https://ee.co.uk/products/nintendo-switch-2-zelda-ocarina-of-time-special-edition"},
    {"item": "Pro Controller", "retailer": "Amazon", "max_price": 85,
     "url": "https://www.amazon.co.uk/dp/B0HJ8344SW"},

    # --- Switched off: these sites block scripts, so they're watched by
    #     Distill on the PC instead (it uses a real browser).
    # Argos console:        https://www.argos.co.uk/product/9625053
    # Argos controller:     https://www.argos.co.uk/product/9754625
    # Currys console:       https://www.currys.co.uk/products/nintendo-switch-2-the-legend-of-zelda-40th-anniversary-edition-console-10311285.html
    # Currys controller:    https://www.currys.co.uk/products/nintendo-switch-2-pro-controller-the-legend-of-zelda-40th-anniversary-edition-10311288.html
    # Smyths console:       https://www.smythstoys.com/uk/en-gb/gaming-and-tech/nintendo-switch-2/nintendo-switch-2-consoles/nintendo-switch-2-the-legend-of-zelda-40th-anniversary-edition-console/p/265573
    # Smyths controller:    https://www.smythstoys.com/uk/en-gb/gaming-and-tech/nintendo-switch-2/nintendo-switch-2-accessories/nintendo-switch-2-pro-controller-the-legend-of-zelda-40th-anniversary-edition/p/265620
    # Scan controller:      https://www.scan.co.uk/products/nintendo-switch-2-pro-controller-legend-of-zelda-40th-anniversary-green-mappable-buttons-usb-c

    # --- Parked until we find their background stock check -----------
    # Nintendo UK console:  https://store.nintendo.com/en-gb/nintendo-switch-2-the-legend-of-zelda-40th-anniversary-edition-P00211
    # Nintendo UK controller + stand:
    #   https://store.nintendo.com/en-gb/nintendo-switch-2-pro-controller-the-legend-of-zelda-40th-anniversary-edition-display-stand-000000000010019437
    # Very console:         https://www.very.co.uk/nintendo-the-legend-of-zelda-40th-anniversary-edition-console-nintendo-switch-2/1601230011.prd
    # Very console + game:  https://www.very.co.uk/nintendo-the-legend-of-zelda-40th-anniversary-edition-console-the-legend-ofnbspzeldanbspocarina-of-time-nintendo-switch-2/1601230021.prd
]

# Word that must appear in a product's structured-data name, so we read
# the right product and ignore "you might also like" items on the page.
PRODUCT_KEYWORD = "zelda"

# Warn you after this many failed reads in a row (roughly 30 minutes).
FAILS_BEFORE_WARNING = 3

STATE_FILE = "state.json"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
MANUAL_RUN = os.environ.get("MANUAL_RUN", "").lower() == "true"

# ---------------------------------------------------------------------
#  Detection rules
# ---------------------------------------------------------------------
IN_SCHEMA = ("instock", "preorder", "presale", "backorder", "limitedavailability", "onlineonly")
OUT_SCHEMA = ("outofstock", "soldout", "discontinued", "instoreonly")

IN_PHRASES = ("add to basket", "add to trolley", "add to cart", "pre-order now", "preorder now", "pre order now")
OUT_PHRASES = ("out of stock", "sold out", "currently unavailable", "temporarily unavailable",
               "not currently available", "not available to order")

# Signs we've been served a bot-check page rather than the product page.
BLOCK_MARKERS = ("robot check", "/errors/validatecaptcha", "enter the characters you see below",
                 "pardon our interruption", "access denied", "cf-chl-", "just a moment...",
                 "px-captcha", "incapsula incident", "request unsuccessful",
                 "verify you are a human", "are you a robot")

LDJSON_RE = re.compile(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
META_KEY_RE = re.compile(r'(?:property|name)=["\']([^"\']+)["\']', re.I)
META_CONTENT_RE = re.compile(r'content=["\']([^"\']*)["\']', re.I)
ITEMPROP_RE = re.compile(r'itemprop=["\']availability["\'][^>]*(?:href|content)=["\']([^"\']+)', re.I)
AMAZON_PRICE_RE = re.compile(r'class="a-offscreen">\s*£\s*([\d,]+\.\d{2})')


def squash(value):
    """'https://schema.org/InStock' -> 'instock'."""
    return re.sub(r"[^a-z]", "", str(value).rsplit("/", 1)[-1].lower())


def to_price(value):
    try:
        return float(str(value).replace(",", "").replace("£", "").strip())
    except (TypeError, ValueError):
        return None


def walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def classify(avail):
    a = squash(avail)
    if any(a.startswith(x) for x in IN_SCHEMA):
        return "in"
    if any(a.startswith(x) for x in OUT_SCHEMA):
        return "out"
    return None


def from_structured_data(html):
    """Read schema.org Product data. Most reliable signal when present."""
    ins, outs = [], []
    for block in LDJSON_RE.findall(html):
        data = None
        for text in (block.strip(), unescape(block.strip())):
            try:
                data = json.loads(text)
                break
            except ValueError:
                continue
        if data is None:
            continue
        for obj in walk(data):
            types = obj.get("@type")
            types = types if isinstance(types, list) else [types]
            if not any(t in ("Product", "ProductGroup") for t in types):
                continue
            if PRODUCT_KEYWORD not in str(obj.get("name", "")).lower():
                continue
            for offer in walk(obj.get("offers") or []):
                if "availability" not in offer:
                    continue
                status = classify(offer["availability"])
                price = to_price(offer.get("price") or offer.get("lowPrice"))
                if status == "in":
                    ins.append(price)
                elif status == "out":
                    outs.append(price)
    if ins:
        prices = [p for p in ins if p]
        return "in", (min(prices) if prices else None), "structured data"
    if outs:
        return "out", None, "structured data"
    return None


def meta_values(html):
    """{'product:availability': 'Pre-order now', 'product:price:amount': '434.99', ...}"""
    found = {}
    for tag in META_TAG_RE.findall(html):
        key, content = META_KEY_RE.search(tag), META_CONTENT_RE.search(tag)
        if key and content:
            found.setdefault(key.group(1).lower(), unescape(content.group(1)))
    return found


def from_meta_tags(html):
    meta = meta_values(html)
    values = [meta[k] for k in ("product:availability", "og:availability") if k in meta]
    values += ITEMPROP_RE.findall(html)
    results = [classify(v) for v in values]
    price = to_price(meta.get("product:price:amount") or meta.get("og:price:amount"))
    if "in" in results and "out" not in results:
        return "in", price, "meta tags"
    if "out" in results:
        return "out", None, "meta tags"
    return None


def visible_text(html):
    html = re.sub(r"<(script|style|noscript|template)\b.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", unescape(text)).lower()


def from_page_text(html):
    text = visible_text(html)
    if any(p in text for p in OUT_PHRASES):
        return "out", None, "page text"
    if any(p in text for p in IN_PHRASES):
        return "in", None, "page text"
    return None


def amazon_rules(html):
    lower = html.lower()
    price = None
    core = lower.find("coreprice")
    if core != -1:
        m = AMAZON_PRICE_RE.search(html, core)
        price = to_price(m.group(1)) if m else None
    if 'id="add-to-cart-button"' in lower or 'id="buy-now-button"' in lower:
        return "in", price, "Amazon basket button"
    if "currently unavailable" in lower or 'id="outofstock"' in lower:
        return "out", None, "Amazon unavailable notice"
    return None


def looks_blocked(status_code, html):
    if status_code in (401, 403, 429, 503):
        return True
    if len(html) < 60000:
        lower = html.lower()
        return any(m in lower for m in BLOCK_MARKERS)
    return False


def check(product):
    """Returns (status, price, how). status: in / out / blocked / unknown / error."""
    try:
        r = requests.get(product["url"], impersonate="chrome", timeout=30,
                         headers={"Accept-Language": "en-GB,en;q=0.9"})
    except Exception as exc:  # network error, timeout, etc.
        return "error", None, type(exc).__name__
    html = r.text or ""
    if looks_blocked(r.status_code, html):
        return "blocked", None, f"HTTP {r.status_code}"
    if r.status_code >= 400:
        return "error", None, f"HTTP {r.status_code}"

    rules = [from_structured_data, from_meta_tags, from_page_text]
    if "amazon." in product["url"]:
        rules.insert(0, amazon_rules)
    for rule in rules:
        result = rule(html)
        if result:
            return result
    return "unknown", None, "no stock signal found"


# ---------------------------------------------------------------------
#  Notifications
# ---------------------------------------------------------------------
def notify(title, message, priority=3, click=None, tags=None):
    if not NTFY_TOPIC:
        print(f"  [no NTFY_TOPIC set, would have sent] {title}: {message}")
        return
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message, "priority": priority}
    if click:
        payload["click"] = click
    if tags:
        payload["tags"] = tags
    try:
        requests.post("https://ntfy.sh/", json=payload, timeout=15)
    except Exception as exc:
        print(f"  Notification failed: {exc}")


def label(p):
    return f"{p['item']} @ {p['retailer']}"


def describe(entry):
    status = entry.get("status", "?")
    if status == "in":
        price = entry.get("price")
        return "IN STOCK" + (f" £{price:.2f}" if price else "")
    return {"out": "out of stock", "blocked": "blocked", "unknown": "can't tell",
            "error": "error"}.get(status, status)


# ---------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def main():
    state = load_state()
    first_run = state is None
    state = state or {}
    items = state.setdefault("items", {})

    for i, product in enumerate(PRODUCTS):
        if i:
            time.sleep(random.uniform(2, 5))  # be polite between requests
        url = product["url"]
        status, price, how = check(product)
        print(f"{label(product):32} {status:8} {('£%.2f' % price) if price else '':10} ({how})")

        prev = items.get(url, {})
        # No timestamps stored, so state.json only changes when something
        # actually changes (keeps the repo history tidy).
        entry = {"status": status, "price": price,
                 "last_good": prev.get("last_good"), "fails": 0,
                 "warned": prev.get("warned", False)}

        if status in ("in", "out"):
            if prev.get("warned"):
                notify(f"Tracker OK again: {product['retailer']}",
                       f"{label(product)} is readable again.", priority=2)
                entry["warned"] = False

            cap = product.get("max_price")
            over_cap = status == "in" and price and cap and price > cap
            last = prev.get("last_good")
            if over_cap:
                if last not in ("in", "over_cap"):
                    notify(f"In stock but £{price:.2f}: {label(product)}",
                           f"Above your £{cap} cap, probably a marketplace seller. Tap to check.",
                           priority=3, click=url, tags=["moneybag"])
                entry["last_good"] = "over_cap"
            elif status == "in":
                if last != "in":
                    price_text = f" at £{price:.2f}" if price else ""
                    notify(f"IN STOCK: {label(product)}",
                           f"Zelda {product['item']} is available at {product['retailer']}{price_text}. "
                           f"Tap to open the page.",
                           priority=5, click=url, tags=["rotating_light", "video_game"])
                entry["last_good"] = "in"
            else:
                entry["last_good"] = "out"
        else:
            entry["fails"] = min(prev.get("fails", 0) + 1, FAILS_BEFORE_WARNING)
            if entry["fails"] == FAILS_BEFORE_WARNING and not entry["warned"]:
                notify(f"Can't read {product['retailer']}",
                       f"{label(product)} has failed {FAILS_BEFORE_WARNING} checks in a row ({status}: {how}). "
                       f"Check it manually for now.", priority=2, click=url, tags=["warning"])
                entry["warned"] = True

        items[url] = entry

    # Drop entries for products you've removed from the list.
    live_urls = {p["url"] for p in PRODUCTS}
    for url in list(items):
        if url not in live_urls:
            del items[url]

    summary = "\n".join(f"{label(p)}: {describe(items[p['url']])}" for p in PRODUCTS)
    week = datetime.now(timezone.utc).strftime("%G-W%V")

    if first_run:
        notify("Zelda tracker is live", summary, priority=3, tags=["white_check_mark"])
    elif MANUAL_RUN:
        notify("Zelda tracker: manual check", summary, priority=3)
    elif state.get("heartbeat_week") != week:
        notify("Zelda tracker: weekly check-in", summary, priority=2)
    state["heartbeat_week"] = week

    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


if __name__ == "__main__":
    main()
