"""Photo capture: a haul, surprise-bag, fridge or receipt photo -> items (+ prices).

`ClaudeVisionExtractor` sends one photo to Anthropic's Messages API and asks
for strict JSON. It uses only the standard library. Set ANTHROPIC_API_KEY
(and optionally FRIDGEBUD_VISION_MODEL) in the environment.

Prices matter: they let the weekly receipt say "$17 kept, at your prices"
instead of a guess. Confidence matters too: FridgeBud only asks about the
one or two items it isn't sure of, never the whole list.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5-5"
CONFIDENT = 0.6  # below this, ask the user instead of guessing

PROMPT = """You are reading a photo for a food-waste app. The photo is one of:
a grocery receipt, a grocery haul or surprise bag laid out on a counter, or the inside of a fridge.
If instead it is a recipe (a screenshot of a recipe page or a cookbook page), do not list its ingredients:
reply with kind "recipe" and an empty items list.
If it is a restaurant menu or a food-delivery app screen (Uber Eats, DoorDash, SkipTheDishes), reply with
kind "menu" and an empty items list.

List the FOOD items only. Rules:
- Use plain, common names a person would text: "baby spinach", "chicken thighs", "strawberries".
  Decode receipt abbreviations (e.g. "PC BBY SPN" -> "baby spinach"). Never invent items you cannot see or read.
- Skip non-food (bags, cleaning products), taxes, totals, discounts, deposits and loyalty points.
- price: the line price in dollars as a number if it is printed (receipts), else null. Never guess a price.
- quantity: short text like "2" or "1 kg" if visible, else null.
- confidence: 0 to 1, how sure you are the name is right.

Reply with JSON only, no prose:
{"kind": "receipt" | "haul" | "fridge" | "recipe" | "menu" | "other",
 "store": string or null,
 "items": [{"name": string, "quantity": string or null, "price": number or null, "confidence": number}]}"""


@dataclass
class CapturedItem:
    name: str
    quantity: str | None = None
    price: float | None = None
    confidence: float = 1.0
    source_kind: str | None = None  # receipt | haul | fridge | other, when known

    @property
    def confident(self) -> bool:
        return self.confidence >= CONFIDENT


class ItemExtractor(Protocol):
    def extract(self, image: bytes, media_type: str = "image/jpeg") -> list[CapturedItem]:
        ...


class NoopExtractor:
    def extract(self, image: bytes, media_type: str = "image/jpeg") -> list[CapturedItem]:
        return []


class FixedExtractor:
    """For tests and the simulator: returns the items it was given."""

    def __init__(self, items: list[str | CapturedItem]):
        self.items = [i if isinstance(i, CapturedItem) else CapturedItem(i) for i in items]

    def extract(self, image: bytes, media_type: str = "image/jpeg") -> list[CapturedItem]:
        return list(self.items)


# A transport takes (url, headers, body_bytes) and returns the parsed JSON response.
Transport = Callable[[str, dict, bytes], dict]


def _http_post(url: str, headers: dict, body: bytes, timeout: float = 60) -> dict:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as err:
        detail = err.read().decode(errors="replace")[:500]
        raise RuntimeError(f"Vision API error {err.code}: {detail}") from err


def parse_reply(text: str) -> list[CapturedItem]:
    """Pull the JSON object out of the model's reply and validate each item."""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    kind = data.get("kind") if data.get("kind") in {"receipt", "haul", "fridge", "recipe", "menu", "other"} else None
    items = []
    for raw in data.get("items") or []:
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        price = raw.get("price")
        try:
            price = round(float(price), 2) if price is not None else None
        except (TypeError, ValueError):
            price = None
        if price is not None and not (0 < price < 500):
            price = None  # reject obviously wrong prices rather than poison the ROI
        try:
            conf = max(0.0, min(1.0, float(raw.get("confidence", 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
        qty = raw.get("quantity")
        items.append(CapturedItem(name, str(qty) if qty not in (None, "") else None, price, conf, kind))
    return items


def reply_kind(text: str) -> str | None:
    match = re.search(r'"kind"\s*:\s*"(\w+)"', text or "")
    return match.group(1) if match else None


class ClaudeVisionExtractor:
    def __init__(self, api_key: str | None = None, model: str | None = None,
                 transport: Transport | None = None, max_tokens: int = 2000):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or os.environ.get("FRIDGEBUD_VISION_MODEL", DEFAULT_MODEL)
        self.transport = transport or _http_post
        self.max_tokens = max_tokens
        if not self.api_key and transport is None:
            raise RuntimeError("Set ANTHROPIC_API_KEY to use photo capture.")

    def extract(self, image: bytes, media_type: str = "image/jpeg") -> list[CapturedItem]:
        return self.extract_with_kind(image, media_type)[1]

    def extract_with_kind(self, image: bytes, media_type: str = "image/jpeg") -> tuple[str | None, list[CapturedItem]]:
        """(kind of photo, items). kind 'recipe' means it is a recipe screenshot, not food."""
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                                 "data": base64.b64encode(image).decode()}},
                    {"type": "text", "text": PROMPT},
                ],
            }],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        reply = self.transport(API_URL, headers, json.dumps(body).encode())
        text = "".join(block.get("text", "") for block in reply.get("content", []) if block.get("type") == "text")
        return reply_kind(text), parse_reply(text)


def media_type_for(path: str) -> str:
    ext = path.lower().rsplit(".", 1)[-1]
    return {"png": "image/png", "webp": "image/webp", "gif": "image/gif"}.get(ext, "image/jpeg")
