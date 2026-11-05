"""What can reject a reply before it reaches the customer: any amount no tool returned."""

import re
from decimal import Decimal

_SYMBOL_AMOUNT = re.compile(r"[$€£]\s?(\d[\d,]*(?:\.\d{1,2})?)")
_CODE_AMOUNT = re.compile(r"(\d[\d,]*(?:\.\d{1,2})?)\s?(?:USD|EUR|GBP|dollars?|euros?|pounds?)\b", re.IGNORECASE)
_PERCENT = re.compile(r"\d+(?:\.\d+)?\s?%")


def _cents(raw: str) -> int:
    return int(Decimal(raw.replace(",", "")) * 100)


def price_violations(text: str, allowed_cents: set[int]) -> list[str]:
    found = []
    for pattern in (_SYMBOL_AMOUNT, _CODE_AMOUNT):
        for match in pattern.finditer(text):
            if _cents(match.group(1)) not in allowed_cents:
                found.append(match.group(0))
    found.extend(m.group(0) for m in _PERCENT.finditer(text))
    return found
