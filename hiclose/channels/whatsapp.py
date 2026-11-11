"""WhatsApp through Twilio. Every incoming webhook is checked against Twilio's signature, so nobody
can post fake customer messages to us; replies go out through Twilio's Messages API."""

import base64
import hashlib
import hmac
import logging
import os
from urllib.parse import parse_qsl

import httpx

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
    ):
        self._sid = account_sid
        self._token = auth_token
        self._from = from_number if from_number.startswith("whatsapp:") else f"whatsapp:{from_number}"
        self._transport = transport

    @classmethod
    def from_env(cls) -> "WhatsAppChannel | None":
        sid, token = os.environ.get("TWILIO_ACCOUNT_SID", ""), os.environ.get("TWILIO_AUTH_TOKEN", "")
        if not sid or not token:
            return None
        return cls(sid, token, os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886"))

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

    async def send(self, message: OutgoingMessage) -> None:
        body = message.text
        if message.checkout_url:
            body += f"\n\nPay securely with PayPal: {message.checkout_url}"
        async with httpx.AsyncClient(transport=self._transport, timeout=20) as client:
            response = await client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{self._sid}/Messages.json",
                data={"From": self._from, "To": message.customer_id, "Body": body[:1600]},
                auth=(self._sid, self._token),
            )
            if response.is_error:
                logging.getLogger("hiclose").error(
                    "twilio send failed: status=%s from=%s to=%s body=%s",
                    response.status_code, self._from, message.customer_id, response.text[:500],
                )
            response.raise_for_status()
