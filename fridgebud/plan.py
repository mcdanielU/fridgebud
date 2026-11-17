"""The rescue plan: what to use first and a dinner for tonight, the moment food is added.

The daily watchlist only shows what is about to turn, so a new household would
hear nothing for days. This looks at everything tracked, soonest first, so the
first photo gets an answer in seconds. Food past its window is still never
cooked with, and nothing here says a food is safe.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from . import taste, recipes, watchlist
from .engine import Engine
from .models import Household, Item
from .recipes import Suggestion
from .watchlist import AT_RISK_DAYS, DROP_AFTER_DAYS, Entry

SHOW = 4


def entries(engine: Engine, household_id: int, today: date) -> list[Entry]:
    """Every tracked item with its days left, soonest first (not just the at-risk ones)."""
    out = []
    for item in engine.store.active_items(household_id):
        rule = watchlist.rule_for(item, engine.table)
        if rule is None:
            continue
        multiplier, _ = engine.store.get_rate(household_id, rule.category)
        left, age = watchlist.days_left(item, rule, multiplier, today)
        if left >= DROP_AFTER_DAYS:
            out.append(Entry(item, left, age))
    return sorted(out, key=lambda e: (e.days_left, e.item.id))


def by_when(days_left: float, today: date) -> str:
    days = max(0, math.floor(days_left))
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    day = today + timedelta(days=days)
    return f"{day:%a}" if days < 7 else f"{day:%b} {day.day}"


def use_first(all_entries: list[Entry], today: date) -> tuple[list[tuple[str, str]], int]:
    fresh = [e for e in all_entries if not e.past_window]
    return [(e.item.name, by_when(e.days_left, today)) for e in fresh[:SHOW]], max(0, len(fresh) - SHOW)


def tonight(engine: Engine, h: Household, today: date, avoid: str | None = None, meal: str = "dinner") -> Suggestion | None:
    """Pick an idea for `meal` (dinner by default) from what's on hand, favouring whatever turns soonest, and
    remember it so MADE works."""
    # Long-keeping items count as neutral, so a recipe isn't marked down for using carrots.
    ranked = [Entry(e.item, min(e.days_left, AT_RISK_DAYS + 1), e.age) for e in entries(engine, h.id, today)]
    pick = recipes.suggest(ranked, engine.store.active_items(h.id), h.dislikes, avoid=avoid, meal=meal,
                           **taste.context(engine.store, h, today))
    if pick:
        state = pick.as_state()
        state["check_ids"] = (h.last_suggestion or {}).get("check_ids", [])
        h.last_suggestion = state
        engine.store.save_household(h)
    return pick


def mark_old(engine: Engine, h: Household, items: list[Item], today: date) -> list[Item]:
    """Treat items as already near the end of their window ("use by tomorrow" at the latest)."""
    moved = []
    for item in items:
        rule = watchlist.rule_for(item, engine.table)
        if rule is None:
            continue
        multiplier, _ = engine.store.get_rate(h.id, rule.category)
        age = max(1, math.ceil(rule.min_days * multiplier - 1))
        added = today - timedelta(days=age)
        if added < item.added_on:
            engine.store.db.execute("UPDATE items SET added_on = ?, confirmed_good_on = NULL WHERE id = ?",
                                    (added.isoformat(), item.id))
            engine.store.db.commit()
            moved.append(item)
    return moved


def find(engine: Engine, items: list[Item], name: str) -> list[Item]:
    """Items that match what someone asked about: same food family, or the name is contained either way."""
    from .shelf_life import _singular, normalize
    target = normalize(name)
    if not target:
        return []
    singular = " ".join(_singular(w) for w in target.split())
    rule = engine.table.lookup(name)
    out = []
    for item in items:
        n = normalize(item.name)
        n_singular = " ".join(_singular(w) for w in n.split())
        if (rule and item.key == rule.key) or singular in n_singular or n_singular in singular:
            out.append(item)
    return out
