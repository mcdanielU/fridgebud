"""The watchlist: the few items most likely to be wasted, plus leftovers.

It is deliberately not an inventory. Accuracy only matters for what is at
risk this week, and every reply teaches the household's spoil rates.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .models import Item
from .shelf_life import Rule, ShelfLifeTable
from .store import Store

AT_RISK_DAYS = 2.0       # show an item once it has this many days or fewer left
CHECK_IT_DAYS = 0.0      # past its window: never cook with it, just ask "still good or tossed?"
DROP_AFTER_DAYS = -3.0   # stop asking after this; assume it's gone
MAX_ITEMS = 12
MIN_MULT, MAX_MULT = 0.5, 2.0


@dataclass
class Entry:
    item: Item
    days_left: float
    age: int

    @property
    def past_window(self) -> bool:
        return self.days_left < CHECK_IT_DAYS

    @property
    def label(self) -> str:
        if self.past_window:
            return "worth checking"
        if self.days_left < 1:
            return "use today"
        if self.days_left < 2:
            return "use by tomorrow"
        return "use in 2 days"

    @property
    def urgency(self) -> float:
        """Higher = more urgent. Items past their window score zero for meals."""
        if self.past_window:
            return 0.0
        return AT_RISK_DAYS + 1 - self.days_left


def rule_for(item: Item, table: ShelfLifeTable) -> Rule | None:
    if item.kind == "leftover":
        return table.leftover
    return table.rules.get(item.key) if item.key else None


def days_left(item: Item, rule: Rule, multiplier: float, today: date) -> tuple[float, int]:
    age = (today - item.added_on).days
    window = rule.min_days * multiplier
    if item.confirmed_good_on:
        # A "still good" reply buys a little more time from that day.
        since_confirm = (today - item.confirmed_good_on).days
        return max(window - age, 2.0 - since_confirm), age
    return window - age, age


def build(store: Store, table: ShelfLifeTable, household_id: int, today: date) -> list[Entry]:
    entries: list[Entry] = []
    for item in store.active_items(household_id):
        rule = rule_for(item, table)
        if rule is None:
            continue  # untracked item (pantry staple or unknown)
        multiplier, _ = store.get_rate(household_id, rule.category)
        left, age = days_left(item, rule, multiplier, today)
        # Leftovers get one quiet day before the nudges start.
        threshold = 1.0 if item.kind == "leftover" else AT_RISK_DAYS
        if DROP_AFTER_DAYS <= left <= threshold:
            entries.append(Entry(item, left, age))
    entries.sort(key=lambda e: (e.days_left, e.item.id))
    return entries[:MAX_ITEMS]


def learn_from_reply(store: Store, table: ShelfLifeTable, item: Item, outcome: str, today: date) -> None:
    """Update this household's spoil rate for the item's category.

    tossed     -> it spoiled at about this age
    still_good -> it lasts longer than we predicted (only informative once due)
    used       -> no information about spoiling
    """
    rule = rule_for(item, table)
    if rule is None or outcome == "used":
        return
    multiplier, n = store.get_rate(item.household_id, rule.category)
    age = max(1, (today - item.added_on).days)
    if outcome == "tossed":
        target = age / rule.min_days
    elif outcome == "still_good":
        left, _ = days_left(item, rule, multiplier, today)
        if left > 0:
            return
        target = (age + 1) / rule.min_days
    else:
        return
    alpha = 1.0 / (n + 2)  # move fast early, settle as evidence builds
    updated = min(MAX_MULT, max(MIN_MULT, multiplier + alpha * (target - multiplier)))
    store.set_rate(item.household_id, rule.category, round(updated, 3), n + 1)
