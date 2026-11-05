import pytest

from hiclose.agent.guardrail import price_violations
from hiclose.agent.money import fmt, parse_amount


def test_allowed_amounts_pass():
    assert price_violations("It's $80, or $76 for you", {8000, 7600}) == []


def test_amount_no_tool_returned_is_flagged():
    assert price_violations("ok $50 it is", {8000}) == ["$50"]


def test_euro_and_pound_and_words_are_checked():
    assert price_violations("€65 or £54", {6500, 5400}) == []
    assert price_violations("just 70 dollars", {8000}) == ["70 dollars"]


def test_thousands_and_cents_are_parsed():
    assert price_violations("$1,200.50", {120050}) == []


def test_percentages_are_always_flagged():
    assert price_violations("20% off today", {8000}) == ["20%"]


def test_plain_numbers_like_sizes_are_not_prices():
    assert price_violations("We have size 10 and 2 left", {8000}) == []


@pytest.mark.parametrize("cents,currency,expected", [(8000, "USD", "$80"), (7650, "USD", "$76.50"), (6500, "EUR", "€65"), (100, "JPY", "1 JPY")])
def test_fmt(cents, currency, expected):
    assert fmt(cents, currency) == expected


@pytest.mark.parametrize("text,expected", [("60", 6000), ("$60", 6000), ("60.5", 6050), ("1,200", 120000), ("60 dollars", 6000), ("lots", None), ("", None), ("nan", None)])
def test_parse_amount(text, expected):
    assert parse_amount(text) == expected
