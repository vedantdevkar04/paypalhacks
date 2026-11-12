import base64
import json

import httpx
from fastapi.testclient import TestClient

from hiclose.agent import ConversationStore, SalesAgent
from hiclose.agent.tryon import Photo
from hiclose.api.app import create_app
from hiclose.api.service import HiCloseService
from hiclose.channels import WebChannel
from hiclose.market import Channel3Client, Channel3Error, PhotoMatcher, market_report
from tests.fakes import FakeModel, call, make_shop, say
from tests.test_api import FakePayments

SNAP = Photo(b"\xff\xd8screenshot", "image/jpeg")
SNAP_URL = "data:image/jpeg;base64," + base64.b64encode(SNAP.data).decode()


def product(title, price, category="Apparel > Outerwear > Jackets", brand="Acme", domain="shop.example", currency="USD"):
    return {"id": title, "title": title, "brands": [{"id": brand, "name": brand}],
            "category": {"slug": "x", "title": category.split(">")[-1].strip(), "path": category},
            "images": [{"url": "https://cdn.trychannel3.com/x.jpg"}],
            "offers": [{"url": f"https://{domain}/p", "domain": domain,
                        "price": {"price": price, "compare_at_price": None, "currency": currency}},
                       {"url": "https://pricier.example/p", "domain": "pricier.example",
                        "price": {"price": price + 30, "currency": currency}}]}


class FakeChannel3:
    """Records requests; answers search by query word and image-search with `image_results`."""

    def __init__(self, by_word=None, image_results=None, status=200):
        self.by_word = by_word or {}
        self.image_results = image_results or []
        self.status = status
        self.requests: list[tuple[str, dict, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append((request.url.path, body, dict(request.headers)))
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "nope"})
        if request.url.path.endswith("/image-search"):
            return httpx.Response(200, json={"products": self.image_results, "next_page_token": None})
        word = next((w for w in self.by_word if w in body["query"].lower()), None)
        return httpx.Response(200, json={"products": self.by_word.get(word, []), "next_page_token": None})

    def client(self, **kwargs) -> Channel3Client:
        return Channel3Client("test-key", transport=httpx.MockTransport(self.handler), **kwargs)


JACKETS = [product("Trucker Jacket", p) for p in (58, 64, 70, 79, 88, 98)]


def test_search_sends_the_key_and_keeps_the_cheapest_offer():
    fake = FakeChannel3({"jacket": JACKETS})
    [first, *_] = fake.client().search("Washed Denim Jacket")
    path, body, headers = fake.requests[0]
    assert path == "/v1/search" and headers["x-api-key"] == "test-key" and body["query"] == "Washed Denim Jacket"
    assert first.price_cents == 5800 and first.domain == "shop.example"  # not the $88 pricier offer


def test_searches_are_cached_to_save_quota():
    now = [0.0]
    fake = FakeChannel3({"jacket": JACKETS})
    client = fake.client(cache_seconds=60, clock=lambda: now[0])
    client.search("Washed Denim Jacket")
    client.search("washed denim jacket")
    assert len(fake.requests) == 1
    now[0] = 61
    client.search("Washed Denim Jacket")
    assert len(fake.requests) == 2


def test_errors_surface_as_channel3_error():
    fake = FakeChannel3(status=401)
    try:
        fake.client().search("anything")
        raise AssertionError("expected Channel3Error")
    except Channel3Error as error:
        assert "401" in str(error)


def test_market_report_places_our_price_against_similar_items():
    fake = FakeChannel3({"jacket": JACKETS, "tote": [product("Canvas Tote", p, "Bags > Totes") for p in (12, 15, 18, 22)]})
    rows = {r["product_id"]: r for r in market_report(fake.client(), make_shop().catalog)}
    jacket = rows["jacket-denim"]
    # the middle half of $58-$98: the cheapest and priciest are trimmed
    assert (jacket["our_price"], jacket["low"], jacket["median"], jacket["high"]) == ("$80", "$64", "$74.50", "$88")
    assert jacket["status"] == "in line" and jacket["compared"] == 6 and jacket["trimmed"] == 2
    assert rows["tote-canvas"]["status"] == "above market"     # our $32 vs $12-$22
    assert rows["scarf-silk"]["status"] == "no matches"         # nothing came back
    assert jacket["examples"][0] == {"title": "Trucker Jacket", "brand": "Acme", "store": "shop.example", "price": "$79"}  # nearest our $80


def test_luxury_outliers_dont_make_us_look_cheap():
    prices = [55, 60, 70, 75, 80, 85, 95, 990]  # one $990 designer jacket
    fake = FakeChannel3({"jacket": [product(f"Jacket {p}", p) for p in prices]})
    jacket = {r["product_id"]: r for r in market_report(fake.client(), make_shop().catalog)}["jacket-denim"]
    assert jacket["high"] == "$85" and jacket["status"] == "in line"
    assert all(e["price"] != "$990" for e in jacket["examples"])


def test_market_report_ignores_other_currencies():
    fake = FakeChannel3({"jacket": [product("Jacket", 50, currency="GBP")]})
    rows = {r["product_id"]: r for r in market_report(fake.client(), make_shop().catalog)}
    assert rows["jacket-denim"]["status"] == "no matches"


def test_photo_matcher_maps_what_it_sees_to_our_products():
    fake = FakeChannel3(image_results=[product("Levi's Trucker Denim Jacket", 89), product("Denim Jean Jacket", 60)])
    found = PhotoMatcher(fake.client(), make_shop().catalog).match(SNAP)
    path, body, _ = fake.requests[0]
    assert path == "/v1/image-search" and base64.b64decode(body["base64_image"]) == SNAP.data
    assert found == {"looks_like": "Jackets", "our_matches": ["jacket-denim"]}


def test_photo_of_something_we_dont_sell_matches_nothing():
    fake = FakeChannel3(image_results=[product("Running Shoe", 120, "Shoes > Sneakers")])
    assert PhotoMatcher(fake.client(), make_shop().catalog).match(SNAP)["our_matches"] == []


def matching_shop(results):
    shop = make_shop()
    fake = FakeChannel3(image_results=results)
    shop.photo_match = PhotoMatcher(fake.client(), shop.catalog)
    return shop


def tool_result(model):
    return [r for turn in model.seen[-1] if isinstance(turn, dict) and turn.get("role") == "tool" for r in turn["results"]][0][1]


def test_agent_gets_our_products_and_never_other_stores_or_prices():
    shop = matching_shop([product("Levi's Trucker Denim Jacket", 89, domain="levi.example")])
    model = FakeModel([call("identify_photo"), say("That's a denim jacket! Ours is $80 in M or L.")])
    reply = SalesAgent(model, shop, ConversationStore()).handle("web:riya", "do you have this?", photo=SNAP)
    result = tool_result(model)
    assert result["looks_like"] == "Jackets" and result["our_closest"][0]["product_id"] == "jacket-denim"
    assert "levi" not in json.dumps(result).lower() and "$89" not in json.dumps(result) and "89" not in json.dumps(result)
    assert reply.text.startswith("That's a denim jacket")
    assert "identify_photo" in model.seen[0][0]["text"]  # the photo note tells the agent about the tool


def test_identify_photo_without_a_photo_asks_for_one():
    model = FakeModel([call("identify_photo"), say("Send me a photo!")])
    SalesAgent(model, matching_shop([]), ConversationStore()).handle("web:riya", "do you have the one I saw?")
    assert tool_result(model)["needs_photo"] is True


def test_identify_photo_is_hidden_without_channel3():
    from hiclose.agent import ShopTools
    from hiclose.agent.conversation import Conversation
    from hiclose.agent.instructions import system_prompt
    assert "identify_photo" not in [s.name for s in ShopTools(make_shop(), Conversation("x")).specs()]
    assert "identify_photo" not in system_prompt("Indigo Lane")
    assert "identify_photo" in system_prompt("Indigo Lane", photo_match=True)


def build(market_client=None, shop=None):
    shop = shop or make_shop()
    payments = FakePayments()
    shop.checkout = payments
    web = WebChannel()
    model = FakeModel([call("identify_photo"), say("Looks like our denim jacket!")])
    service = HiCloseService(SalesAgent(model, shop, ConversationStore()), shop, payments, {"web": web})
    return TestClient(create_app(service, web, market_client=market_client)), model


def test_market_endpoint_off_without_a_key_and_on_with_one():
    client, _ = build()
    assert client.get("/api/market").json() == {"enabled": False, "rows": []}
    client, _ = build(FakeChannel3({"jacket": JACKETS}).client())
    data = client.get("/api/market").json()
    assert data["enabled"] and any(r["product_id"] == "jacket-denim" and r["compared"] == 6 for r in data["rows"])
    assert "floor" not in json.dumps(data) and "6800" not in json.dumps(data)  # never leaks our lowest price


def test_web_photo_reaches_channel3_end_to_end():
    shop = matching_shop([product("Denim Jacket", 70)])
    client, model = build(shop=shop)
    shop_info = client.get("/api/shop").json()
    assert shop_info["photo_match"] is True and shop_info["photos"] is True and shop_info["tryon"] is False
    assert client.post("/api/chat", json={"customer_id": "riya", "text": "", "photo": SNAP_URL}).status_code == 200
    assert tool_result(model)["our_closest"][0]["product_id"] == "jacket-denim"
