"""The sales agent: one customer message in, one reply out. Workers each run a copy of this."""

from dataclasses import dataclass, field

from hiclose.agent.conversation import ConversationStore
from hiclose.agent.guardrail import leaked_instructions, price_violations
from hiclose.agent.instructions import BUSY_FALLBACK, CORRECTION, LEAK_CORRECTION, SAFE_FALLBACK, render_user, system_prompt
from hiclose.agent.llm import ChatModel, ModelUnavailable
from hiclose.agent.tools import Shop, ShopTools
from hiclose.agent.tryon import Photo


@dataclass(frozen=True)
class AgentReply:
    text: str
    checkout_url: str | None = None
    order_id: str | None = None
    handed_off: bool = False
    tools_used: list[str] = field(default_factory=list)
    guard_rewrites: int = 0
    image_url: str | None = None


class SalesAgent:
    def __init__(self, model: ChatModel, shop: Shop, store: ConversationStore, max_steps: int = 6):
        self._model = model
        self._shop = shop
        self._store = store
        self._max_steps = max_steps
        self._system = system_prompt(shop.brand, tryon=shop.tryon is not None, photo_match=shop.photo_match is not None)

    def _context(self, post_id: str | None, handed_off: str | None, sent_photo: bool = False) -> str | None:
        notes = []
        product = self._shop.catalog.for_post(post_id)
        if product is not None:
            notes.append(f"The customer is messaging about post {post_id}, which shows product_id {product.product_id} ({product.name}).")
        if handed_off:
            notes.append(f"Earlier this chat was passed to the owner for: {handed_off}. Keep helping with shopping questions.")
        if sent_photo:
            ways = []
            if self._shop.photo_match is not None:
                ways.append("if it shows a product (a screenshot or something they saw), call identify_photo")
            if self._shop.tryon is not None:
                ways.append("if it's of themselves and they want to see an item on them, call try_on for that item")
            notes.append("The customer just sent a photo: " + "; ".join(ways) + ". If you can't tell which, ask.")
        return " ".join(notes) or None

    def handle(self, customer_id: str, text: str, post_id: str | None = None, photo: Photo | None = None) -> AgentReply | None:
        convo = self._store.get(customer_id)
        if convo.owner_active:
            return None
        if photo is not None:
            convo.keep_photo(photo)
        tools = ShopTools(self._shop, convo)
        photos_used = self._shop.tryon is not None or self._shop.photo_match is not None
        context = self._context(post_id, convo.handed_off, sent_photo=photo is not None and photos_used)
        convo.history.append(self._model.user_turn(render_user(text, context)))
        used: list[str] = []
        rewrites = 0

        def reply(message: str) -> AgentReply:
            return AgentReply(message, tools.checkout_url, tools.order_id, tools.handed_off_now, used, rewrites, tools.image_url)

        for _ in range(self._max_steps):
            try:
                turn = self._model.generate(self._system, convo.history, tools.specs())
            except ModelUnavailable:
                return reply(BUSY_FALLBACK)
            convo.history.append(turn.raw)
            if turn.tool_calls:
                results = []
                for call in turn.tool_calls:
                    used.append(call.name)
                    results.append((call, tools.run(call)))
                convo.history.append(self._model.tool_results_turn(results))
                continue
            found = price_violations(turn.text, tools.allowed_cents())
            leaked = leaked_instructions(turn.text, self._system)
            if not found and not leaked and turn.text:
                return reply(turn.text)
            if rewrites >= 1:
                return reply(SAFE_FALLBACK)
            rewrites += 1
            note = LEAK_CORRECTION if leaked and not found else CORRECTION.format(found=", ".join(found) or "an empty reply")
            convo.history.append(self._model.user_turn(note))
        return reply(SAFE_FALLBACK)
