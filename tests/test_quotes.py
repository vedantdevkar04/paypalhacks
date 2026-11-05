from datetime import datetime, timedelta, timezone

import pytest

from hiclose.rules import QuoteBook, QuoteError

START = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now


def test_quote_can_be_read_back_before_it_expires():
    book = QuoteBook(clock=FakeClock())
    quote = book.create("jacket-denim", 7000)
    assert book.get(quote.quote_id).price_cents == 7000


def test_quote_expires():
    clock = FakeClock()
    book = QuoteBook(ttl=timedelta(minutes=15), clock=clock)
    quote = book.create("jacket-denim", 7000)
    clock.now = START + timedelta(minutes=15)
    with pytest.raises(QuoteError):
        book.get(quote.quote_id)


def test_unknown_quote_is_refused():
    with pytest.raises(QuoteError):
        QuoteBook().get("Q-made-up-by-the-model")


def test_quote_price_must_be_positive():
    with pytest.raises(QuoteError):
        QuoteBook().create("jacket-denim", 0)


def test_quote_ids_are_unique():
    book = QuoteBook()
    ids = {book.create("jacket-denim", 7000).quote_id for _ in range(200)}
    assert len(ids) == 200
