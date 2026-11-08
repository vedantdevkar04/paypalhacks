"""Agreed prices, stored on the server. create_order takes a quote_id, never a number."""

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable


class QuoteError(Exception):
    pass


@dataclass(frozen=True)
class Quote:
    quote_id: str
    product_id: str
    price_cents: int
    currency: str
    expires_at: datetime
    size: str = ""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class QuoteBook:
    def __init__(self, ttl: timedelta = timedelta(minutes=15), clock: Callable[[], datetime] = _utcnow):
        self._ttl = ttl
        self._clock = clock
        self._quotes: dict[str, Quote] = {}

    def create(self, product_id: str, price_cents: int, currency: str = "USD", size: str = "") -> Quote:
        if price_cents <= 0:
            raise QuoteError("a quote must have a positive price")
        quote = Quote(
            quote_id=f"Q-{secrets.token_hex(4)}",
            product_id=product_id,
            price_cents=price_cents,
            currency=currency,
            expires_at=self._clock() + self._ttl,
            size=size,
        )
        self._quotes[quote.quote_id] = quote
        return quote

    def get(self, quote_id: str) -> Quote:
        quote = self._quotes.get(quote_id)
        if quote is None:
            raise QuoteError(f"unknown quote {quote_id}")
        if self._clock() >= quote.expires_at:
            raise QuoteError(f"quote {quote_id} has expired")
        return quote
