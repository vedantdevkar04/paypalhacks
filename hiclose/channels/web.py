"""The web chat: the channel anyone can try, no account needed. Messages the shop sends later
(like "Paid ✅") wait in a per-customer transcript that the page polls."""

import base64
import binascii
import secrets

from hiclose.agent.tryon import Photo, acceptable
from hiclose.channels.base import IncomingMessage, OutgoingMessage

PHOTO_TEXT = "(sent a photo)"


def _decode_data_url(value: str) -> Photo | None:
    """A photo the page uploaded, as a data: URL. Bytes only; we never fetch anything."""
    if not isinstance(value, str) or not value.startswith("data:") or "," not in value:
        return None
    header, _, payload = value.partition(",")
    if not header.endswith(";base64"):
        return None
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None
    return acceptable(data, header[5:-7])


class WebChannel:
    def __init__(self):
        self._transcripts: dict[str, list[dict]] = {}
        self._uploads: dict[str, Photo] = {}

    def parse(self, payload: dict) -> list[IncomingMessage]:
        customer = str(payload.get("customer_id", "")).strip()[:64]
        text = str(payload.get("text", "")).strip()[:1000]
        photo = _decode_data_url(payload.get("photo")) if payload.get("photo") else None
        if not customer or not (text or photo):
            return []
        media: tuple[str, ...] = ()
        if photo is not None:
            token = secrets.token_urlsafe(12)
            self._uploads[token] = photo
            media = (f"upload:{token}",)
        post_id = payload.get("post_id") or None
        message = IncomingMessage(
            channel="web",
            kind="comment" if payload.get("kind") == "comment" else "dm",
            customer_id=f"web:{customer}",
            message_id=str(payload.get("message_id") or secrets.token_hex(8)),
            text=text or PHOTO_TEXT,
            post_id=str(post_id) if post_id else None,
            media_urls=media,
        )
        self._record(message.customer_id, "customer", text or PHOTO_TEXT)
        return [message]

    async def fetch_photo(self, url: str) -> Photo | None:
        """Hand over an uploaded photo once; it isn't kept here afterwards."""
        if not url.startswith("upload:"):
            return None
        return self._uploads.pop(url[len("upload:"):], None)

    async def send(self, message: OutgoingMessage) -> None:
        self._record(message.customer_id, "shop", message.text, message.checkout_url, message.image_url)

    def transcript(self, customer_id: str, after: int = 0) -> list[dict]:
        return self._transcripts.get(customer_id, [])[after:]

    def _record(self, customer_id: str, sender: str, text: str, checkout_url: str | None = None, image_url: str | None = None) -> None:
        entries = self._transcripts.setdefault(customer_id, [])
        entries.append({"n": len(entries) + 1, "from": sender, "text": text, "checkout_url": checkout_url, "image_url": image_url})
