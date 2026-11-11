"""WhatsApp through Twilio. Every incoming webhook is checked against Twilio's signature, so nobody
can post fake customer messages to us. Replies go back as TwiML in the webhook response; Twilio
refuses free-form Messages API sends from our sender (error 21654), so the API is only a fallback."""

import base64
import hashlib
import hmac
import logging
import os
from urllib.parse import parse_qsl, urlparse
from xml.sax.saxutils import escape as xml_escape

import httpx

from hiclose.agent.tryon import Photo, acceptable
from hiclose.channels.base import IncomingMessage, OutgoingMessage


def twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


class WhatsAppChannel:
    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        transport: httpx.AsyncBaseTransport | None = None,
        public_base_url: str = "",
    ):
        self._sid = account_sid
        self._token = auth_token
        self._from = from_number if from_number.startswith("whatsapp:") else f"whatsapp:{from_number}"
        self._transport = transport
        # Twilio downloads photos itself, so they need our public address (Render sets RENDER_EXTERNAL_URL).
        self._base = public_base_url.rstrip("/")

    @classmethod
    def from_env(cls) -> "WhatsAppChannel | None":
        sid, token = os.environ.get("TWILIO_ACCOUNT_SID", ""), os.environ.get("TWILIO_AUTH_TOKEN", "")
        if not sid or not token:
            return None
        base = os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or ""
        return cls(sid, token, os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886"), public_base_url=base)

    async def fetch_photo(self, url: str) -> Photo | None:
        """Download a photo the customer attached. Only Twilio's own media host, with our credentials:
        this is the platform's attachment, not a link the customer typed (hard rule 7)."""
        parts = urlparse(url)
        if parts.scheme != "https" or parts.hostname != "api.twilio.com":
            return None
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=10, follow_redirects=True) as client:
                response = await client.get(url, auth=(self._sid, self._token))
        except httpx.HTTPError:
            logging.getLogger("hiclose").warning("couldn't download whatsapp photo")
            return None
        if response.is_error:
            return None
        return acceptable(response.content, response.headers.get("content-type", ""))

    def media_url(self, message: OutgoingMessage) -> str | None:
        """The photo as an https URL Twilio can fetch, or None (no photo, or no public address)."""
        if not message.image_url:
            return None
        if message.image_url.startswith("https://"):
            return message.image_url
        if message.image_url.startswith("/") and self._base.startswith("https://"):
            return self._base + message.image_url
        return None

    @staticmethod
    def form(body: bytes) -> dict[str, str]:
        return dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))

    def verify(self, url: str, params: dict[str, str], signature: str) -> bool:
        if not signature:
            return False
        return hmac.compare_digest(twilio_signature(self._token, url, params), signature)

    def parse(self, payload: dict) -> list[IncomingMessage]:
        sender = payload.get("From", "")
        text = payload.get("Body", "").strip()[:1000]
        if not sender.startswith("whatsapp:") or not payload.get("MessageSid"):
            return []
        media = tuple(payload[f"MediaUrl{i}"] for i in range(int(payload.get("NumMedia", "0") or 0)) if f"MediaUrl{i}" in payload)
        if not text and not media:
            return []
        return [IncomingMessage(
            channel="whatsapp",
            kind="dm",
            customer_id=sender,
            message_id=payload["MessageSid"],
            text=text or "(sent a photo)",
            media_urls=media,
        )]

    @staticmethod
    def render(message: OutgoingMessage) -> str:
        body = message.text
        if message.checkout_url:
            body += f"\n\nPay securely with PayPal: {message.checkout_url}"
        return body[:1600]

    def twiml(self, messages: "list[OutgoingMessage] | OutgoingMessage | None") -> str:
        """Replies as TwiML, returned straight to Twilio's webhook call (no API send).
        Each reply becomes its own <Message>, so the customer gets them as separate bubbles;
        a product photo rides along as <Media>."""
        if messages is None:
            messages = []
        elif isinstance(messages, OutgoingMessage):
            messages = [messages]
        parts = []
        for message in messages:
            media = self.media_url(message)
            inner = f"<Body>{xml_escape(self.render(message))}</Body>"
            if media:
                inner += f"<Media>{xml_escape(media)}</Media>"
            parts.append(f"<Message>{inner}</Message>")
        return f'<?xml version="1.0" encoding="UTF-8"?><Response>{"".join(parts)}</Response>'

    async def send(self, message: OutgoingMessage) -> None:
        data = {"From": self._from, "To": message.customer_id, "Body": self.render(message)}
        media = self.media_url(message)
        if media:
            data["MediaUrl"] = media
        async with httpx.AsyncClient(transport=self._transport, timeout=20) as client:
            response = await client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{self._sid}/Messages.json",
                data=data,
                auth=(self._sid, self._token),
            )
            if response.is_error:
                logging.getLogger("hiclose").error(
                    "twilio send failed: status=%s from=%s to=%s body=%s",
                    response.status_code, self._from, message.customer_id, response.text[:500],
                )
            response.raise_for_status()
