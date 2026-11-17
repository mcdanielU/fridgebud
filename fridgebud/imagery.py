"""Pictures for recipe cards, so a recipe looks like food and not just a wall of text.

- A YouTube recipe gets the video's own thumbnail (free, and it's the real dish).
- An AI-written recipe gets a simple AI food photo from Gemini, captioned as an AI image so nobody mistakes it
  for the real thing. Each dish is drawn once: Telegram keeps the photo and Bud resends it by its file_id.
- Anything else (a blog link, a screenshot, a paste) gets no picture rather than a made-up one.

    GEMINI_API_KEY            same key as video and voice
    FRIDGEBUD_IMAGE_MODEL     default gemini-2.5-flash-image (about US$0.04 an image)
    FRIDGEBUD_IMAGE_DAILY_CAP new pictures a day across everyone (default 40), so cost can't run away
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime

from .capture import _http_post

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-2.5-flash-image"

SCHEMA = """
CREATE TABLE IF NOT EXISTS photo_cache (
    key TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def youtube_thumbnail(video_id: str) -> str:
    return f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"


def dish_key(title: str) -> str:
    return "img:" + hashlib.sha1(" ".join((title or "").lower().split()).encode()).hexdigest()[:16]


def dish_prompt(title: str) -> str:
    return (f"A simple, appetizing photo of {title}, home-cooked, served on a plain plate or bowl on a wooden table, "
            "soft natural daylight, slightly overhead angle, realistic. No text, no labels, no people, no hands.")


class PhotoCache:
    def __init__(self, store):
        self.db = store.db
        self.db.executescript(SCHEMA)

    def file_id(self, key: str) -> str | None:
        row = self.db.execute("SELECT file_id FROM photo_cache WHERE key = ?", (key,)).fetchone()
        return row["file_id"] if row else None

    def save(self, key: str, file_id: str | None, now: datetime) -> None:
        if file_id:
            self.db.execute("INSERT OR REPLACE INTO photo_cache (key, file_id, created_at) VALUES (?,?,?)",
                            (key, file_id, now.isoformat()))
        else:
            self.db.execute("DELETE FROM photo_cache WHERE key = ?", (key,))
        self.db.commit()


class Imager:
    def __init__(self, api_key: str | None = None, model: str | None = None, transport=None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")
        self.model = model or os.environ.get("FRIDGEBUD_IMAGE_MODEL", DEFAULT_MODEL)
        self.daily_cap = int(os.environ.get("FRIDGEBUD_IMAGE_DAILY_CAP", "40"))
        self.transport = transport or (lambda url, headers, body: _http_post(url, headers, body, timeout=90))
        if not self.api_key and transport is None:
            raise RuntimeError("Set GEMINI_API_KEY to draw recipe pictures.")

    def draw(self, prompt: str) -> tuple[bytes, str]:
        """(image bytes, mime type). Raises if Gemini sends no picture."""
        body = {"contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseModalities": ["IMAGE"]}}
        headers = {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}
        reply = self.transport(API.format(model=self.model), headers, json.dumps(body).encode())
        for cand in reply.get("candidates") or []:
            for part in (cand.get("content") or {}).get("parts") or []:
                blob = part.get("inlineData") or part.get("inline_data")
                if blob and blob.get("data"):
                    return base64.b64decode(blob["data"]), blob.get("mimeType") or blob.get("mime_type") or "image/png"
        raise RuntimeError("Gemini returned no image")
