from pathlib import Path

from hiclose.agent import FakeCheckout, ModelTurn, Shop, ToolCall
from hiclose.agent.llm import ModelUnavailable
from hiclose.rules import Catalog, QuoteBook

SEED = Path(__file__).parent.parent / "fixtures" / "catalog.json"


def make_shop() -> Shop:
    return Shop("Indigo Lane", Catalog.from_json(SEED), QuoteBook(), FakeCheckout())


def say(text: str) -> ModelTurn:
    return ModelTurn(text=text, raw={"role": "model", "text": text})


def call(name: str, **args) -> ModelTurn:
    return ModelTurn(text="", tool_calls=[ToolCall(name, args)], raw={"role": "model", "call": name})


class FakeModel:
    def __init__(self, script: list[ModelTurn] | None = None, busy: bool = False):
        self.script = list(script or [])
        self.busy = busy
        self.seen: list[list] = []

    def user_turn(self, text: str) -> dict:
        return {"role": "user", "text": text}

    def tool_results_turn(self, results) -> dict:
        return {"role": "tool", "results": [(c.name, r) for c, r in results]}

    def generate(self, system, history, tools) -> ModelTurn:
        if self.busy:
            raise ModelUnavailable("busy")
        self.seen.append(list(history))
        return self.script.pop(0)
