"""Make each cook-along once, then reuse it.

Voicing a recipe costs real money (Claude writes the script, Gemini reads it), so both are kept:
    script   by recipe content, so a replay (or another home with the same recipe) skips Claude
    file_id  Telegram's id for the uploaded audio, so a replay is resent instantly without Gemini at all
The key is the recipe's content, not its id: edit a recipe and it gets a fresh cook-along.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from .store import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS audio_cache (
    key TEXT PRIMARY KEY,
    title TEXT,
    script TEXT NOT NULL,
    ai_written INTEGER DEFAULT 0,
    file_id TEXT,
    created_at TEXT
);
"""


def recipe_key(title: str, ingredients: list[tuple[str, str]], steps: list[str]) -> str:
    body = json.dumps([title.strip().lower(), [[n.strip().lower(), (q or "").strip().lower()] for n, q in ingredients],
                       [s.strip() for s in steps]], sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()[:32]


class AudioCache:
    def __init__(self, store: Store):
        self.db = store.db
        self.db.executescript(SCHEMA)

    def script(self, key: str) -> tuple[str, bool] | None:
        row = self.db.execute("SELECT script, ai_written FROM audio_cache WHERE key = ?", (key,)).fetchone()
        return (row["script"], bool(row["ai_written"])) if row else None

    def save_script(self, key: str, title: str, script: str, ai_written: bool, now: datetime) -> None:
        self.db.execute("INSERT OR IGNORE INTO audio_cache (key, title, script, ai_written, created_at) VALUES (?,?,?,?,?)",
                        (key, title, script, int(ai_written), now.isoformat()))
        self.db.commit()

    def file_id(self, key: str) -> str | None:
        row = self.db.execute("SELECT file_id FROM audio_cache WHERE key = ?", (key,)).fetchone()
        return row["file_id"] if row else None

    def save_file_id(self, key: str, file_id: str | None) -> None:
        self.db.execute("UPDATE audio_cache SET file_id = ? WHERE key = ?", (file_id, key))
        self.db.commit()
