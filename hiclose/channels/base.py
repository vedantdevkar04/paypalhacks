"""The shape every channel turns its messages into. The agent never knows which platform it's on."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Protocol

Channel = Literal["web", "whatsapp", "instagram"]
Kind = Literal["dm", "comment"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class IncomingMessage:
    channel: Channel
    kind: Kind
    customer_id: str
    message_id: str
    text: str
    received_at: datetime = field(default_factory=utcnow)
    post_id: str | None = None
    media_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutgoingMessage:
    channel: Channel
    customer_id: str
    text: str
    checkout_url: str | None = None
    image_url: str | None = None  # a product photo from our own catalog, as a site path like /products/x.jpg


class ChannelAdapter(Protocol):
    def parse(self, payload: dict) -> list[IncomingMessage]: ...
    async def send(self, message: OutgoingMessage) -> None: ...
