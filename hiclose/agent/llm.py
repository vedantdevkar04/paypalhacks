"""The only place HiClose calls a model. Retries busy models with backoff, then falls back."""

import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from google import genai
from google.genai import errors, types


class ModelUnavailable(Exception):
    pass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict
    call_id: str | None = None


@dataclass
class ModelTurn:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None
    model: str = ""


class ChatModel(Protocol):
    def user_turn(self, text: str) -> Any: ...
    def tool_results_turn(self, results: list[tuple[ToolCall, dict]]) -> Any: ...
    def generate(self, system: str, history: list, tools: list[ToolSpec]) -> ModelTurn: ...


def _retryable(error: Exception) -> bool:
    if isinstance(error, errors.ServerError):
        return True
    return isinstance(error, errors.ClientError) and error.code == 429


class GeminiModel:
    def __init__(
        self,
        api_key: str,
        models: list[str],
        attempts: int = 3,
        timeout_ms: int = 25000,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_ms))
        self._models = models
        self._attempts = attempts
        self._sleep = sleep

    @classmethod
    def from_env(cls) -> "GeminiModel":
        primary = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
        fallback = os.environ.get("GEMINI_SMALL_MODEL", "gemini-3.5-flash-lite")
        return cls(os.environ["GEMINI_API_KEY"], [primary, fallback])

    def user_turn(self, text: str) -> types.Content:
        return types.Content(role="user", parts=[types.Part.from_text(text=text)])

    def tool_results_turn(self, results: list[tuple[ToolCall, dict]]) -> types.Content:
        parts = [
            types.Part(function_response=types.FunctionResponse(id=call.call_id, name=call.name, response=result))
            for call, result in results
        ]
        return types.Content(role="user", parts=parts)

    def generate(self, system: str, history: list, tools: list[ToolSpec]) -> ModelTurn:
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.parameters)
                for t in tools
            ])] if tools else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        last_error: Exception | None = None
        for model in self._models:
            for attempt in range(self._attempts):
                try:
                    response = self._call(model, history, config)
                    return self._parse(response, model)
                except Exception as error:
                    if not _retryable(error):
                        raise
                    last_error = error
                    if attempt < self._attempts - 1:
                        self._sleep(2 ** attempt + random.random())
        raise ModelUnavailable(f"every model is busy: {last_error}")

    def _call(self, model: str, history: list, config: types.GenerateContentConfig):
        return self._client.models.generate_content(model=model, contents=history, config=config)

    @staticmethod
    def _parse(response, model: str) -> ModelTurn:
        content = response.candidates[0].content if response.candidates else types.Content(role="model", parts=[])
        parts = content.parts or []
        calls = [
            ToolCall(name=p.function_call.name, args=dict(p.function_call.args or {}), call_id=p.function_call.id)
            for p in parts if p.function_call
        ]
        text = "".join(p.text for p in parts if p.text and not p.thought)
        return ModelTurn(text=text.strip(), tool_calls=calls, raw=content, model=model)
