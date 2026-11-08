"""One record per customer. Workers load it, run the agent, and save it back."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Conversation:
    customer_id: str
    history: list[Any] = field(default_factory=list)
    rounds: dict[str, int] = field(default_factory=dict)
    standing_offers: dict[str, int] = field(default_factory=dict)
    revealed_cents: set[int] = field(default_factory=set)
    handed_off: str | None = None
    owner_active: bool = False


class ConversationStore:
    def __init__(self):
        self._conversations: dict[str, Conversation] = {}

    def get(self, customer_id: str) -> Conversation:
        if customer_id not in self._conversations:
            self._conversations[customer_id] = Conversation(customer_id)
        return self._conversations[customer_id]
