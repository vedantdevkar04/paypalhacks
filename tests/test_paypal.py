import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from hiclose.paypal import OrderBook, PayPalCheckout, PayPalClient, PayPalError, PayPalWebhooks
from hiclose.paypal.orders import to_cents, to_value
from hiclose.rules import Quote

SIGNED = {
    "PayPal-Transmission-Id": "t1", "PayPal-Transmission-Time": "now", "PayPal-Cert-Url": "https://api.paypal.com/cert",
    "PayPal-Auth-Algo": "SHA256withRSA", "PayPal-Transmission-Sig": "sig",
}


def quote(cents=7200, currency="USD", qid="Q-abc"):
    return Quote(qid, "jacket-denim", cents, currency, datetime.now(timezone.utc) + timedelta(minutes=15), size="M")


class FakePayPal:
    def __init__(self):
        self.calls: list[tuple[str, str, dict, dict]] = []
        self.tokens_issued = 0
        self.reject_next_token_use = False
        self.capture_amount = "72.00"
        self.capture_status = "COMPLETED"
        self.already_captured = False
        self.signature_ok = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content and request.headers.get("content-type", "").startswith("application/json") else {}
        self.calls.append((request.method, request.url.path, dict(request.headers), body))
        path = request.url.path
        if path == "/v1/oauth2/token":
            self.tokens_issued += 1
            return httpx.Response(200, json={"access_token": f"tok{self.tokens_issued}", "expires_in": 32400})
        if self.reject_next_token_use:
            self.reject_next_token_use = False
            return httpx.Response(401, json={"name": "AUTHENTICATION_FAILURE"})
        if path == "/v2/checkout/orders" and request.method == "POST":
            return httpx.Response(201, json={"id": "ORD1", "status": "PAYER_ACTION_REQUIRED", "links": [
                {"rel": "self", "href": "https://api/ORD1"}, {"rel": "payer-action", "href": "https://sandbox.paypal.com/checkoutnow?token=ORD1"}]})
        if path.endswith("/capture"):
            if self.already_captured:
                return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_ALREADY_CAPTURED"}]})
            return httpx.Response(201, json=self._captured())
        if path == "/v2/checkout/orders/ORD1" and request.method == "GET":
            return httpx.Response(200, json=self._captured())
        if path.startswith("/v2/payments/captures/"):
            return httpx.Response(201, json={"id": "REF1", "status": "COMPLETED"})
        if path.endswith("/track"):
            return httpx.Response(201, json={"id": "ORD1", "status": "COMPLETED"})
        if path == "/v1/notifications/verify-webhook-signature":
            return httpx.Response(200, json={"verification_status": "SUCCESS" if self.signature_ok else "FAILURE"})
        return httpx.Response(404, json={"name": "NOT_FOUND"})

    def _captured(self):
        return {"id": "ORD1", "status": "COMPLETED", "payer": {"email_address": "buyer@example.com"},
                "purchase_units": [{"shipping": {"address": {"country_code": "US"}},
                                    "payments": {"captures": [{"id": "CAP1", "status": self.capture_status,
                                                               "amount": {"currency_code": "USD", "value": self.capture_amount}}]}}]}

    def paths(self):
        return [path for _, path, _, _ in self.calls]


def setup(clock=None):
    fake = FakePayPal()
    kwargs = {"clock": clock} if clock else {}
    client = PayPalClient("id", "secret", transport=httpx.MockTransport(fake), **kwargs)
    checkout = PayPalCheckout(client, OrderBook(), "Indigo Lane", public_base_url="https://hiclose.test")
    return fake, client, checkout


def test_only_the_sandbox_is_allowed():
    with pytest.raises(ValueError):
        PayPalClient("id", "secret", environment="PRODUCTION")


def test_order_is_created_at_the_quote_price_with_an_idempotency_key():
    fake, _, checkout = setup()
    result = checkout.create_checkout(quote())
    assert result.approve_url == "https://sandbox.paypal.com/checkoutnow?token=ORD1"
    method, path, headers, body = fake.calls[-1]
    assert body["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "72.00"}
    assert body["purchase_units"][0]["custom_id"] == "Q-abc"
    assert headers["paypal-request-id"] == "create-Q-abc"
    assert body["payment_source"]["paypal"]["experience_context"]["return_url"] == "https://hiclose.test/checkout/return"


def test_token_is_reused_until_it_expires():
    now = [0.0]
    fake, _, checkout = setup(clock=lambda: now[0])
    checkout.create_checkout(quote(qid="Q-1"))
    checkout.create_checkout(quote(qid="Q-2"))
    assert fake.tokens_issued == 1
    now[0] = 32400
    checkout.create_checkout(quote(qid="Q-3"))
    assert fake.tokens_issued == 2


def test_a_rejected_token_is_refreshed_once():
    fake, _, checkout = setup()
    checkout.create_checkout(quote(qid="Q-1"))
    fake.reject_next_token_use = True
    checkout.create_checkout(quote(qid="Q-2"))
    assert fake.tokens_issued == 2


def test_capture_marks_paid_only_when_paypal_says_completed_for_the_right_amount():
    _, _, checkout = setup()
    order = checkout.create_checkout(quote())
    record = checkout.confirm(order.order_id)
    assert record.status == "PAID"
    assert record.capture_id == "CAP1"
    assert record.payer_email == "buyer@example.com"
    assert record.ship_to == {"address": {"country_code": "US"}}


def test_capture_is_sent_as_json_even_without_a_body():
    fake, _, checkout = setup()
    order = checkout.create_checkout(quote())
    checkout.confirm(order.order_id)
    capture = next(headers for _, path, headers, _ in fake.calls if path.endswith("/capture"))
    assert capture["content-type"] == "application/json"


def test_wrong_captured_amount_is_not_paid():
    fake, _, checkout = setup()
    fake.capture_amount = "1.00"
    order = checkout.create_checkout(quote())
    assert checkout.confirm(order.order_id).status == "FAILED"


def test_pending_capture_is_not_paid():
    fake, _, checkout = setup()
    fake.capture_status = "PENDING"
    order = checkout.create_checkout(quote())
    assert checkout.confirm(order.order_id).status == "FAILED"


def test_confirming_twice_captures_once():
    fake, _, checkout = setup()
    order = checkout.create_checkout(quote())
    checkout.confirm(order.order_id)
    checkout.confirm(order.order_id)
    assert sum(path.endswith("/capture") for path in fake.paths()) == 1


def test_order_already_captured_elsewhere_is_read_back():
    fake, _, checkout = setup()
    order = checkout.create_checkout(quote())
    fake.already_captured = True
    assert checkout.confirm(order.order_id).status == "PAID"


def test_unknown_order_cannot_be_confirmed():
    _, _, checkout = setup()
    with pytest.raises(PayPalError):
        checkout.confirm("ORDER-made-up")


def test_refund_goes_to_the_original_capture_and_never_exceeds_the_payment():
    fake, _, checkout = setup()
    order = checkout.create_checkout(quote())
    checkout.confirm(order.order_id)
    checkout.refund(order.order_id, 2000)
    assert fake.paths()[-1] == "/v2/payments/captures/CAP1/refund"
    with pytest.raises(PayPalError):
        checkout.refund(order.order_id, 6000)
    assert checkout.refund(order.order_id, 5200).status == "REFUNDED"


def test_unpaid_order_cannot_be_refunded():
    _, _, checkout = setup()
    order = checkout.create_checkout(quote())
    with pytest.raises(PayPalError):
        checkout.refund(order.order_id, 100)


def test_verified_approval_webhook_captures_the_order():
    _, client, checkout = setup()
    order = checkout.create_checkout(quote())
    hooks = PayPalWebhooks(client, checkout, "WH-1")
    result = hooks.handle(SIGNED, {"id": "EV1", "event_type": "CHECKOUT.ORDER.APPROVED", "resource": {"id": order.order_id}})
    assert result.accepted and result.order.status == "PAID"


def test_unverified_webhook_is_ignored():
    fake, client, checkout = setup()
    order = checkout.create_checkout(quote())
    fake.signature_ok = False
    result = PayPalWebhooks(client, checkout, "WH-1").handle(SIGNED, {"id": "EV1", "event_type": "CHECKOUT.ORDER.APPROVED", "resource": {"id": order.order_id}})
    assert result.accepted is False
    assert checkout._orders.get(order.order_id).status == "CREATED"


def test_webhook_without_signature_headers_is_ignored():
    _, client, checkout = setup()
    assert PayPalWebhooks(client, checkout, "WH-1").handle({}, {"id": "EV1"}).accepted is False


def test_duplicate_webhook_is_handled_once():
    fake, client, checkout = setup()
    order = checkout.create_checkout(quote())
    hooks = PayPalWebhooks(client, checkout, "WH-1")
    event = {"id": "EV1", "event_type": "CHECKOUT.ORDER.APPROVED", "resource": {"id": order.order_id}}
    hooks.handle(SIGNED, event)
    assert hooks.handle(SIGNED, event).reason == "duplicate event, already handled"


@pytest.mark.parametrize("cents,currency,value", [(7200, "USD", "72.00"), (6550, "EUR", "65.50"), (5, "GBP", "0.05"), (1000, "JPY", "10")])
def test_amounts_convert_both_ways(cents, currency, value):
    assert to_value(cents, currency) == value
    assert to_cents(value, currency) == cents


def test_tracking_goes_to_the_paid_order_once_with_its_capture():
    fake, _, checkout = setup()
    order = checkout.create_checkout(quote())
    with pytest.raises(PayPalError):  # nothing to ship before it's paid
        checkout.add_tracking(order.order_id, "IL1", "Indigo Lane Delivery")
    checkout.confirm(order.order_id)
    record = checkout.add_tracking(order.order_id, "ILP001ORD1", "Indigo Lane Delivery")
    method, path, headers, body = fake.calls[-1]
    assert (method, path) == ("POST", "/v2/checkout/orders/ORD1/track")
    assert body == {"capture_id": "CAP1", "tracking_number": "ILP001ORD1", "carrier": "OTHER",
                    "carrier_name_other": "Indigo Lane Delivery", "notify_payer": True}
    assert headers["paypal-request-id"] == "track-ORD1" and record.tracking_number == "ILP001ORD1"
    checkout.add_tracking(order.order_id, "ILP001ORD1", "Indigo Lane Delivery")
    assert fake.paths().count("/v2/checkout/orders/ORD1/track") == 1
