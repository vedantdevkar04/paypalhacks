"""The web chat: the channel anyone can try, no account needed. Messages the shop sends later
(like "Paid ✅") wait in a per-customer transcript that the page polls."""

import secrets

from hiclose.channels.base import IncomingMessage, OutgoingMessage


class WebChannel:
    def __init__(self):
        self._transcripts: dict[str, list[dict]] = {}

    def parse(self, payload: dict) -> list[IncomingMessage]:
        customer = str(payload.get("customer_id", "")).strip()[:64]
        text = str(payload.get("text", "")).strip()[:1000]
        if not customer or not text:
            return []
        post_id = payload.get("post_id") or None
        message = IncomingMessage(
            channel="web",
            kind="comment" if payload.get("kind") == "comment" else "dm",
            customer_id=f"web:{customer}",
            message_id=str(payload.get("message_id") or secrets.token_hex(8)),
            text=text,
            post_id=str(post_id) if post_id else None,
        )
        self._record(message.customer_id, "customer", text)
        return [message]

    async def send(self, message: OutgoingMessage) -> None:
        self._record(message.customer_id, "shop", message.text, message.checkout_url)

    def transcript(self, customer_id: str, after: int = 0) -> list[dict]:
        return self._transcripts.get(customer_id, [])[after:]

    def _record(self, customer_id: str, sender: str, text: str, checkout_url: str | None = None) -> None:
        entries = self._transcripts.setdefault(customer_id, [])
        entries.append({"n": len(entries) + 1, "from": sender, "text": text, "checkout_url": checkout_url})
