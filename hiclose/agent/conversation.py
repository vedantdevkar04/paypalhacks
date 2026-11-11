"""One record per customer. Workers load it, run the agent, and save it back."""

import time
from dataclasses import dataclass, field
from typing import Any

from hiclose.agent.tryon import Photo

PHOTO_TTL_SECONDS = 30 * 60
MAX_TRYONS = 3


@dataclass
class Conversation:
    customer_id: str
    history: list[Any] = field(default_factory=list)
    rounds: dict[str, int] = field(default_factory=dict)
    standing_offers: dict[str, int] = field(default_factory=dict)
    revealed_cents: set[int] = field(default_factory=set)
    handed_off: str | None = None
    owner_active: bool = False
    # The customer's latest photo, for try-ons only. Memory only, dropped after 30 minutes.
    photo: Photo | None = field(default=None, repr=False)
    photo_at: float = 0.0
    tryons: int = 0

    def keep_photo(self, photo: Photo, now: float | None = None) -> None:
        self.photo, self.photo_at = photo, time.monotonic() if now is None else now

    def fresh_photo(self, now: float | None = None) -> Photo | None:
        now = time.monotonic() if now is None else now
        if self.photo is not None and now - self.photo_at > PHOTO_TTL_SECONDS:
            self.photo = None
        return self.photo


class ConversationStore:
    def __init__(self):
        self._conversations: dict[str, Conversation] = {}

    def get(self, customer_id: str) -> Conversation:
        if customer_id not in self._conversations:
            self._conversations[customer_id] = Conversation(customer_id)
        return self._conversations[customer_id]
