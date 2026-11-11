"""Channel3: product search across 100M+ products at other stores. We use it two ways, neither of
which lets it touch our own prices or checkout:

1. Market prices for the owner. What similar items sell for elsewhere, next to our list price, so
   the owner can set list and floor prices. Owner-facing only: these numbers never reach the sales
   agent (hard rule 1), and no customer data is sent.
2. Photo matching. A customer sends a photo ("do you have this?"); Channel3 says what it is, and
   our own code maps that to the closest products *we* sell. The agent sees only what the item is
   and our product IDs: no other stores, brands' prices or links.

Results are cached (6 hours by default) so the free tier's monthly quota lasts."""

import base64
import logging
import os
import re
import statistics
import threading
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from hiclose.agent.money import fmt
from hiclose.agent.tryon import Photo

API = "https://api.trychannel3.com/v1"
log = logging.getLogger("hiclose")


class Channel3Error(Exception):
    pass


@dataclass(frozen=True)
class MarketItem:
    title: str
    brand: str
    category: str
    price_cents: int | None
    currency: str | None
    domain: str
    url: str


def _cents(value) -> int | None:
    try:
        return round(float(value) * 100)
    except (TypeError, ValueError):
        return None


def _item(product: dict) -> MarketItem:
    offers = [o for o in product.get("offers") or [] if (o.get("price") or {}).get("price") is not None]
    cheapest = min(offers, key=lambda o: o["price"]["price"]) if offers else {}
    price = cheapest.get("price") or {}
    brands = product.get("brands") or []
    category = product.get("category") or {}
    return MarketItem(
        title=str(product.get("title", ""))[:160],
        brand=str(brands[0].get("name", "")) if brands else "",
        category=str(category.get("path") or category.get("title") or ""),
        price_cents=_cents(price.get("price")),
        currency=price.get("currency"),
        domain=str(cheapest.get("domain", "")),
        url=str(cheapest.get("url", "")),
    )


class Channel3Client:
    def __init__(self, api_key: str, transport: httpx.BaseTransport | None = None, cache_seconds: int = 6 * 3600,
                 clock: Callable[[], float] = time.monotonic):
        self._key = api_key
        self._transport = transport
        self._ttl = cache_seconds
        self._clock = clock
        self._cache: dict[str, tuple[float, list[MarketItem]]] = {}
        self._lock = threading.Lock()
        self.calls = 0

    @classmethod
    def from_env(cls) -> "Channel3Client | None":
        key = os.environ.get("CHANNEL3_API_KEY", "")
        return cls(key) if key else None

    def _post(self, path: str, body: dict) -> list[MarketItem]:
        self.calls += 1
        try:
            with httpx.Client(transport=self._transport, timeout=12) as client:
                response = client.post(f"{API}/{path}", json=body, headers={"x-api-key": self._key})
        except httpx.HTTPError as error:
            raise Channel3Error(f"channel3 unreachable: {error}") from None
        if response.is_error:
            raise Channel3Error(f"channel3 {path} failed: {response.status_code} {response.text[:200]}")
        return [_item(p) for p in response.json().get("products") or []]

    def search(self, query: str, limit: int = 20, currency: str = "USD") -> list[MarketItem]:
        key = f"search:{currency}:{limit}:{query.lower()}"
        with self._lock:
            hit = self._cache.get(key)
            if hit and self._clock() - hit[0] < self._ttl:
                return hit[1]
        items = self._post("search", {"query": query, "limit": limit, "config": {"currency": currency, "country": "US"}})
        with self._lock:
            self._cache[key] = (self._clock(), items)
        return items

    def image_search(self, photo: Photo, limit: int = 8) -> list[MarketItem]:
        # Not cached: customer photos are never kept beyond the conversation (hard rule 12).
        return self._post("image-search", {"base64_image": base64.b64encode(photo.data).decode(),
                                           "limit": limit, "segment": "AUTO"})


# ---- 1. market prices for the owner -------------------------------------------------------------


def market_report(client: Channel3Client, catalog) -> list[dict]:
    """One row per product: what similar items cost elsewhere vs our list price."""
    rows = []
    for product in catalog.products():
        row = {"product_id": product.product_id, "product": product.name,
               "our_price": fmt(product.list_cents, product.currency), "our_cents": product.list_cents}
        try:
            items = client.search(product.name, currency=product.currency)
        except Channel3Error as error:
            log.warning("market check for %s failed: %s", product.product_id, error)
            rows.append({**row, "status": "unavailable", "compared": 0})
            continue
        prices = sorted(i.price_cents for i in items if i.price_cents and i.currency == product.currency)
        if not prices:
            rows.append({**row, "status": "no matches", "compared": 0})
            continue
        low, high, median = prices[0], prices[-1], round(statistics.median(prices))
        q1, q3 = (statistics.quantiles(prices, n=4)[0], statistics.quantiles(prices, n=4)[2]) if len(prices) >= 4 else (low, high)
        position = "below market" if product.list_cents < q1 else "above market" if product.list_cents > q3 else "in line"
        examples = [i for i in items if i.price_cents and i.currency == product.currency][:3]
        rows.append({
            **row, "status": position, "compared": len(prices),
            "low": fmt(low, product.currency), "median": fmt(median, product.currency), "high": fmt(high, product.currency),
            "median_cents": median,
            "examples": [{"title": i.title, "brand": i.brand, "store": i.domain, "price": fmt(i.price_cents, product.currency)}
                         for i in examples],
        })
    return rows


# ---- 2. photo matching for the agent -------------------------------------------------------------

SYNONYMS = {
    "jacket": {"jacket", "jackets", "coat", "trucker", "outerwear", "blazer"},
    "denim": {"denim", "jean", "jeans", "chambray"},
    "tote": {"tote", "totes", "bag", "bags", "handbag", "handbags", "shopper", "purse"},
    "canvas": {"canvas", "cotton"},
    "scarf": {"scarf", "scarves", "shawl", "stole", "bandana", "wrap"},
    "silk": {"silk", "satin"},
    "shirt": {"shirt", "shirts", "top", "tops", "blouse", "button-down", "button", "camp"},
    "linen": {"linen"},
}
WORD = re.compile(r"[a-z][a-z-]+")


def _words(text: str) -> set[str]:
    return set(WORD.findall(text.lower()))


def _concepts(words: set[str]) -> set[str]:
    return {concept for concept, forms in SYNONYMS.items() if words & forms}


class PhotoMatcher:
    def __init__(self, client: Channel3Client, catalog):
        self._client = client
        self._catalog = catalog

    def match(self, photo: Photo) -> dict:
        items = self._client.image_search(photo)
        if not items:
            return {"looks_like": None, "our_matches": []}
        seen_words = [_words(f"{i.title} {i.category}") for i in items[:5]]
        seen = _concepts(set().union(*seen_words))
        scores = []
        for product in self._catalog.products():
            ours = _concepts(_words(f"{product.name} {product.description}"))
            score = len(ours & seen)
            if score:
                scores.append((score, product.product_id))
        scores.sort(reverse=True)
        best = scores[0][0] if scores else 0
        # A short, brand-free description of what's in the photo, from the most common category.
        categories = [i.category.split(">")[-1].strip() for i in items[:5] if i.category]
        looks_like = statistics.mode(categories) if categories else (" ".join(sorted(seen)) or None)
        return {"looks_like": looks_like, "our_matches": [pid for score, pid in scores if score == best][:2]}
