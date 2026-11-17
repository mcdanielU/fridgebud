"""Turn a household's reply into an intent. Rule-based for v0.

Replies are meant to be one or two words ("made it", "tossed spinach"), so
rules cover most of them; anything else falls through to UNKNOWN, which an
LLM fallback can handle later.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_SPLIT = re.compile(r"\s*(?:,|;|\band\b|&|\+|\n)\s*")
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "a couple": 2, "a few": 3}


@dataclass
class Intent:
    kind: str                      # see INTENTS
    items: list[str] = field(default_factory=list)
    portions: int | None = None


INTENTS = ("stop", "start", "help", "list", "made", "used", "tossed", "good", "leftover", "add", "dislike", "liked", "diet", "unknown")


def _items(text: str) -> list[str]:
    return [p.strip(" .!") for p in _SPLIT.split(text) if p.strip(" .!")]


def _portions(text: str) -> tuple[str, int | None]:
    m = re.search(r"(?:x\s*(\d+)|(\d+)\s*(?:portions?|servings?|bowls?|plates?)|"
                  r"(one|two|three|four|five|six|a couple|a few)\s+(?:portions?|servings?|bowls?|plates?))", text)
    if not m:
        return text, None
    n = int(m.group(1) or m.group(2)) if (m.group(1) or m.group(2)) else _NUMBER_WORDS[m.group(3)]
    cleaned = (text[: m.start()] + text[m.end():]).strip(" ,.-")
    cleaned = re.sub(r"\b(?:left|leftover|leftovers|of)\b", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" ,.-"), n


def parse(text: str) -> Intent:
    t = re.sub(r"\s+", " ", text.strip().lower())
    if not t:
        return Intent("unknown")
    if t in {"stop", "unsubscribe", "cancel", "quit", "end", "stopall", "arret", "arrêt"}:
        return Intent("stop")
    if t in {"start", "unstop", "yes", "y", "resume"}:
        return Intent("start")
    if t in {"help", "?", "info", "aide"}:
        return Intent("help")
    if t in {"list", "ls", "what's left", "whats left", "what do i have", "fridge", "watchlist"}:
        return Intent("list")

    # Leftovers: "leftovers: chili x3", "made chili, 3 portions", "3 portions of chili left", "left chili 3"
    m = re.match(r"^(?:leftovers?|left)\s*:?\s+(.+)$", t)
    if m:
        name, n = _portions(m.group(1))
        if n is None:
            tail = re.match(r"^(.*?)\s+(\d+)$", name)
            if tail:
                name, n = tail.group(1), int(tail.group(2))
        return Intent("leftover", [name], n)
    if re.search(r"\b(?:portions?|servings?|bowls?|plates?)\b", t) or re.search(r"\bx\s*\d+\b", t):
        body = re.sub(r"^(?:made|cooked|i made|we made)\s+", "", t)
        name, n = _portions(body)
        if name and n:
            return Intent("leftover", [name], n)

    if re.match(r"^(?:made|cooked|did|done)(?:\s+(?:it|that|this|dinner|the recipe))?[.!]*$", t):
        return Intent("made")

    for pattern, kind in (
        (r"^(?:diet|cuisine|dietary)\b\s*:?\s*(.*)$", "diet"),
        (r"^(?:liked|loved|love|enjoyed)\b\s*:?\s*(?:the\s+)?(.*)$", "liked"),
        (r"^(?:dislike|don'?t like|do not like|hate)\b\s*:?\s*(?:the\s+)?(.*)$", "dislike"),
        (r"^(?:tossed|threw out|threw away|binned|trashed|composted|toss|bin)\b\s*(?:the\s+)?(.*)$", "tossed"),
        (r"^(?:still good|good|fine|ok|okay)\b\s*:?\s*(?:the\s+)?(.*)$", "good"),
        (r"^(?:used|ate|finished|used up|we ate|i ate)\b\s*(?:the\s+)?(.*)$", "used"),
        (r"^(?:bought|got|add|added|picked up|grabbed)\b\s*:?\s*(.*)$", "add"),
    ):
        m = re.match(pattern, t)
        if m:
            return Intent(kind, _items(m.group(1)))
    return Intent("unknown")
