"""PayPal webhooks. Every event is verified with PayPal before we act on it, and acted on once."""

from dataclasses import dataclass

from hiclose.paypal.client import PayPalClient
from hiclose.paypal.orders import OrderRecord, PayPalCheckout

HEADERS = {
    "transmission_id": "paypal-transmission-id",
    "transmission_time": "paypal-transmission-time",
    "cert_url": "paypal-cert-url",
    "auth_algo": "paypal-auth-algo",
    "transmission_sig": "paypal-transmission-sig",
}


@dataclass(frozen=True)
class WebhookResult:
    accepted: bool
    reason: str
    order: OrderRecord | None = None


class PayPalWebhooks:
    def __init__(self, client: PayPalClient, checkout: PayPalCheckout, webhook_id: str):
        self._client = client
        self._checkout = checkout
        self._webhook_id = webhook_id
        self._seen: set[str] = set()

    def verify(self, headers: dict[str, str], event: dict) -> bool:
        lower = {k.lower(): v for k, v in headers.items()}
        if not self._webhook_id or any(h not in lower for h in HEADERS.values()):
            return False
        body = {field: lower[header] for field, header in HEADERS.items()}
        body.update(webhook_id=self._webhook_id, webhook_event=event)
        result = self._client.request("POST", "/v1/notifications/verify-webhook-signature", json=body)
        return result.get("verification_status") == "SUCCESS"

    def handle(self, headers: dict[str, str], event: dict) -> WebhookResult:
        if not self.verify(headers, event):
            return WebhookResult(False, "signature not verified by PayPal")
        event_id = event.get("id", "")
        if event_id in self._seen:
            return WebhookResult(True, "duplicate event, already handled")
        self._seen.add(event_id)
        kind = event.get("event_type")
        resource = event.get("resource", {})
        if kind == "CHECKOUT.ORDER.APPROVED":
            return WebhookResult(True, "captured", self._checkout.confirm(resource.get("id", "")))
        if kind == "PAYMENT.CAPTURE.COMPLETED":
            order_id = resource.get("supplementary_data", {}).get("related_ids", {}).get("order_id", "")
            return WebhookResult(True, "capture confirmed", self._checkout.confirm(order_id))
        return WebhookResult(True, f"ignored {kind}")
