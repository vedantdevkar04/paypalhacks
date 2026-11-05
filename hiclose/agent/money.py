from decimal import Decimal, InvalidOperation

SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£"}


def fmt(cents: int, currency: str) -> str:
    amount = f"{cents // 100}" if cents % 100 == 0 else f"{cents / 100:.2f}"
    symbol = SYMBOLS.get(currency)
    return f"{symbol}{amount}" if symbol else f"{amount} {currency}"


def parse_amount(text: str) -> int | None:
    cleaned = str(text).strip().lstrip("$€£").replace(",", "").split()[0] if str(text).strip() else ""
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, IndexError):
        return None
    if not value.is_finite():
        return None
    return int((value * 100).to_integral_value())
