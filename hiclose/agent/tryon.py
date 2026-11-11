"""Virtual try-on: the customer's own photo plus our product photo in, a preview of them wearing it out.

Privacy: customer photos live only in memory, on that customer's conversation, for 30 minutes.
Try-on results are served at an unguessable URL for an hour, then dropped. Nothing is written to
disk or logged."""

import logging
import os
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}
MAX_PHOTO_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class Photo:
    data: bytes
    mime: str

    def __repr__(self) -> str:  # never dump image bytes into logs
        return f"Photo({self.mime}, {len(self.data)} bytes)"


def acceptable(data: bytes, mime: str) -> Photo | None:
    mime = (mime or "").split(";")[0].strip().lower()
    if mime not in ALLOWED_MIME or not data or len(data) > MAX_PHOTO_BYTES:
        return None
    return Photo(data, mime)


class TryOnFailed(Exception):
    """unavailable: no image model answered (wrong name, quota, outage). no_image: a model looked at
    the photo and made no image, so a clearer photo might help."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind


class ImageModel(Protocol):
    def dress(self, person: Photo, garment: Photo, item: str) -> Photo | None: ...


PROMPT = """You are making a virtual try-on preview for a clothing shop.
The first image is the customer. The second image is the product: {item}.
Make a realistic photo of the same person wearing or carrying this exact product.
Keep their face, skin tone, hair, body shape, pose and background exactly as they are; change only
what's needed to show the product, matching its colour, pattern and details. Keep them fully and
modestly clothed. No text, logos or watermarks. Return only the image."""


log = logging.getLogger("hiclose")


class GeminiImageModel:
    """Gemini image editing. Tries each model in turn, since image model names change often; if none
    of the configured names work, asks the API which image models this key can use."""

    def __init__(self, api_key: str, models: list[str], timeout_ms: int = 60000, client=None):
        from google.genai import types

        self._types = types
        if client is None:
            from google import genai

            client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_ms))
        self._client = client
        self._models = list(models)
        self._discovered = False
        self._clock = time.monotonic
        self._paused_until = 0.0
        self.last_model: str | None = None
        self.last_error: str | None = None

    QUOTA_PAUSE_SECONDS = 600

    def status(self) -> dict:
        paused = max(0, round(self._paused_until - self._clock()))
        return {"models": self._models, "last_model": self.last_model, "last_error": self.last_error,
                "paused_seconds": paused}

    def _discover(self) -> list[str]:
        """Image-capable models this key can call, newest-looking first."""
        self._discovered = True
        try:
            found = [
                m.name.removeprefix("models/") for m in self._client.models.list()
                if "image" in (m.name or "") and "generateContent" in (m.supported_actions or [])
            ]
        except Exception as error:
            log.warning("couldn't list gemini models: %s", error)
            return []
        new = sorted((m for m in found if m not in self._models), reverse=True)
        log.warning("try-on: configured image models failed; found these for this key: %s", new)
        self._models += new
        return new

    @classmethod
    def from_env(cls) -> "GeminiImageModel | None":
        key = os.environ.get("GEMINI_API_KEY", "")
        if not key:
            return None
        models = [m.strip() for m in os.environ.get(
            "GEMINI_IMAGE_MODELS", "gemini-3.1-flash-image,gemini-2.5-flash-image").split(",") if m.strip()]
        return cls(key, models)

    def dress(self, person: Photo, garment: Photo, item: str) -> Photo | None:
        if self._clock() < self._paused_until:
            raise TryOnFailed("unavailable", f"paused after quota errors: {self.last_error}")
        types = self._types
        contents = [
            types.Part.from_bytes(data=person.data, mime_type=person.mime),
            types.Part.from_bytes(data=garment.data, mime_type=garment.mime),
            PROMPT.format(item=item),
        ]
        config = types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"])
        errors: list[str] = []
        queue = list(self._models)
        while queue:
            model = queue.pop(0)
            try:
                response = self._client.models.generate_content(model=model, contents=contents, config=config)
            except Exception as error:  # unknown model, busy, quota: try the next one
                errors.append(f"{model}: {type(error).__name__}: {str(error)[:200]}")
                log.warning("try-on model %s failed: %s", model, errors[-1])
                if not queue and not self._discovered:
                    queue = self._discover()
                continue
            self.last_model = model
            image = self._image(response)
            if image is not None:
                self.last_error = None
                return image
            # The model answered but made no image: say why in our logs (no photo data in there).
            reasons = [str(c.finish_reason) for c in response.candidates or [] if c.finish_reason]
            feedback = getattr(response, "prompt_feedback", None)
            block = str(getattr(feedback, "block_reason", "") or "")
            said = " ".join(p.text for c in response.candidates or [] if c.content
                            for p in (c.content.parts or []) if p.text)[:200]
            self.last_error = f"{model} made no image (finish={','.join(reasons) or '-'} block={block or '-'})"
            log.warning("try-on: %s; model said: %s", self.last_error, said or "-")
            return None
        self.last_error = " | ".join(errors) or "no image models configured"
        if errors and any("429" in e or "RESOURCE_EXHAUSTED" in e for e in errors):
            # Out of quota (the free tier allows no image generation): stop calling for a while.
            self._paused_until = self._clock() + self.QUOTA_PAUSE_SECONDS
        raise TryOnFailed("unavailable", self.last_error)

    @staticmethod
    def _image(response) -> Photo | None:
        """The final image in the response (thinking models may send draft images first)."""
        found = None
        for candidate in response.candidates or []:
            for part in (candidate.content.parts if candidate.content else None) or []:
                blob = part.inline_data
                if blob is not None and blob.data and not getattr(part, "thought", False):
                    found = Photo(blob.data, blob.mime_type or "image/png")
        return found


class TryOnStudio:
    """Makes try-ons and keeps the results just long enough for the chat to show them."""

    def __init__(self, model: ImageModel, products_dir: Path, ttl_seconds: int = 3600, max_items: int = 200,
                 clock=time.monotonic):
        self._model = model
        self._dir = Path(products_dir)
        self._ttl = ttl_seconds
        self._max = max_items
        self._clock = clock
        self._results: OrderedDict[str, tuple[float, Photo]] = OrderedDict()

    def render(self, person: Photo, product) -> str:
        """Returns the site path of the try-on image. Raises TryOnFailed if none could be made."""
        if not product.image:
            raise TryOnFailed("unavailable", "no product photo")
        path = self._dir / product.image
        garment = Photo(path.read_bytes(), "image/png" if path.suffix == ".png" else "image/jpeg")
        result = self._model.dress(person, garment, f"{product.name}. {product.description}")
        if result is None:
            raise TryOnFailed("no_image")
        token = secrets.token_urlsafe(18)
        self._results[token] = (self._clock(), result)
        while len(self._results) > self._max:
            self._results.popitem(last=False)
        return f"/tryon/{token}"

    def status(self) -> dict:
        status = getattr(self._model, "status", None)
        return status() if status else {}

    def get(self, token: str) -> Photo | None:
        entry = self._results.get(token)
        if entry is None:
            return None
        made, photo = entry
        if self._clock() - made > self._ttl:
            del self._results[token]
            return None
        return photo


class FakeImageModel:
    """For tests and local runs: returns a fixed image and remembers what it was asked."""

    def __init__(self, result: Photo | None = Photo(b"\xff\xd8fake-tryon", "image/jpeg")):
        self.result = result
        self.calls: list[tuple[Photo, Photo, str]] = []

    def dress(self, person: Photo, garment: Photo, item: str) -> Photo | None:
        self.calls.append((person, garment, item))
        return self.result
