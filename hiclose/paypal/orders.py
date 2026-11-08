"""PayPal orders for agreed quotes. An order becomes PAID only from PayPal's own capture response."""

import os
from dataclasses import dataclass
from typing import Literal

from hiclose.agent.tools import Checkout
from hiclose.paypal.client import PayPalClient, PayPalError
from hiclose.rules import Quote

Status = Literal["CREATED", "PAID", "FAILED", "REFUNDED"]
ZERO_DECIMAL = {"JPY", "HUF", "TWD"}


def to_value(cents: int, currency: str) -> str:
    return str(cents // 100) if currency in ZERO_DECIMAL else f"{cents // 100}.{cents % 100:02d}"


def to_cents(value: str, currency: str) -> int:
    whole, _, frac = value.partition(".")
    return int(whole) * 100 + (0 if currency in ZERO_DECIMAL else int((frac + "00")[:2]))


@dataclass
class OrderRecord:
    order_id: str
    quote: Quote
    approve_url: str
    status: Status = "CREATED"
    capture_id: str | None = None
    payer_email: str | None = None
    ship_to: dict | None = None
    refunded_cents: int = 0


class OrderBook:
    def __init__(self):
        self._orders: dict[str, OrderRecord] = {}

    def add(self, record: OrderRecord) -> None:
        self._orders[record.order_id] = record

    def get(self, order_id: str) -> OrderRecord | None:
        return self._orders.get(order_id)

    def all(self) -> list[OrderRecord]:
        return list(self._orders.values())


class PayPalCheckout:
    """The real CheckoutProvider. Creates the order at the quote's price, never at any other."""

    def __init__(self, client: PayPalClient, orders: OrderBook, brand: str, public_base_url: str | None = None):
        self._client = client
        self._orders = orders
        self._brand = brand
        self._base = (
            public_base_url
            or os.environ.get("PUBLIC_BASE_URL")
            or os.environ.get("RENDER_EXTERNAL_URL")
            or "http://localhost:8000"
        ).rstrip("/")

    def create_checkout(self, quote: Quote) -> Checkout:
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": quote.quote_id,
                "custom_id": quote.quote_id,
                "description": f"{quote.product_id} {quote.size}".strip()[:127],
                "amount": {"currency_code": quote.currency, "value": to_value(quote.price_cents, quote.currency)},
            }],
            "payment_source": {"paypal": {"experience_context": {
                "brand_name": self._brand[:127],
                "shipping_preference": "GET_FROM_FILE",
                "user_action": "PAY_NOW",
                "return_url": f"{self._base}/checkout/return",
                "cancel_url": f"{self._base}/checkout/cancel",
            }}},
        }
        data = self._client.request("POST", "/v2/checkout/orders", json=body, request_id=f"create-{quote.quote_id}")
        url = next((l["href"] for l in data.get("links", []) if l.get("rel") in ("payer-action", "approve")), None)
        if not url:
            raise PayPalError(502, {"name": "NO_APPROVE_LINK", "message": "PayPal returned no approval link"})
        self._orders.add(OrderRecord(order_id=data["id"], quote=quote, approve_url=url))
        return Checkout(order_id=data["id"], approve_url=url)

    def get_order(self, order_id: str) -> OrderRecord | None:
        return self._orders.get(order_id)

    def cancel(self, order_id: str) -> OrderRecord | None:
        record = self._orders.get(order_id)
        if record is not None and record.status == "CREATED":
            record.status = "FAILED"
        return record

    def confirm(self, order_id: str) -> OrderRecord:
        """Capture an approved order. Safe to call twice: a paid order is returned as it is."""
        record = self._orders.get(order_id)
        if record is None:
            raise PayPalError(404, {"name": "UNKNOWN_ORDER", "message": f"{order_id} is not a HiClose order"})
        if record.status != "CREATED":
            return record
        try:
            data = self._client.request("POST", f"/v2/checkout/orders/{order_id}/capture", request_id=f"capture-{order_id}")
        except PayPalError as error:
            if isinstance(error.body, dict) and _issue(error.body) == "ORDER_ALREADY_CAPTURED":
                data = self._client.request("GET", f"/v2/checkout/orders/{order_id}")
            else:
                raise
        return self._apply_capture(record, data)

    def _apply_capture(self, record: OrderRecord, data: dict) -> OrderRecord:
        unit = (data.get("purchase_units") or [{}])[0]
        captures = unit.get("payments", {}).get("captures", [])
        capture = captures[0] if captures else {}
        amount = capture.get("amount", {})
        paid = (
            data.get("status") == "COMPLETED"
            and capture.get("status") == "COMPLETED"
            and amount.get("currency_code") == record.quote.currency
            and to_cents(amount.get("value", "0"), record.quote.currency) == record.quote.price_cents
        )
        record.status = "PAID" if paid else "FAILED"
        record.capture_id = capture.get("id")
        record.payer_email = data.get("payer", {}).get("email_address")
        record.ship_to = unit.get("shipping")
        return record

    def refund(self, order_id: str, cents: int) -> OrderRecord:
        """Refund to the original payment only. Called after the rules engine approves (US-13)."""
        record = self._orders.get(order_id)
        if record is None or record.status != "PAID" or not record.capture_id:
            raise PayPalError(409, {"name": "NOT_REFUNDABLE", "message": f"{order_id} is not a paid order"})
        if cents <= 0 or record.refunded_cents + cents > record.quote.price_cents:
            raise PayPalError(409, {"name": "REFUND_TOO_LARGE", "message": "refund is more than was paid"})
        currency = record.quote.currency
        self._client.request(
            "POST", f"/v2/payments/captures/{record.capture_id}/refund",
            json={"amount": {"value": to_value(cents, currency), "currency_code": currency}},
            request_id=f"refund-{order_id}-{record.refunded_cents + cents}",
        )
        record.refunded_cents += cents
        if record.refunded_cents == record.quote.price_cents:
            record.status = "REFUNDED"
        return record


def _issue(body: dict) -> str | None:
    details = body.get("details") or [{}]
    return details[0].get("issue")
