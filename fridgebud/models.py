from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class Household:
    id: int
    phone: str
    name: str = ""
    timezone: str = "America/Toronto"
    nudge_hour: int = 16
    nudge_minute: int = 30
    dislikes: list[str] = field(default_factory=list)
    diet_notes: str = ""
    persona: dict = field(default_factory=dict)   # likes, cuisines, meals {breakfast: "07:30"...}, main_meal
    opted_in: bool = True
    last_nudge_on: date | None = None
    last_suggestion: dict | None = None  # {"recipe": str, "item_ids": [int]}


@dataclass
class Item:
    id: int
    household_id: int
    name: str
    key: str | None          # shelf-life rule key; None = not tracked
    category: str | None
    kind: str                # "grocery" or "leftover"
    added_on: date
    portions: int | None = None
    status: str = "active"   # active | used | tossed
    closed_on: date | None = None
    confirmed_good_on: date | None = None  # last "still good" reply
    price: float | None = None             # from a receipt or order email; drives "$ kept"
