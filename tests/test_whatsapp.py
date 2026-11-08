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


def build(script, outbox):
    shop = make_shop()
    whatsapp = channel(outbox)
    web = WebChannel()
    service = HiCloseService(SalesAgent(FakeModel(script), shop, ConversationStore()), shop, FakePayments(), {"web": web, "whatsapp": whatsapp})
    return TestClient(create_app(service, web, whatsapp=whatsapp))


def post(client, params, signature):
    return client.post("/webhooks/whatsapp", content=urlencode(params),
                       headers={"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": signature})


def test_signed_webhook_gets_an_agent_reply_on_whatsapp():
    outbox = Outbox()
    client = build([say("Hey! The denim jacket is $80 ✨")], outbox)
    params = incoming("hi")
    response = post(client, params, twilio_signature(TOKEN, URL, params))
    assert response.status_code == 200 and "<Response>" in response.text
    deadline = time.time() + 3
    while not outbox.sent and time.time() < deadline:
        time.sleep(0.05)
    assert outbox.sent[0]["Body"] == "Hey! The denim jacket is $80 ✨"


def test_forged_webhook_is_refused():
    client = build([], Outbox())
    assert post(client, incoming("hi"), "forged").status_code == 403


def test_whatsapp_route_needs_configuring():
    shop = make_shop()
    web = WebChannel()
    service = HiCloseService(SalesAgent(FakeModel([]), shop, ConversationStore()), shop, FakePayments(), {"web": web})
    client = TestClient(create_app(service, web))
    assert client.post("/webhooks/whatsapp", content="Body=hi").status_code == 503
