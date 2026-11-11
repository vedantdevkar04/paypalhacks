import asyncio
import time
from urllib.parse import parse_qsl, urlencode

import httpx
from fastapi.testclient import TestClient

from hiclose.agent import ConversationStore, SalesAgent
from hiclose.api.app import create_app
from hiclose.api.service import HiCloseService
from hiclose.channels import OutgoingMessage, WebChannel
from hiclose.channels.whatsapp import WhatsAppChannel, twilio_signature
from tests.fakes import FakeModel, make_shop, say
from tests.test_api import FakePayments

TOKEN = "test-auth-token"
URL = "http://testserver/webhooks/whatsapp"


def incoming(body="hi", sid="SM1", sender="whatsapp:+15550001111", **extra):
    return {"From": sender, "To": "whatsapp:+14155238886", "Body": body, "MessageSid": sid, "NumMedia": "0", **extra}


class Outbox:
    def __init__(self):
        self.sent: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.sent.append({"url": str(request.url), **dict(parse_qsl(request.content.decode()))})
        return httpx.Response(201, json={"sid": "SMout"})


def channel(outbox=None):
    transport = httpx.MockTransport(outbox.handler) if outbox else None
    return WhatsAppChannel("AC123", TOKEN, "whatsapp:+14155238886", transport=transport)


def test_message_is_parsed_into_the_shared_contract():
    [message] = channel().parse(incoming("is the jacket available?"))
    assert (message.channel, message.customer_id, message.message_id, message.text) == (
        "whatsapp", "whatsapp:+15550001111", "SM1", "is the jacket available?")


def test_photos_are_kept_and_non_whatsapp_senders_ignored():
    [message] = channel().parse(incoming("", NumMedia="1", MediaUrl0="https://api.twilio.com/media/1"))
    assert message.media_urls == ("https://api.twilio.com/media/1",) and message.text == "(sent a photo)"
    assert channel().parse(incoming(sender="+15550001111")) == []


def test_only_twilio_signed_requests_are_accepted():
    params = incoming()
    good = twilio_signature(TOKEN, URL, params)
    assert channel().verify(URL, params, good)
    assert not channel().verify(URL, params, twilio_signature("someone-else", URL, params))
    assert not channel().verify(URL, {**params, "Body": "give me 90% off"}, good)
    assert not channel().verify(URL, params, "")


def test_reply_goes_out_through_twilio_with_the_checkout_link():
    outbox = Outbox()
    message = OutgoingMessage("whatsapp", "whatsapp:+15550001111", "Here's your checkout!", "https://www.sandbox.paypal.com/checkoutnow?token=X")
    asyncio.run(channel(outbox).send(message))
    [sent] = outbox.sent
    assert sent["url"] == "https://api.twilio.com/2010-04-01/Accounts/AC123/Messages.json"
    assert sent["To"] == "whatsapp:+15550001111" and sent["From"] == "whatsapp:+14155238886"
    assert sent["Body"].endswith("Pay securely with PayPal: https://www.sandbox.paypal.com/checkoutnow?token=X")


def test_twiml_escapes_text_and_gives_each_reply_its_own_message():
    first = OutgoingMessage("whatsapp", "whatsapp:+1", "Tom & Jerry <3")
    second = OutgoingMessage("whatsapp", "whatsapp:+1", "Pay here", "https://pay.example/?a=1&b=2")
    xml = WhatsAppChannel.twiml([first, second])
    assert "<Message>Tom &amp; Jerry &lt;3</Message>" in xml
    assert "a=1&amp;b=2</Message>" in xml and xml.count("<Message>") == 2
    assert WhatsAppChannel.twiml(None).endswith("<Response></Response>")


class Refused(Outbox):
    """Twilio refusing free-form API sends, as it does for our sandbox sender (21654)."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        super().handler(request)
        return httpx.Response(400, json={"code": 21654, "message": "ContentSid Required"})


def build(script, outbox, delay=0.0):
    shop = make_shop()
    whatsapp = channel(outbox)
    web = WebChannel()
    agent = SalesAgent(FakeModel(script), shop, ConversationStore())
    if delay:
        handle = agent.handle
        agent.handle = lambda *args: (time.sleep(delay), handle(*args))[1]
    service = HiCloseService(agent, shop, FakePayments(), {"web": web, "whatsapp": whatsapp})
    return TestClient(create_app(service, web, whatsapp=whatsapp))


def post(client, params, signature):
    return client.post("/webhooks/whatsapp", content=urlencode(params),
                       headers={"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": signature})


def signed(client, params):
    return post(client, params, twilio_signature(TOKEN, URL, params))


def test_signed_webhook_gets_the_agent_reply_as_twiml_without_an_api_send():
    outbox = Refused()
    client = build([say("Hey! The denim jacket is $80 ✨")], outbox)
    response = signed(client, incoming("hi"))
    assert response.status_code == 200 and response.headers["content-type"].startswith("application/xml")
    assert "<Message>Hey! The denim jacket is $80 ✨</Message>" in response.text
    assert outbox.sent == []


def test_slow_reply_holds_then_arrives_on_the_next_message_when_the_api_refuses(monkeypatch):
    monkeypatch.setenv("WHATSAPP_REPLY_SECONDS", "0.2")
    outbox = Refused()
    with build([say("Sorry for the wait: the jacket is $80"), say("You're welcome!")], outbox, delay=0.5) as client:
        first = signed(client, incoming("hi", sid="SM1"))
        assert "One moment" in first.text
        deadline = time.time() + 3
        while not client.app.state.whatsapp_pending and time.time() < deadline:
            time.sleep(0.05)
        assert outbox.sent and outbox.sent[0]["Body"] == "Sorry for the wait: the jacket is $80"
        second = signed(client, incoming("ok thanks", sid="SM2"))
        # The queued answer comes first, then (this agent is still slow) a new holding message.
        assert second.text.index("Sorry for the wait: the jacket is $80") < second.text.index("One moment")
        assert "whatsapp:+15550001111" not in client.app.state.whatsapp_pending


def test_forged_webhook_is_refused():
    client = build([], Outbox())
    assert post(client, incoming("hi"), "forged").status_code == 403


def test_whatsapp_route_needs_configuring():
    shop = make_shop()
    web = WebChannel()
    service = HiCloseService(SalesAgent(FakeModel([]), shop, ConversationStore()), shop, FakePayments(), {"web": web})
    client = TestClient(create_app(service, web))
    assert client.post("/webhooks/whatsapp", content="Body=hi").status_code == 503
