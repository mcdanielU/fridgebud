"""Shelf-life rules: how long a food is worth keeping in the fridge.

Times come from Health Canada's safe food storage chart where it lists the
food ("hc"), and from conservative quality estimates otherwise ("est").
FridgeBud never tells anyone a food is safe to eat; these numbers only decide
when to nudge.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

DATA_FILE = Path(__file__).parent / "data" / "shelf_life.json"

_QUANTITY = re.compile(
    r"^(?:\d+(?:\.\d+)?\s*(?:x|kg|g|lb|lbs|ml|l|oz|pack|packs|bag|bags|box)?\b\s*"
    r"|a\s+(?:bag|box|pack|bunch|head|carton|tub|container)\s+of\s+"
    r"|some\s+|a\s+|an\s+|the\s+|half\s+a\s+)",
)
_TRAILING_QTY = re.compile(r"\s*(?:x\s*\d+|\d+\s*x)$")
_NOISE = re.compile(r"\b(?:organic|fresh|large|small|medium|local|frozen|pc|no name|great value)\b")


@dataclass(frozen=True)
class Rule:
    key: str
    category: str
    min_days: float
    max_days: float
    source: str
    note: str = ""


def normalize(name: str) -> str:
    """Lowercase, strip quantities, brand-ish noise and punctuation."""
    text = name.lower().strip()
    text = re.sub(r"[^\w\s%']", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    previous = None
    while previous != text:
        previous = text
        text = _QUANTITY.sub("", text).strip()
    text = _TRAILING_QTY.sub("", text).strip()
    text = _NOISE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("oes"):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


class ShelfLifeTable:
    def __init__(self, path: Path = DATA_FILE):
        raw = json.loads(Path(path).read_text())
        self.source_url: str = raw["source_url"]
        self._by_alias: dict[str, Rule] = {}
        self.rules: dict[str, Rule] = {}
        for item in raw["items"]:
            low, high = item["use_within_days"]
            rule = Rule(item["key"], item["category"], low, high, item["source"], item.get("note", ""))
            self.rules[rule.key] = rule
            for alias in item["names"]:
                self._by_alias[normalize(alias)] = rule
        lo = raw["leftover"]
        self.leftover = Rule(lo["key"], lo["category"], *lo["use_within_days"], lo["source"], lo.get("note", ""))
        # Longest aliases first, so "baby spinach" wins over "spinach" and
        # "ground beef" wins over "beef".
        self._aliases_by_length = sorted(self._by_alias, key=len, reverse=True)

    def lookup(self, name: str) -> Rule | None:
        text = normalize(name)
        if not text:
            return None
        if text in self._by_alias:
            return self._by_alias[text]
        singular = " ".join(_singular(w) for w in text.split())
        if singular in self._by_alias:
            return self._by_alias[singular]
        padded = f" {text} "
        padded_singular = f" {singular} "
        for alias in self._aliases_by_length:
            needle = f" {alias} "
            if needle in padded or needle in padded_singular:
                return self._by_alias[alias]
        return None
