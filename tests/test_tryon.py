import asyncio
import base64
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from hiclose.agent import ConversationStore, SalesAgent
from hiclose.agent.conversation import MAX_TRYONS, PHOTO_TTL_SECONDS, Conversation
from hiclose.agent.tryon import FakeImageModel, Photo, TryOnStudio
from hiclose.api.app import create_app
from hiclose.api.service import HiCloseService
from hiclose.channels import WebChannel
from hiclose.channels.whatsapp import WhatsAppChannel
from tests.fakes import FakeModel, call, make_shop, say
from tests.test_api import FakePayments

PRODUCTS = Path(__file__).parent.parent / "hiclose" / "api" / "static" / "products"
SELFIE = Photo(b"\xff\xd8selfie", "image/jpeg")
SELFIE_URL = "data:image/jpeg;base64," + base64.b64encode(SELFIE.data).decode()


def studio_shop(result=FakeImageModel().result):
    shop = make_shop()
    model = FakeImageModel(result)
    shop.tryon = TryOnStudio(model, PRODUCTS)
    return shop, model


def results(model):
    """The tool results the agent saw, by tool name."""
    return [r for turn in model.seen[-1] if isinstance(turn, dict) and turn.get("role") == "tool" for r in turn["results"]]


def test_try_on_without_a_photo_asks_for_one_and_makes_nothing():
    shop, images = studio_shop()
    chat = FakeModel([call("try_on", product_id="jacket-denim"), say("Send me a photo of you and I'll show you!")])
    reply = SalesAgent(chat, shop, ConversationStore()).handle("web:riya", "how would it look on me?")
    assert reply.image_url is None and images.calls == []
    assert results(chat)[0][1]["needs_photo"] is True


def test_try_on_uses_the_customers_photo_and_our_product_photo():
    shop, images = studio_shop()
    chat = FakeModel([call("try_on", product_id="jacket-denim"), say("Here's a preview of you in it! M or L?")])
    reply = SalesAgent(chat, shop, ConversationStore()).handle("web:riya", "(sent a photo)", photo=SELFIE)
    assert reply.image_url.startswith("/tryon/")
    person, garment, item = images.calls[0]
    assert person == SELFIE and garment.data == (PRODUCTS / "jacket-denim.jpg").read_bytes()
    assert "Washed Denim Jacket" in item
    assert shop.tryon.get(reply.image_url.removeprefix("/tryon/")) == images.result
    assert "sent a photo" in chat.seen[0][0]["text"]


def test_photo_is_remembered_for_a_later_try_on_request():
    shop, images = studio_shop()
    store = ConversationStore()
    chat = FakeModel([say("Lovely photo! Which item?"), call("try_on", product_id="scarf-silk"), say("Here you go!")])
    agent = SalesAgent(chat, shop, store)
    agent.handle("web:riya", "(sent a photo)", photo=SELFIE)
    assert agent.handle("web:riya", "the scarf please").image_url.startswith("/tryon/")


def test_customer_photo_expires():
    convo = Conversation("web:riya")
    convo.keep_photo(SELFIE, now=100.0)
    assert convo.fresh_photo(now=100.0 + PHOTO_TTL_SECONDS - 1) == SELFIE
    assert convo.fresh_photo(now=100.0 + PHOTO_TTL_SECONDS + 1) is None and convo.photo is None


def test_try_ons_are_capped_per_chat():
    shop, images = studio_shop()
    store = ConversationStore()
    store.get("web:riya").tryons = MAX_TRYONS
    chat = FakeModel([call("try_on", product_id="jacket-denim"), say("Which size would you like?")])
    reply = SalesAgent(chat, shop, store).handle("web:riya", "one more", photo=SELFIE)
    assert reply.image_url is None and images.calls == []


def test_failed_try_on_is_reported_to_the_agent_not_the_customer():
    shop, _ = studio_shop(result=None)
    chat = FakeModel([call("try_on", product_id="jacket-denim"), say("Could you send a clearer photo?")])
    reply = SalesAgent(chat, shop, ConversationStore()).handle("web:riya", "(sent a photo)", photo=SELFIE)
    assert reply.image_url is None and results(chat)[0][1]["ok"] is False


def test_try_on_is_off_without_a_studio():
    chat = FakeModel([call("try_on", product_id="jacket-denim"), say("Here's a photo instead!")])
    reply = SalesAgent(chat, make_shop(), ConversationStore()).handle("web:riya", "on me?", photo=SELFIE)
    assert reply.image_url is None and results(chat)[0][1]["ok"] is False


def test_results_expire():
    now = [0.0]
    studio = TryOnStudio(FakeImageModel(), PRODUCTS, ttl_seconds=60, clock=lambda: now[0])
    token = studio.render(SELFIE, make_shop().catalog.get("tote-canvas")).removeprefix("/tryon/")
    assert studio.get(token) is not None
    now[0] = 61
    assert studio.get(token) is None


def test_web_upload_goes_end_to_end_and_the_preview_is_served():
    shop, images = studio_shop()
    payments = FakePayments()
    shop.checkout = payments
    web = WebChannel()
    chat = FakeModel([call("try_on", product_id="jacket-denim"), say("Here's you in the jacket!")])
    service = HiCloseService(SalesAgent(chat, shop, ConversationStore()), shop, payments, {"web": web})
    client = TestClient(create_app(service, web))
    assert client.post("/api/chat", json={"customer_id": "riya", "text": "", "photo": SELFIE_URL, "post_id": "post-001"}).status_code == 200
    assert images.calls[0][0] == SELFIE
    messages = client.get("/api/chat/riya/messages").json()["messages"]
    assert messages[0]["text"] == "(sent a photo)"
    preview = client.get(messages[-1]["image_url"])
    assert preview.status_code == 200 and preview.content == images.result.data
    assert client.get("/tryon/not-a-real-token").status_code == 404


def test_web_refuses_non_images_and_bad_data():
    web = WebChannel()
    assert web.parse({"customer_id": "riya", "text": "", "photo": "data:text/html;base64,PGgxPg=="}) == []
    assert web.parse({"customer_id": "riya", "text": "", "photo": "https://evil.example/me.jpg"}) == []
    [message] = web.parse({"customer_id": "riya", "text": "hi", "photo": "data:image/png;base64,!!!"})
    assert message.media_urls == ()


def test_web_upload_is_handed_over_once():
    web = WebChannel()
    [message] = web.parse({"customer_id": "riya", "text": "", "photo": SELFIE_URL})
    assert asyncio.run(web.fetch_photo(message.media_urls[0])) == SELFIE
    assert asyncio.run(web.fetch_photo(message.media_urls[0])) is None


def test_whatsapp_downloads_only_from_twilio_with_our_credentials():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=SELFIE.data, headers={"content-type": "image/jpeg"})

    channel = WhatsAppChannel("AC123", "tok", "whatsapp:+14155238886", transport=httpx.MockTransport(handler))
    url = "https://api.twilio.com/2010-04-01/Accounts/AC123/Messages/MM1/Media/ME1"
    assert asyncio.run(channel.fetch_photo(url)) == SELFIE
    assert seen[0].headers["authorization"].startswith("Basic ")
    assert asyncio.run(channel.fetch_photo("https://evil.example/selfie.jpg")) is None
    assert asyncio.run(channel.fetch_photo("http://api.twilio.com/x")) is None
    assert len(seen) == 1
