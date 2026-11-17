"""Watch a cooking video and write down what is actually said and shown.

Claude can't take video. Google's Gemini API can: it accepts a public YouTube link
directly, or a short video file, and processes both the picture and the sound.
So for videos, Gemini watches and takes notes, and the notes then go through the
same recipe extraction as any other source. Nothing is filled in here: where the
video gives no amount, the notes say so.

    GEMINI_API_KEY          from aistudio.google.com (there is a free tier)
    FRIDGEBUD_VIDEO_MODEL   default gemini-3.7-flash

Note: on Gemini's free tier, Google may use what you send to improve its products.
"""
from __future__ import annotations

import base64
import json
import os
import re

from .capture import Transport, _http_post

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-3.7-flash"
MAX_INLINE_BYTES = 14 * 1024 * 1024  # the whole request must stay under 20 MB once encoded

PROMPT = """Watch this video carefully, both what is said and what is shown on screen (including any text overlays).

If it is not someone cooking or preparing a dish, reply with exactly: NOT A RECIPE

Otherwise write plain notes so that someone could cook the dish without watching:
DISH: the name of the dish
SERVES / TIME: only if the video says
INGREDIENTS: every ingredient used, one per line, with the amount exactly as said or shown.
  If the video never gives an amount for something, write "amount not given" for it. Do not guess amounts.
STEPS: every step in the order it happens, with the heat levels, times and visual cues the cook mentions or shows.
TIPS: anything the cook stresses.

Only write what is in the video. Do not add ingredients or steps from your own knowledge."""

_YT_ID = re.compile(r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/|live/))([\w-]{6,20})", re.I)


def youtube_id(url: str) -> str | None:
    m = _YT_ID.search(url or "")
    return m.group(1) if m else None


class GeminiWatcher:
    def __init__(self, api_key: str | None = None, transport: Transport | None = None, model: str | None = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")
        self.model = model or os.environ.get("FRIDGEBUD_VIDEO_MODEL", DEFAULT_MODEL)
        self.transport = transport or (lambda url, headers, body: _http_post(url, headers, body, timeout=240))
        if not self.api_key and transport is None:
            raise RuntimeError("Set GEMINI_API_KEY to watch videos.")

    def _ask(self, video_part: dict) -> str | None:
        body = {"contents": [{"parts": [video_part, {"text": PROMPT}]}], "generationConfig": {"temperature": 0.2}}
        headers = {"x-goog-api-key": self.api_key, "content-type": "application/json"}
        reply = self.transport(API.format(model=self.model), headers, json.dumps(body).encode())
        parts = ((reply.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        notes = "".join(p.get("text", "") for p in parts).strip()
        if not notes or notes.upper().startswith("NOT A RECIPE"):
            return None
        return notes[:6000]

    def watch_youtube(self, url: str) -> str | None:
        """Notes from a public YouTube video, or None if it isn't a cooking video."""
        vid = youtube_id(url)
        if not vid:
            raise ValueError("not a YouTube video link")
        return self._ask({"file_data": {"file_uri": f"https://www.youtube.com/watch?v={vid}"}})

    def watch_file(self, data: bytes, mime_type: str = "video/mp4") -> str | None:
        """Notes from a video file someone sent (e.g. a saved TikTok or Reel)."""
        if len(data) > MAX_INLINE_BYTES:
            raise ValueError("video too large")
        return self._ask({"inline_data": {"mime_type": mime_type or "video/mp4", "data": base64.b64encode(data).decode()}})


LISTEN = """This is a message someone sent to Bud, a kitchen assistant ({what}). Reply with JSON only:
{{"transcript": "what they said, word for word",
 "about_me": true if they are mainly describing their own food taste or eating routine, false if it is a request or question,
 "likes": ["dishes, ingredients or flavours they say they love"],
 "cuisines": ["cuisines they say they like"],
 "avoid": ["foods they dislike, can't eat or avoid"],
 "diet": "any diet or food rule they follow, else empty",
 "meals": {{"breakfast": "HH:MM 24h or empty", "brunch": "HH:MM or empty", "lunch": "HH:MM or empty", "dinner": "HH:MM or empty", "snacks": "HH:MM or empty"}},
 "main_meal": "breakfast, brunch, lunch or dinner: the meal they cook or care about most, else empty"}}
Only include what they actually said. Use empty lists and strings for anything they didn't mention."""


def _listen(watcher: "GeminiWatcher", parts: list[dict]) -> dict | None:
    body = {"contents": [{"parts": parts}], "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}}
    headers = {"x-goog-api-key": watcher.api_key, "content-type": "application/json"}
    reply = watcher.transport(API.format(model=watcher.model), headers, json.dumps(body).encode())
    out = "".join(p.get("text", "") for p in ((reply.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
    try:
        data = json.loads(re.sub(r"^```(?:json)?|```$", "", out.strip()).strip())
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("transcript") else None


def listen_voice(watcher: "GeminiWatcher", data: bytes, mime_type: str = "audio/ogg") -> dict | None:
    """A voice note: what was said, and any food taste or routine in it (see LISTEN)."""
    if len(data) > MAX_INLINE_BYTES:
        raise ValueError("voice note too large")
    return _listen(watcher, [{"inline_data": {"mime_type": mime_type or "audio/ogg", "data": base64.b64encode(data).decode()}},
                             {"text": LISTEN.format(what="a voice note")}])


def read_taste(watcher: "GeminiWatcher", text: str) -> dict | None:
    """The same, for someone who typed it instead (TASTE ...)."""
    return _listen(watcher, [{"text": LISTEN.format(what="typed: " + text.strip()[:2000])}])
