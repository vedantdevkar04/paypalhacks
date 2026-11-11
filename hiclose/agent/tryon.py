"""Virtual try-on: the customer's own photo plus our product photo in, a preview of them wearing it out.

Privacy: customer photos live only in memory, on that customer's conversation, for 30 minutes.
Try-on results are served at an unguessable URL for an hour, then dropped. Nothing is written to
disk or logged."""

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
    pass


class ImageModel(Protocol):
    def dress(self, person: Photo, garment: Photo, item: str) -> Photo | None: ...


PROMPT = """You are making a virtual try-on preview for a clothing shop.
The first image is the customer. The second image is the product: {item}.
Make a realistic photo of the same person wearing or carrying this exact product.
Keep their face, skin tone, hair, body shape, pose and background exactly as they are; change only
what's needed to show the product, matching its colour, pattern and details. Keep them fully and
modestly clothed. No text, logos or watermarks. Return only the image."""


class GeminiImageModel:
    """Gemini image editing. Tries each model in turn, since image model names change often."""

    def __init__(self, api_key: str, models: list[str], timeout_ms: int = 60000):
        from google import genai
        from google.genai import types

        self._types = types
        self._client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_ms))
        self._models = models

    @classmethod
    def from_env(cls) -> "GeminiImageModel | None":
        key = os.environ.get("GEMINI_API_KEY", "")
        if not key:
            return None
        models = [m.strip() for m in os.environ.get(
            "GEMINI_IMAGE_MODELS", "gemini-3.1-flash-image,gemini-2.5-flash-image").split(",") if m.strip()]
        return cls(key, models)

    def dress(self, person: Photo, garment: Photo, item: str) -> Photo | None:
        types = self._types
        contents = [
            types.Part.from_bytes(data=person.data, mime_type=person.mime),
            types.Part.from_bytes(data=garment.data, mime_type=garment.mime),
            PROMPT.format(item=item),
        ]
        config = types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"])
        last_error: Exception | None = None
        for model in self._models:
            try:
                response = self._client.models.generate_content(model=model, contents=contents, config=config)
            except Exception as error:  # unknown model, busy, quota: try the next one
                last_error = error
                continue
            for candidate in response.candidates or []:
                for part in (candidate.content.parts if candidate.content else None) or []:
                    blob = part.inline_data
                    if blob is not None and blob.data:
                        return Photo(blob.data, blob.mime_type or "image/png")
            return None  # the model answered but made no image (e.g. no person in the photo)
        raise TryOnFailed(f"no image model available: {last_error}")


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
            raise TryOnFailed("no product photo")
        path = self._dir / product.image
        garment = Photo(path.read_bytes(), "image/png" if path.suffix == ".png" else "image/jpeg")
        result = self._model.dress(person, garment, f"{product.name}. {product.description}")
        if result is None:
            raise TryOnFailed("the model made no image")
        token = secrets.token_urlsafe(18)
        self._results[token] = (self._clock(), result)
        while len(self._results) > self._max:
            self._results.popitem(last=False)
        return f"/tryon/{token}"

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
