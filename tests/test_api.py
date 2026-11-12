from fastapi.testclient import TestClient

from hiclose.agent import Checkout, ConversationStore, SalesAgent
from hiclose.api.app import create_app
from hiclose.api.service import HiCloseService
from hiclose.channels import WebChannel
from hiclose.paypal.orders import OrderRecord
from tests.fakes import FakeModel, call, make_shop, say


class FakePayments:
    def __init__(self):
        self._orders: dict[str, OrderRecord] = {}

    def create_checkout(self, quote):
        order_id = f"ORD-{len(self._orders) + 1}"
        url = f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}"
        self._orders[order_id] = OrderRecord(order_id, quote, url)
        return Checkout(order_id, url)

    def get_order(self, order_id):
        return self._orders.get(order_id)

    def orders(self):
        return list(self._orders.values())

    def confirm(self, order_id):
        record = self._orders[order_id]
        if record.status == "CREATED":
            record.status = "PAID"
            record.ship_to = {"address": {"admin_area_2": "San Jose", "admin_area_1": "CA", "country_code": "US"}}
        return record

    def add_tracking(self, order_id, tracking_number, carrier_name):
        record = self._orders[order_id]
        self.tracked = getattr(self, "tracked", []) + [(order_id, tracking_number, carrier_name)]
        record.tracking_number = record.tracking_number or tracking_number
        return record

    def cancel(self, order_id):
        record = self._orders.get(order_id)
        if record and record.status == "CREATED":
            record.status = "FAILED"
        return record


def build(script):
    shop = make_shop()
    payments = FakePayments()
    shop.checkout = payments
    model = FakeModel(script)
    web = WebChannel()
    service = HiCloseService(SalesAgent(model, shop, ConversationStore()), shop, payments, {"web": web}, workers=4)
    return TestClient(create_app(service, web)), service, shop, model


def transcript(client, customer="riya"):
    return client.get(f"/api/chat/{customer}/messages").json()["messages"]


def test_shop_lists_every_post_without_floor_prices():
    client, *_ = build([])
    data = client.get("/api/shop").json()
    assert data["brand"] == "Indigo Lane"
    assert [p["post_id"] for p in data["posts"]] == ["post-001", "post-002", "post-003", "post-004", "post-005"]
    assert "6800" not in str(data) and "floor" not in str(data)


def test_chat_round_trip_lands_in_the_transcript():
    client, _, _, model = build([say("Hey! The denim jacket is $80 ✨")])
    assert client.post("/api/chat", json={"customer_id": "riya", "text": "hi", "post_id": "post-001"}).status_code == 200
    messages = transcript(client)
    assert [(m["from"], m["text"]) for m in messages] == [("customer", "hi"), ("shop", "Hey! The denim jacket is $80 ✨")]
    assert "jacket-denim" in model.seen[0][0]["text"]


def test_empty_message_is_refused():
    client, *_ = build([])
    assert client.post("/api/chat", json={"customer_id": "riya", "text": "  "}).status_code == 400


def test_full_purchase_ends_with_a_paid_message_in_the_chat():
    client, service, shop, _ = build([
        call("propose_price", product_id="jacket-denim", offered_amount="70"), say("Deal at $70!"),
        call("checkout", product_id="jacket-denim", size="M"), say("Here's your checkout for $70"),
    ])
    client.post("/api/chat", json={"customer_id": "riya", "text": "70?"})
    client.post("/api/chat", json={"customer_id": "riya", "text": "M please"})
    checkout = transcript(client)[-1]
    assert checkout["checkout_url"] == "https://www.sandbox.paypal.com/checkoutnow?token=ORD-1"
    page = client.get("/checkout/return", params={"token": "ORD-1"})
    assert "Paid ✅" in page.text
    paid = transcript(client)[-1]["text"]
    assert paid.startswith("Paid ✅ $70 for the Washed Denim Jacket (M)")
    assert "San Jose, CA" in paid
    assert service.stats.paid == 1 and service.stats.revenue_cents == {"USD": 7000}


def test_returning_twice_sends_one_paid_message():
    client, service, *_ = build([call("checkout", product_id="tote-canvas", size="ONE"), say("Here's your checkout for $32")])
    client.post("/api/chat", json={"customer_id": "riya", "text": "the tote please"})
    client.get("/checkout/return", params={"token": "ORD-1"})
    client.get("/checkout/return", params={"token": "ORD-1"})
    paid = [m["text"] for m in transcript(client) if m["text"].startswith("Paid ✅")]
    assert len(paid) == 1
    assert "(ONE)" not in paid[0]
    assert service.stats.paid == 1


def test_cancelled_checkout_puts_the_stock_back_and_tells_the_customer():
    client, _, shop, _ = build([call("checkout", product_id="jacket-denim", size="M"), say("Here's your checkout for $80")])
    client.post("/api/chat", json={"customer_id": "riya", "text": "M please"})
    assert shop.catalog.in_stock("jacket-denim", "M") == 1
    client.get("/checkout/cancel", params={"token": "ORD-1"})
    assert shop.catalog.in_stock("jacket-denim", "M") == 2
    assert "cancelled" in transcript(client)[-1]["text"]


def test_expired_hold_puts_the_stock_back_and_paid_orders_are_kept():
    from datetime import timedelta

    client, service, shop, _ = build([
        call("checkout", product_id="jacket-denim", size="M"), say("Here's your checkout for $80"),
        call("checkout", product_id="jacket-denim", size="L"), say("Here's your checkout for $80"),
    ])
    client.post("/api/chat", json={"customer_id": "riya", "text": "M please"})
    client.post("/api/chat", json={"customer_id": "tom", "text": "L please"})
    client.get("/checkout/return", params={"token": "ORD-2"})
    assert (shop.catalog.in_stock("jacket-denim", "M"), shop.catalog.in_stock("jacket-denim", "L")) == (1, 2)
    later = service.payments.get_order("ORD-1").quote.expires_at + timedelta(seconds=1)
    assert service.release_expired(later) == ["ORD-1"]
    assert (shop.catalog.in_stock("jacket-denim", "M"), shop.catalog.in_stock("jacket-denim", "L")) == (2, 2)
    assert service.payments.get_order("ORD-2").status == "PAID"
    assert service.release_expired(later) == []


def test_duplicate_message_is_handled_once():
    client, _, _, model = build([say("Hi there!")])
    body = {"customer_id": "riya", "text": "hi", "message_id": "m-1"}
    client.post("/api/chat", json=body)
    client.post("/api/chat", json=body)
    assert len(model.seen) == 1


def test_switched_off_agent_sends_a_holding_reply():
    client, service, _, model = build([])
    service.enabled = False
    client.post("/api/chat", json={"customer_id": "riya", "text": "hi"})
    assert transcript(client)[-1]["text"] == "Thanks for your message! We'll reply soon."
    assert model.seen == []


def test_paypal_webhook_needs_configuring():
    client, *_ = build([])
    assert client.post("/webhooks/paypal", json={"id": "EV1"}).status_code == 503


def test_stats_count_the_funnel():
    client, *_ = build([call("checkout", product_id="tote-canvas", size="ONE"), say("Here's your checkout for $32")])
    client.post("/api/chat", json={"customer_id": "riya", "text": "the tote please"})
    client.get("/checkout/return", params={"token": "ORD-1"})
    stats = client.get("/api/stats").json()
    assert (stats["messages"], stats["checkouts"], stats["paid"], stats["revenue"]) == (1, 1, 1, {"USD": "$32"})


def test_one_visitor_is_rate_limited():
    from hiclose.api.ratelimit import RateLimiter

    shop = make_shop()
    web = WebChannel()
    service = HiCloseService(SalesAgent(FakeModel([say("hi!"), say("hi!")]), shop, ConversationStore()), shop, FakePayments(), {"web": web})
    client = TestClient(create_app(service, web, limiter=RateLimiter(2, 60)))
    codes = [client.post("/api/chat", json={"customer_id": "riya", "text": f"hi {i}"}).status_code for i in range(3)]
    assert codes == [200, 200, 429]


def test_rate_limit_window_slides():
    from hiclose.api.ratelimit import RateLimiter

    now = [0.0]
    limiter = RateLimiter(1, 60, clock=lambda: now[0])
    assert limiter.allow("ip") and not limiter.allow("ip")
    now[0] = 60
    assert limiter.allow("ip")


def test_owner_dashboard_shows_the_funnel_chats_and_orders():
    client, *_ = build([
        call("propose_price", product_id="jacket-denim", offered_amount="70"), say("Deal at $70!"),
        call("checkout", product_id="jacket-denim", size="M"), say("Here's your checkout for $70"),
        say("Hi! How can I help?"),
    ])
    client.post("/api/chat", json={"customer_id": "riya", "text": "70?"})
    client.post("/api/chat", json={"customer_id": "riya", "text": "M please"})
    client.post("/api/chat", json={"customer_id": "tom", "text": "hi"})
    client.get("/checkout/return", params={"token": "ORD-1"})
    data = client.get("/api/owner").json()
    assert [(s["label"], s["count"]) for s in data["funnel"]] == [
        ("Started a chat", 2), ("Kept talking", 1), ("Negotiated", 1), ("Got a checkout link", 1), ("Paid", 1)]
    stages = {c["customer"]: c["stage"] for c in data["chats"]}
    assert stages == {"Web riya": "paid", "Web tom": "chatting"}
    assert data["orders"] == [{"order_id": "ORD-1", "product": "Washed Denim Jacket", "size": "M", "price": "$70",
                               "list_price": "$80", "status": "PAID", "tracking": "", "ships_to": "San Jose, US"}]
    assert client.get("/owner").status_code == 200


def test_whatsapp_numbers_are_masked_on_the_dashboard():
    from hiclose.api.service import masked
    assert masked("whatsapp:+919876543298") == "WhatsApp ···3298"


def test_handed_off_chat_shows_owner_needed():
    client, *_ = build([call("hand_to_owner", reason="complaint"), say("The owner will reply soon.")])
    client.post("/api/chat", json={"customer_id": "riya", "text": "it arrived broken"})
    assert client.get("/api/owner").json()["chats"][0]["stage"] == "owner needed"


def test_chat_page_is_served():
    client, *_ = build([])
    assert "HiClose demo shop" in client.get("/").text


def test_product_photos_are_served_and_reach_the_web_transcript():
    client, *_ = build([call("show_photo", product_id="scarf-silk"), say("Here's the indigo scarf!")])
    posts = client.get("/api/shop").json()["posts"]
    assert posts[0]["image_url"] == "/products/jacket-denim.jpg"
    photo = client.get("/products/jacket-denim.jpg")
    assert photo.status_code == 200 and photo.headers["content-type"] == "image/jpeg"
    client.post("/api/chat", json={"customer_id": "riya", "text": "show me the scarf"})
    assert transcript(client)[-1]["image_url"] == "/products/scarf-silk.jpg"
