import pytest
from google.genai import errors, types

from hiclose.agent.llm import GeminiModel, ModelUnavailable


def busy():
    return errors.ServerError(503, {"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}})


def reply(*parts):
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=list(parts)))])


def model_with(outcomes):
    sleeps = []
    model = GeminiModel("test-key", ["primary", "fallback"], attempts=3, sleep=sleeps.append)
    calls = []

    def fake_call(name, history, config):
        calls.append(name)
        outcome = outcomes[name].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    model._call = fake_call
    return model, calls, sleeps


def test_busy_primary_falls_back_to_the_second_model():
    model, calls, sleeps = model_with({"primary": [busy(), busy(), busy()], "fallback": [reply(types.Part.from_text(text="hi"))]})
    turn = model.generate("system", [], [])
    assert turn.model == "fallback" and turn.text == "hi"
    assert calls == ["primary", "primary", "primary", "fallback"]
    assert len(sleeps) == 2


def test_retry_succeeds_on_the_same_model():
    model, calls, _ = model_with({"primary": [busy(), reply(types.Part.from_text(text="ok"))], "fallback": []})
    assert model.generate("system", [], []).model == "primary"
    assert calls == ["primary", "primary"]


def test_everything_busy_raises_model_unavailable():
    model, _, _ = model_with({"primary": [busy()] * 3, "fallback": [busy()] * 3})
    with pytest.raises(ModelUnavailable):
        model.generate("system", [], [])


def test_bad_request_is_not_retried():
    bad = errors.ClientError(400, {"error": {"code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}})
    model, calls, _ = model_with({"primary": [bad], "fallback": []})
    with pytest.raises(errors.ClientError):
        model.generate("system", [], [])
    assert calls == ["primary"]


def test_repeated_text_pieces_are_dropped():
    model, _, _ = model_with({"primary": [reply(
        types.Part.from_text(text='Sure! "Want the jacket in M or L?'),
        types.Part.from_text(text="Want the jacket in M or L?"),
    )], "fallback": []})
    assert model.generate("system", [], []).text == 'Sure! "Want the jacket in M or L?'


def test_tool_calls_and_text_are_parsed():
    part = types.Part(function_call=types.FunctionCall(name="get_product", args={"product_id": "x"}, id="c1"))
    model, _, _ = model_with({"primary": [reply(part)], "fallback": []})
    turn = model.generate("system", [], [])
    assert turn.tool_calls[0].name == "get_product"
    assert turn.tool_calls[0].args == {"product_id": "x"}
    assert turn.tool_calls[0].call_id == "c1"
