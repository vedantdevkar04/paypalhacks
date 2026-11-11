"""Runs the agent for every channel: a bounded pool of workers, one customer at a time, and the
messages the shop sends on its own when PayPal confirms or a checkout is cancelled."""

import asyncio
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from hiclose.agent import SalesAgent, Shop
from hiclose.agent.money import fmt
from hiclose.channels.base import ChannelAdapter, IncomingMessage, OutgoingMessage
from hiclose.paypal.orders import OrderRecord

log = logging.getLogger("hiclose")


class Payments(Protocol):
    def get_order(self, order_id: str) -> OrderRecord | None: ...
    def confirm(self, order_id: str) -> OrderRecord: ...
    def cancel(self, order_id: str) -> OrderRecord | None: ...


@dataclass
class Stats:
    messages: int = 0
    replies: int = 0
    checkouts: int = 0
    paid: int = 0
    revenue_cents: dict[str, int] = field(default_factory=dict)
    handed_off: int = 0
    guard_rewrites: int = 0
    busy_replies: int = 0
    events: list[dict] = field(default_factory=list)

    def log(self, kind: str, **data) -> None:
        self.events.append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, **data})
        del self.events[:-500]


STAGE_ORDER = ["chatting", "negotiating", "checkout", "paid"]


@dataclass
class ChatSummary:
    customer: str
    channel: str
    stage: str = "chatting"
    messages: int = 0
    last_customer: str = ""
    last_shop: str = ""
    updated_at: str = ""
    negotiated: bool = False
    order_id: str | None = None
    owner_needed: bool = False


def masked(customer_id: str) -> str:
    channel, _, ident = customer_id.partition(":")
    if channel == "whatsapp":
        return f"WhatsApp ···{ident[-4:]}"
    return f"{channel.title()} {ident[:12]}"


class HiCloseService:
    def __init__(self, agent: SalesAgent, shop: Shop, payments: Payments, channels: dict[str, ChannelAdapter], workers: int | None = None):
        self.agent = agent
        self.shop = shop
        self.payments = payments
        self.channels = channels
        self.stats = Stats()
        self.enabled = True
        self._slots = asyncio.Semaphore(workers or int(os.environ.get("WORKER_COUNT", "20")))
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._seen: set[str] = set()
        self._order_owner: dict[str, tuple[str, str]] = {}
        self._notified: set[str] = set()
        self.chats: dict[str, ChatSummary] = {}

    def _chat(self, channel: str, customer: str) -> ChatSummary:
        chat = self.chats.setdefault(customer, ChatSummary(masked(customer), channel))
        chat.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return chat

    def _advance(self, chat: ChatSummary, stage: str) -> None:
        if STAGE_ORDER.index(stage) > STAGE_ORDER.index(chat.stage if chat.stage in STAGE_ORDER else "chatting"):
            chat.stage = stage

    async def handle(self, message: IncomingMessage, deliver: bool = True) -> OutgoingMessage | None:
        """Run one customer message. With deliver=False the reply is returned, not sent
        (the WhatsApp webhook hands it straight back to Twilio as TwiML)."""
        key = f"{message.channel}:{message.message_id}"
        if key in self._seen:
            return None
        self._seen.add(key)
        self.stats.messages += 1
        self.stats.log("message", customer=masked(message.customer_id), channel=message.channel)
        chat = self._chat(message.channel, message.customer_id)
        chat.messages += 1
        chat.last_customer = message.text[:200]
        if not self.enabled:
            return await self._send(OutgoingMessage(message.channel, message.customer_id, "Thanks for your message! We'll reply soon."), deliver)
        photo = await self._fetch_photo(message)
        async with self._slots, self._locks[message.customer_id]:
            reply = await asyncio.to_thread(self.agent.handle, message.customer_id, message.text, message.post_id, photo)
        if reply is None:
            return None
        self.stats.replies += 1
        self.stats.guard_rewrites += reply.guard_rewrites
        chat.last_shop = reply.text[:200]
        if "propose_price" in reply.tools_used:
            chat.negotiated = True
            self._advance(chat, "negotiating")
        if reply.handed_off:
            chat.owner_needed = True
            self.stats.handed_off += 1
            self.stats.log("handoff", customer=chat.customer)
        if reply.order_id:
            self._order_owner[reply.order_id] = (message.channel, message.customer_id)
            chat.order_id = reply.order_id
            self._advance(chat, "checkout")
            self.stats.checkouts += 1
            self.stats.log("checkout", customer=chat.customer, order=reply.order_id)
        return await self._send(OutgoingMessage(message.channel, message.customer_id, reply.text, reply.checkout_url, reply.image_url), deliver)

    async def payment_returned(self, order_id: str) -> OrderRecord:
        record = await asyncio.to_thread(self.payments.confirm, order_id)
        await self.notify_payment(record)
        return record

    async def notify_payment(self, record: OrderRecord | None) -> None:
        if record is None or record.status != "PAID" or record.order_id in self._notified:
            return
        self._notified.add(record.order_id)
        quote = record.quote
        self.stats.paid += 1
        self.stats.revenue_cents[quote.currency] = self.stats.revenue_cents.get(quote.currency, 0) + quote.price_cents
        self.stats.log("paid", order=record.order_id, cents=quote.price_cents, currency=quote.currency)
        owner = self._order_owner.get(record.order_id)
        if owner is None:
            return
        if owner[1] in self.chats:
            self._advance(self.chats[owner[1]], "paid")
        product = self.shop.catalog.get(quote.product_id)
        address = (record.ship_to or {}).get("address", {})
        place = ", ".join(p for p in (address.get("admin_area_2"), address.get("admin_area_1")) if p)
        size = f" ({quote.size})" if quote.size and quote.size != "ONE" else ""
        text = f"Paid ✅ {fmt(quote.price_cents, quote.currency)} for the {product.name}{size}"
        text += f". Order {record.order_id}" + (f", shipping to {place}." if place else ".") + " Thank you! 💛"
        await self._send(OutgoingMessage(owner[0], owner[1], text))

    def release_expired(self, now: datetime | None = None) -> list[str]:
        """Unpaid checkouts past their 15-minute hold give their item back to stock."""
        now = now or datetime.now(timezone.utc)
        list_orders = getattr(self.payments, "orders", None)
        released = []
        for record in (list_orders() if callable(list_orders) else []):
            if record.status != "CREATED" or record.quote.expires_at > now:
                continue
            self.payments.cancel(record.order_id)
            if record.quote.size:
                self.shop.catalog.release(record.quote.product_id, record.quote.size)
            self.stats.log("expired", order=record.order_id)
            released.append(record.order_id)
        return released

    async def checkout_cancelled(self, order_id: str) -> None:
        record = self.payments.cancel(order_id)
        if record is None or order_id in self._notified:
            return
        self._notified.add(order_id)
        if record.quote.size:
            self.shop.catalog.release(record.quote.product_id, record.quote.size)
        self.stats.log("cancelled", order=order_id)
        owner = self._order_owner.get(order_id)
        if owner:
            await self._send(OutgoingMessage(owner[0], owner[1], "No worries, I've cancelled that checkout. Just say if you'd like it again!"))

    def owner_view(self) -> dict:
        chats = list(self.chats.values())
        funnel = [
            ("Started a chat", len(chats)),
            ("Kept talking", sum(c.messages >= 2 for c in chats)),
            ("Negotiated", sum(c.negotiated for c in chats)),
            ("Got a checkout link", sum(c.order_id is not None for c in chats)),
            ("Paid", sum(c.stage == "paid" for c in chats)),
        ]
        orders = []
        list_orders = getattr(self.payments, "orders", None)
        for record in (list_orders() if callable(list_orders) else []):
            quote = record.quote
            address = (record.ship_to or {}).get("address", {})
            orders.append({
                "order_id": record.order_id,
                "product": self.shop.catalog.get(quote.product_id).name,
                "size": "" if quote.size == "ONE" else quote.size,
                "price": fmt(quote.price_cents, quote.currency),
                "list_price": fmt(self.shop.catalog.get(quote.product_id).list_cents, quote.currency),
                "status": record.status,
                "ships_to": ", ".join(p for p in (address.get("admin_area_2"), address.get("country_code")) if p),
            })
        rows = [
            {**{k: v for k, v in vars(c).items() if k != "negotiated"}, "stage": "owner needed" if c.owner_needed and c.stage != "paid" else c.stage}
            for c in sorted(chats, key=lambda c: c.updated_at, reverse=True)
        ]
        return {"funnel": [{"label": label, "count": n} for label, n in funnel], "chats": rows, "orders": orders}

    async def _fetch_photo(self, message: IncomingMessage):
        """The first photo attached to the message, if the channel can hand it over. Only used for try-ons."""
        if not message.media_urls or self.shop.tryon is None:
            return None
        fetch = getattr(self.channels.get(message.channel), "fetch_photo", None)
        if fetch is None:
            return None
        try:
            return await fetch(message.media_urls[0])
        except Exception:
            log.warning("couldn't fetch the photo on %s message %s", message.channel, message.message_id)
            return None

    async def _send(self, message: OutgoingMessage, deliver: bool = True) -> OutgoingMessage:
        channel = self.channels.get(message.channel)
        if deliver and channel is not None:
            await channel.send(message)
        return message
