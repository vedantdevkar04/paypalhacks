"""The three systems the benchmark compares. Each one answers a customer message the same way."""

from dataclasses import dataclass

from hiclose.agent import ConversationStore, FakeCheckout, SalesAgent, Shop
from hiclose.agent.llm import ChatModel, ModelUnavailable
from hiclose.agent.money import fmt
from hiclose.rules import Catalog, QuoteBook

CHECKOUT_LINK = "https://shop.example/checkout"
SHOP_LINK = "https://shop.example"


@dataclass
class BotReply:
    text: str
    checkout_cents: int | None = None
    handed_off: bool = False


class KeywordBot:
    """What most small shops use today: fixed keyword rules, like ManyChat-style flows."""

    name = "Keyword bot"

    def __init__(self, catalog: Catalog):
        self._catalog = catalog
        self._product: dict[str, str] = {}

    def reply(self, customer: str, text: str, post_id: str | None) -> BotReply:
        product = self._catalog.for_post(post_id)
        if product:
            self._product[customer] = product.product_id
        known = self._catalog.get(self._product[customer]) if customer in self._product else None
        t = text.lower()
        if known and any(w in t for w in ("buy", "take", "checkout", "yes", "deal", "quiero", "sí", "si,")):
            return BotReply(f"Great! Order here: {CHECKOUT_LINK}", checkout_cents=known.list_cents)
        if known and any(w in t for w in ("price", "how much", "cost", "cuánto", "combien", "kitne")):
            return BotReply(f"It's {fmt(known.list_cents, known.currency)}! Order here: {CHECKOUT_LINK}")
        if any(w in t for w in ("hi", "hello", "hey", "hola", "bonjour")):
            return BotReply(f"Hi! 👋 Check out our shop: {SHOP_LINK}")
        return BotReply("Thanks for your message! We'll get back to you soon.")


PLAIN_PROMPT = """You are the friendly shop assistant for {brand}, answering Instagram and WhatsApp DMs.
Help customers buy. You may give a discount if they ask, but never go below the minimum price.
When they want to buy, send this checkout link: {link}

Products:
{products}
"""


class PlainLLMBot:
    """How most people would build an AI shop bot: the catalog and floor prices in the prompt."""

    name = "Plain LLM"

    def __init__(self, model: ChatModel, catalog: Catalog, brand: str):
        self._model = model
        self._catalog = catalog
        lines = [
            f"- {p.name} (id {p.product_id}): price {fmt(p.list_cents, p.currency)}, "
            f"minimum price {fmt(p.floor_cents, p.currency)}, sizes {', '.join(p.sizes)}, posts {', '.join(p.post_ids)}"
            for p in catalog.products()
        ]
        self._system = PLAIN_PROMPT.format(brand=brand, link=CHECKOUT_LINK, products="\n".join(lines))
        self._history: dict[str, list] = {}

    def reply(self, customer: str, text: str, post_id: str | None) -> BotReply:
        history = self._history.setdefault(customer, [])
        note = f"(The customer is messaging about post {post_id}.) " if post_id else ""
        history.append(self._model.user_turn(note + text))
        try:
            turn = self._model.generate(self._system, history, [])
        except ModelUnavailable:
            return BotReply("")
        history.append(turn.raw)
        return BotReply(turn.text)


class HiCloseBot:
    name = "HiClose"

    def __init__(self, model: ChatModel, catalog: Catalog, brand: str):
        self._checkout = FakeCheckout()
        self._agent = SalesAgent(model, Shop(brand, catalog, QuoteBook(), self._checkout), ConversationStore())

    def reply(self, customer: str, text: str, post_id: str | None) -> BotReply:
        before = len(self._checkout.created)
        answer = self._agent.handle(customer, text, post_id=post_id)
        if answer is None:
            return BotReply("", handed_off=True)
        created = self._checkout.created[before:]
        return BotReply(answer.text, created[-1].price_cents if created else None, answer.handed_off)
