"""What can reject a reply before it reaches the customer: any amount no tool returned."""

import re
from decimal import Decimal

_SYMBOL_AMOUNT = re.compile(r"[$€£]\s?(\d[\d,]*(?:\.\d{1,2})?)")
_CODE_AMOUNT = re.compile(r"(\d[\d,]*(?:\.\d{1,2})?)\s?(?:USD|EUR|GBP|dollars?|euros?|pounds?)\b", re.IGNORECASE)
_PERCENT = re.compile(r"\d+(?:\.\d+)?\s?%")


def _cents(raw: str) -> int:
    return int(Decimal(raw.replace(",", "")) * 100)


def _normal(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower().replace("\n", " ")).strip()


def leaked_instructions(text: str, system: str, min_chars: int = 30) -> list[str]:
    """Pieces of the system prompt that turn up word for word in a reply."""
    reply = " ".join(_normal(text).split())
    pieces = re.split(r"(?<=[.!?:;])\s+|\n+|\s+-\s+", system)
    leaked = []
    for piece in pieces:
        normal = " ".join(_normal(piece).split())
        if len(normal) >= min_chars and normal in reply:
            leaked.append(piece.strip())
    return leaked


def price_violations(text: str, allowed_cents: set[int]) -> list[str]:
    found = []
    for pattern in (_SYMBOL_AMOUNT, _CODE_AMOUNT):
        for match in pattern.finditer(text):
            if _cents(match.group(1)) not in allowed_cents:
                found.append(match.group(0))
    found.extend(m.group(0) for m in _PERCENT.finditer(text))
    return found
