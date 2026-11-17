"""The daily loop that makes Bud part of someone's evening (Nir Eyal's Hooked model, used for the user's benefit).

    trigger     one pick at the moment "what's for dinner?" starts nagging (about 2 hours before their main meal),
                every day the fridge can make something, not only on days something is about to go off
    action      a one-tap reply: 👍 locks it in, 👎 swaps it. No app, no menu, no planning a week ahead
    reward      MADE pays out something different each time: what was rescued and roughly what it was worth, the
                month's running total, this week's home-cooked count, a milestone now and then, a peek at tomorrow
    investment  a one-tap rating (🔥 / 👌 / 👎) that changes the next picks: favourites come back, flops never do

Rules that keep it on the right side of persuasion: no guilt or shame (a tossed item is a shrug, not a scolding),
no fake urgency, never more than two texts a day, quiet hours respected, STOP always works, and every dollar figure
that is an estimate says so.
"""
from __future__ import annotations

import random
import re
from datetime import date

# Rough Canadian shelf prices for what usually gets rescued: an estimate, always shown with "≈".
_PRICES = [
    (r"salmon|steak|shrimp|prawn|lamb|goat|tilapia|cod|fish", 13.0),
    (r"chicken|beef|pork|turkey|sausage|bacon|mince", 11.0),
    (r"berr|cherries", 6.0),
    (r"cheese|butter|cream|yogh?urt|milk|kefir|paneer|tofu|hummus", 5.0),
    (r"egg", 5.0),
    (r"spinach|kale|lettuce|salad|greens|arugula|herb|basil|cilantro|parsley|mushroom|asparagus|broccoli|cauliflower", 4.0),
    (r"tomato|pepper|zucchini|cucumber|eggplant|aubergine|green bean|okra|bok choy|cabbage|celery|leek", 3.0),
    (r"bread|tortilla|wrap|pita|naan|bagel|bun", 4.0),
    (r"avocado|mango|pineapple|melon|grape", 3.0),
    (r"potato|carrot|onion|apple|orange|pear|banana|lemon|lime|plantain|yam", 2.0),
]
LEFTOVER_PORTION = 4.0
DOLLAR_MARKS = (10, 25, 50, 100, 250, 500, 1000)
MEAL_MARKS = (1, 5, 10, 25, 50, 100)

_CHEERS = ("Chef's kiss 👨‍🍳", "Look at you go 🙌", "Nailed it 🍳", "That's how it's done 💪", "Kitchen hero 🦸",
           "Love that 😋", "Big win 🏆", "Smooth 😎")

LOVE = {"🔥", "❤", "❤️", "😍", "🤤", "🤩", "loved it", "loved", "love it", "amazing", "so good", "delicious", "fire",
        "10/10", "5/5", "banger", "it was amazing", "it was so good", "really good", "great"}
FINE = {"👌", "👍", "ok", "okay", "fine", "good", "decent", "not bad", "alright", "it was ok", "it was okay",
        "it was good", "it was fine", "pretty good"}
NOPE = {"👎", "not again", "meh", "bad", "nope", "didn't like it", "didnt like it", "hated it", "not great", "never again",
        "it was bad", "yuck", "🤢"}
LOCK = {"👍", "yes", "y", "yep", "yeah", "ok", "okay", "sure", "deal", "lock it in", "lock it", "let's do it",
        "cook it", "cook this", "i'll cook it", "ill cook it", "i'll make it", "ill make it", "make it", "this one",
        "sounds great", "yum", "yes please", "thumbs up",
        "lets do it", "sounds good", "perfect", "on it", "do it", "i'm in", "im in", "👌", "✅", "🔒", "bet"}
SWAP = {"👎", "nah", "nope", "not feeling it", "not tonight", "something else", "swap", "swap it", "🔄", "↻", "pass",
        "another", "another one", "another idea", "different", "something different", "a different idea", "no",
        "thumbs down", "not this", "not that"}


def clean(text: str) -> str:
    """'👍🏾', 'Yes!!', ' lock it in. ' -> '👍', 'yes', 'lock it in'"""
    t = re.sub(r"[\U0001F3FB-\U0001F3FF️‍]", "", text or "").strip().lower()
    return re.sub(r"[\s.!?,]+$", "", t)


def value(name: str, price: float | None = None, kind: str = "grocery", portions: int | None = None) -> float:
    if price:
        return float(price)
    if kind == "leftover":
        return LEFTOVER_PORTION * (portions or 1)
    low = name.lower()
    return next((p for pattern, p in _PRICES if re.search(pattern, low)), 2.5)


def month_saved(closed: list) -> float:
    """Everything they used up this month (real receipt prices where Bud has them, estimates otherwise)."""
    return round(sum(value(i.name, i.price, i.kind, i.portions) for i in closed if i.status == "used"), 2)


def crossed(before: float, after: float, marks: tuple) -> int | None:
    hit = [m for m in marks if before < m <= after]
    return hit[-1] if hit else None


def cheer(seed: int) -> str:
    return random.Random(seed).choice(_CHEERS)


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def seed_for(household_id: int, day: date, n: int) -> int:
    return household_id * 100_003 + day.toordinal() * 31 + n
