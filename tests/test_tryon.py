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


class FakeGenai:
    """Stands in for google-genai's client.models: each model name maps to a response or an error."""

    def __init__(self, outcomes, listed=()):
        self.outcomes = outcomes
        self.listed = listed
        self.called: list[str] = []
        self.models = self

    def generate_content(self, model, contents, config):
        self.called.append(model)
        outcome = self.outcomes[model]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def list(self):
        from types import SimpleNamespace
        return [SimpleNamespace(name=f"models/{n}", supported_actions=["generateContent"]) for n in self.listed]


def image_response(data=b"PNG"):
    from google.genai import types
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=[
        types.Part(inline_data=types.Blob(data=b"draft", mime_type="image/png"), thought=True),
        types.Part(inline_data=types.Blob(data=data, mime_type="image/png"))]))])


def text_response(text="I can't edit this photo."):
    from google.genai import types
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text=text)]))])


GARMENT = Photo(b"garment", "image/jpeg")


def test_quota_errors_mean_unavailable_and_pause_further_calls():
    from hiclose.agent.tryon import GeminiImageModel, TryOnFailed
    quota = RuntimeError("429 RESOURCE_EXHAUSTED free tier limit: 0")
    fake = FakeGenai({"a": quota, "b": quota})
    model = GeminiImageModel("k", ["a", "b"], client=fake)
    try:
        model.dress(SELFIE, GARMENT, "Jacket")
        raise AssertionError("expected TryOnFailed")
    except TryOnFailed as failure:
        assert failure.kind == "unavailable" and "RESOURCE_EXHAUSTED" in model.status()["last_error"]
    calls = len(fake.called)
    try:
        model.dress(SELFIE, GARMENT, "Jacket")
    except TryOnFailed as failure:
        assert failure.kind == "unavailable"
    assert len(fake.called) == calls and model.status()["paused_seconds"] > 0


def test_unknown_model_names_fall_back_to_image_models_the_key_can_use():
    from hiclose.agent.tryon import GeminiImageModel
    fake = FakeGenai({"old": RuntimeError("404 NOT_FOUND"), "gemini-9-flash-image": image_response(b"FINAL")},
                     listed=["gemini-9-flash-image", "old"])
    model = GeminiImageModel("k", ["old"], client=fake)
    assert model.dress(SELFIE, GARMENT, "Jacket") == Photo(b"FINAL", "image/png")  # final image, not the draft
    assert fake.called == ["old", "gemini-9-flash-image"] and model.status()["last_model"] == "gemini-9-flash-image"


def test_model_that_declines_means_no_image_so_a_clearer_photo_might_help():
    from hiclose.agent.tryon import GeminiImageModel
    model = GeminiImageModel("k", ["a"], client=FakeGenai({"a": text_response()}))
    assert model.dress(SELFIE, GARMENT, "Jacket") is None
    assert "made no image" in model.status()["last_error"]


class Unavailable(FakeImageModel):
    def dress(self, person, garment, item):
        from hiclose.agent.tryon import TryOnFailed
        raise TryOnFailed("unavailable", "429 quota")


def test_outage_is_not_blamed_on_the_customers_photo():
    shop = make_shop()
    shop.tryon = TryOnStudio(Unavailable(), PRODUCTS)
    chat = FakeModel([call("try_on", product_id="jacket-denim"), say("Sorry! Here's a photo instead.")])
    SalesAgent(chat, shop, ConversationStore()).handle("web:riya", "(sent a photo)", photo=SELFIE)
    error = results(chat)[0][1]["error"]
    assert "not their photo" in error and "clearer" not in error


def test_health_reports_try_on_status():
    shop, _ = studio_shop()
    web = WebChannel()
    service = HiCloseService(SalesAgent(FakeModel([]), shop, ConversationStore()), shop, FakePayments(), {"web": web})
    assert TestClient(create_app(service, web)).get("/health").json()["tryon"]["enabled"] is True


def test_when_switched_off_the_agent_never_sees_try_on():
    from hiclose.agent import ShopTools
    from hiclose.agent.conversation import Conversation
    from hiclose.agent.instructions import system_prompt
    off = make_shop()
    assert "try_on" not in [s.name for s in ShopTools(off, Conversation("x")).specs()]
    assert "try_on" not in system_prompt("Indigo Lane")
    on, _ = studio_shop()
    assert "try_on" in [s.name for s in ShopTools(on, Conversation("x")).specs()]
    assert "try_on" in system_prompt("Indigo Lane", tryon=True)
    web = WebChannel()
    service = HiCloseService(SalesAgent(FakeModel([]), off, ConversationStore()), off, FakePayments(), {"web": web})
    client = TestClient(create_app(service, web))
    assert client.get("/api/shop").json()["tryon"] is False
    assert client.get("/health").json()["tryon"] == {"enabled": False}

