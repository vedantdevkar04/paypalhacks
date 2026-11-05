"""How far the shop will move on price. Plain code: the model never sees the floor."""

from dataclasses import dataclass
from typing import Literal

Action = Literal["accept", "counter", "final", "reject"]


@dataclass(frozen=True)
class Policy:
    list_cents: int
    floor_cents: int
    max_rounds: int = 3

    def __post_init__(self) -> None:
        if self.floor_cents <= 0 or self.floor_cents > self.list_cents:
            raise ValueError("floor must be above zero and no higher than the list price")
        if self.max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")


@dataclass(frozen=True)
class Decision:
    action: Action
    price_cents: int


def decide(policy: Policy, asked_cents: int, round_no: int) -> Decision:
    """Answer a buyer who asked for `asked_cents` in negotiation round `round_no` (1, 2, 3...).

    - asked at or above the list price: accept at the list price (never charge more than list)
    - asked between the floor and the list price: accept at what they asked
    - asked below the floor, rounds left: counter, stepping down evenly from list toward floor
      (step = (list - floor) // max_rounds, counter = list - step * round_no, rounded up to a whole
      unit like $62 rather than $61.67, never below floor)
    - asked below the floor, no rounds left (round_no >= max_rounds): final offer at the floor
    - asked zero or less: reject, price 0
    """
    if asked_cents <= 0:
        return Decision("reject", 0)
    if asked_cents >= policy.list_cents:
        return Decision("accept", policy.list_cents)
    if asked_cents >= policy.floor_cents:
        return Decision("accept", asked_cents)
    if round_no >= policy.max_rounds:
        return Decision("final", policy.floor_cents)
    step = (policy.list_cents - policy.floor_cents) // policy.max_rounds
    counter = policy.list_cents - step * round_no
    counter = min(policy.list_cents, -(-counter // 100) * 100)
    return Decision("counter", max(policy.floor_cents, counter))
