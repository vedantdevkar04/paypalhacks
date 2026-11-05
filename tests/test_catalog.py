import json
from pathlib import Path

import pytest

from hiclose.rules import Catalog, CatalogError, Product

SEED = Path(__file__).parent.parent / "fixtures" / "catalog.json"


def jacket(**overrides) -> Product:
    fields = dict(
        product_id="jacket-denim", name="Denim Jacket", description="Boxy fit.",
        currency="USD", list_cents=8000, floor_cents=6800, sizes=("S", "M"), post_ids=("post-001",),
    )
    fields.update(overrides)
    return Product(**fields)


def test_seed_catalog_loads():
    catalog = Catalog.from_json(SEED)
    assert catalog.get("jacket-denim").list_cents == 8000


def test_post_maps_to_its_product():
    catalog = Catalog.from_json(SEED)
    assert catalog.for_post("post-001").product_id == "jacket-denim"
    assert catalog.for_post("post-004").product_id == "scarf-silk"


def test_unknown_or_missing_post_gives_no_product():
    catalog = Catalog.from_json(SEED)
    assert catalog.for_post("post-999") is None
    assert catalog.for_post(None) is None


def test_public_view_never_contains_the_floor():
    catalog = Catalog.from_json(SEED)
    view = catalog.public("jacket-denim")
    assert "floor_cents" not in view
    assert "6800" not in json.dumps(view)


def test_public_view_lists_only_sizes_in_stock():
    catalog = Catalog.from_json(SEED)
    assert catalog.public("jacket-denim")["sizes_in_stock"] == ["M", "L"]


def test_reserve_takes_one_from_stock_and_stops_at_zero():
    catalog = Catalog([jacket()], {"jacket-denim": {"M": 1}})
    catalog.reserve("jacket-denim", "M")
    assert catalog.in_stock("jacket-denim", "M") == 0
    with pytest.raises(CatalogError):
        catalog.reserve("jacket-denim", "M")


def test_release_puts_stock_back():
    catalog = Catalog([jacket()], {"jacket-denim": {"M": 1}})
    catalog.reserve("jacket-denim", "M")
    catalog.release("jacket-denim", "M")
    assert catalog.in_stock("jacket-denim", "M") == 1


def test_product_carries_a_valid_policy():
    assert jacket().policy.floor_cents == 6800


def test_floor_above_list_is_refused():
    with pytest.raises(ValueError):
        jacket(floor_cents=9000)


def test_bad_currency_is_refused():
    with pytest.raises(CatalogError):
        jacket(currency="usd")


def test_one_post_cannot_point_to_two_products():
    with pytest.raises(CatalogError):
        Catalog([jacket(), jacket(product_id="other")], {})


def test_negative_stock_is_refused():
    with pytest.raises(CatalogError):
        Catalog([jacket()], {"jacket-denim": {"M": -1}})


def test_unknown_product_is_refused():
    with pytest.raises(CatalogError):
        Catalog.from_json(SEED).get("made-up")
