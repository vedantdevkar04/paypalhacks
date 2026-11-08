"""What the agent can do. Every number comes from the rules engine; the model only picks a tool."""

from dataclasses import dataclass
from typing import Protocol

from hiclose.agent.conversation import Conversation
from hiclose.agent.llm import ToolCall, ToolSpec
from hiclose.agent.money import fmt, parse_amount
from hiclose.rules import Catalog, CatalogError, Quote, QuoteBook, decide


@dataclass(frozen=True)
class Checkout:
    order_id: str
    approve_url: str


class CheckoutProvider(Protocol):
    def create_checkout(self, quote: Quote) -> Checkout: ...


class FakeCheckout:
    def __init__(self):
        self.created: list[Quote] = []

    def create_checkout(self, quote: Quote) -> Checkout:
        self.created.append(quote)
        return Checkout(order_id=f"ORDER-{quote.quote_id}", approve_url=f"https://sandbox.paypal.test/checkout/{quote.quote_id}")


@dataclass
class Shop:
    brand: str
    catalog: Catalog
    quotes: QuoteBook
    checkout: CheckoutProvider


SPECS = [
    ToolSpec("list_products", "List what the shop sells, with prices and sizes in stock.", {"type": "object", "properties": {}}),
    ToolSpec(
        "get_product",
        "Get one product's details, price and sizes in stock.",
        {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"]},
    ),
    ToolSpec(
        "propose_price",
        "Use whenever the customer asks for a lower price or names a price. Returns the shop's answer; "
        "say exactly that price, never another.",
        {
            "type": "object",
            "properties": {
                "product_id": {"type": "string"},
                "offered_amount": {"type": "string", "description": "The amount the customer named, e.g. '60'"},
            },
            "required": ["product_id", "offered_amount"],
        },
    ),
    ToolSpec(
        "checkout",
        "Use when the customer clearly wants to buy and has chosen a size. Creates a secure PayPal checkout "
        "at the agreed price and holds the item for 15 minutes. The link is attached to your reply automatically.",
        {
            "type": "object",
            "properties": {"product_id": {"type": "string"}, "size": {"type": "string"}},
            "required": ["product_id", "size"],
        },
    ),
    ToolSpec(
        "hand_to_owner",
        "Pass the chat to the shop owner: complaints, returns, custom orders, collaborations, anything unusual.",
        {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    ),
]


class ShopTools:
    def __init__(self, shop: Shop, conversation: Conversation):
        self._shop = shop
        self._convo = conversation
        self.checkout_url: str | None = None
        self.order_id: str | None = None

    def specs(self) -> list[ToolSpec]:
        return SPECS

    def allowed_cents(self) -> set[int]:
        listed = {p.list_cents for p in self._shop.catalog.products()}
        return listed | self._convo.revealed_cents

    def run(self, call: ToolCall) -> dict:
        handler = getattr(self, f"_{call.name}", None)
        if handler is None or call.name not in {s.name for s in SPECS}:
            return {"ok": False, "error": f"unknown tool {call.name}"}
        try:
            return handler(**call.args)
        except CatalogError as error:
            return {"ok": False, "error": str(error)}
        except TypeError:
            return {"ok": False, "error": f"bad arguments for {call.name}"}

    def _price(self, cents: int, currency: str) -> str:
        self._convo.revealed_cents.add(cents)
        return fmt(cents, currency)

    def _view(self, product_id: str) -> dict:
        view = self._shop.catalog.public(product_id)
        price = self._price(view.pop("list_cents"), view["currency"])
        return {**view, "price": price}

    def _list_products(self) -> dict:
        return {"ok": True, "products": [self._view(p.product_id) for p in self._shop.catalog.products()]}

    def _get_product(self, product_id: str) -> dict:
        return {"ok": True, "product": self._view(product_id)}

    def _propose_price(self, product_id: str, offered_amount: str) -> dict:
        product = self._shop.catalog.get(product_id)
        asked = parse_amount(offered_amount)
        if asked is None:
            return {"ok": False, "error": "could not read that amount; ask the customer for a number"}
        round_no = self._convo.rounds.get(product_id, 0) + 1
        self._convo.rounds[product_id] = round_no
        decision = decide(product.policy, asked, round_no)
        if decision.action == "reject":
            return {"ok": True, "decision": "reject", "say": "That offer isn't possible."}
        price = decision.price_cents
        standing = self._convo.standing_offers.get(product_id)
        if standing is not None and standing < price:
            price = standing
        self._convo.standing_offers[product_id] = price
        return {
            "ok": True,
            "decision": "accept" if decision.action == "accept" else decision.action,
            "price": self._price(price, product.currency),
            "final": decision.action == "final",
        }

    def _checkout(self, product_id: str, size: str) -> dict:
        product = self._shop.catalog.get(product_id)
        if size not in product.sizes:
            return {"ok": False, "error": f"size {size} doesn't exist; sizes are {list(product.sizes)}"}
        if self._shop.catalog.in_stock(product_id, size) <= 0:
            return {"ok": False, "error": f"size {size} is out of stock"}
        price = self._convo.standing_offers.get(product_id, product.list_cents)
        quote = self._shop.quotes.create(product_id, price, product.currency, size=size)
        self._shop.catalog.reserve(product_id, size)
        try:
            checkout = self._shop.checkout.create_checkout(quote)
        except Exception:
            self._shop.catalog.release(product_id, size)
            return {"ok": False, "error": "checkout is unavailable right now; apologise and offer to try again"}
        self.checkout_url, self.order_id = checkout.approve_url, checkout.order_id
        return {"ok": True, "price": self._price(price, product.currency), "size": size, "held_minutes": 15}

    def _hand_to_owner(self, reason: str) -> dict:
        self._convo.handed_off = reason
        return {"ok": True, "say": "Tell the customer the owner will reply personally soon."}
