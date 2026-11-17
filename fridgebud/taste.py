"""What cuisines a household actually eats, learned from what they do, not just what they said once.

Every dish they ask for, save, cook or rate is a signal for its cuisine. Signals add up in persona["learned"];
once a cuisine passes LEARNED it counts like something they told Bud, and Bud says so once ("Looks like you've been into
West African food lately"), so they can see it learning (and correct it). Stated cuisines from a voice note or TASTE
count most. Picks lean toward the strongest; nothing is ever ruled out by a guess.
"""
from __future__ import annotations

import re
from datetime import date

LEARNED = 6.0          # signals before Bud says a cuisine is "theirs" (each lookup counts 1.5, so about 4 of them)
STATED = 3.0           # what they told Bud outright
BAR_MIN, BAR_MAX = 4.0, 14.0   # the bar adapts per home between these
FADE_PER_DAY = 0.97    # old signals fade (about a 3-week half-life), so taste can change
MIN_DAYS = 2           # a one-sitting binge is not a taste: signals must come from at least 2 different days

# Checked in this order, so "green curry" is Thai before "curry" is Indian and "curry goat" is Caribbean.
CUISINES: list[tuple[str, str]] = [
    ("West African", r"nigeri|ghana|west african|jollof|egusi|\befo\b|ogbono|suya|moi ?moi|pepper ?soup|fufu|\beba\b|"
                     r"garri|plantain|dodo|puff ?puff|akara|ofada|banga|okra soup|afang|edikang|asaro|yam porridge|"
                     r"waakye|kelewele|chin ?chin|nkwobi|abacha|ewedu|gbegiri|amala|tuwo|kilishi|egg sauce|"
                     r"senegal|thieboudienne|yassa|mafe|groundnut stew|light soup|banku|kenkey|red red|nkatenkwan"),
    ("Caribbean", r"caribbean|jamaica|\bjerk\b|oxtail|curry goat|rice and peas|ackee|trinidad|doubles|callaloo|"
                  r"\bpatt(y|ies)\b|escovitch|pelau|brown stew"),
    ("Thai", r"\bthai\b|pad thai|krapow|kra pao|pad kra|green curry|red curry|massaman|tom yum|tom kha|larb|som tam|"
             r"pad see ew|panang|khao"),
    ("Japanese", r"japan|ramen|teriyaki|sushi|katsu|miso|udon|donburi|gyoza|yakitori|onigiri|tonkatsu|karaage"),
    ("Korean", r"korea|bibimbap|kimchi|bulgogi|japchae|tteok|gochujang|kimbap|galbi"),
    ("Chinese", r"chinese|fried rice|lo mein|chow mein|kung pao|mapo|dumpling|szechuan|sichuan|sweet and sour|"
                r"char siu|\bbao\b|congee|beef and broccoli|general tso|orange chicken|egg foo"),
    ("Indian", r"india|biryani|tikka|masala|\bdal\b|daal|dhal|paneer|chana|\bcurry\b|korma|vindaloo|samosa|naan|"
               r"\broti\b|chapati|palak|saag|\baloo\b|tandoori|butter chicken|pakora|rajma|keema"),
    ("Mexican", r"mexic|\btaco|quesadilla|burrito|enchilada|fajita|tamale|pozole|chilaquiles|nacho|elote|carnitas|"
                r"birria|tostada|mole\b"),
    ("Middle Eastern", r"middle east|lebanese|shawarma|falafel|hummus|kebab|tabbouleh|shakshuka|fattoush|mujadara|"
                       r"kofta|turkish|persian|moroccan|tagine"),
    ("Italian", r"ital|pasta|risotto|lasagn|pizza|carbonara|bolognese|gnocchi|pesto|parmigiana|alfredo|spaghetti|"
                r"penne|frittata|minestrone"),
]
_COMPILED = [(name, re.compile(pattern, re.I)) for name, pattern in CUISINES]


def cuisine_of(text: str) -> str | None:
    for name, pattern in _COMPILED:
        if pattern.search(text or ""):
            return name
    return None


def normalize(stated: str) -> str | None:
    """'Nigerian food' -> 'West African'; 'thai' -> 'Thai'."""
    return cuisine_of(stated) or next((n for n, _ in CUISINES if n.lower() == (stated or "").strip().lower()), None)


def weights(persona: dict | None) -> dict[str, float]:
    p = persona or {}
    out: dict[str, float] = {}
    for c in p.get("cuisines") or []:
        name = normalize(str(c))
        if name:
            out[name] = max(out.get(name, 0.0), STATED)
    for name, score in (p.get("learned") or {}).items():
        out[name] = out.get(name, 0.0) + float(score)
    return out


def bar(persona: dict | None) -> float:
    """How much evidence Bud wants before saying a cuisine is "theirs". Adapts: rises when Bud has been
    wrong about this home, falls when it has been right."""
    return float((persona or {}).get("bar", LEARNED))


def _days(persona: dict | None, cuisine: str) -> int:
    return len(((persona or {}).get("days") or {}).get(cuisine) or [])


def learned(persona: dict | None) -> list[str]:
    """Cuisines Bud worked out on its own (not ones they stated), strongest first."""
    p = persona or {}
    stated = {normalize(str(c)) for c in p.get("cuisines") or []}
    scores = p.get("learned") or {}
    b = bar(p)
    return [c for c, s in sorted(scores.items(), key=lambda kv: -kv[1])
            if s >= b and _days(p, c) >= MIN_DAYS and c not in stated]


def learn(persona: dict | None, cuisine: str | None, weight: float, today=None) -> tuple[dict, bool]:
    """Add one signal. Returns (new persona, True if this cuisine just became one Bud should say it noticed).

    Adaptive in three ways: old signals fade with time (`today`), a cuisine needs signals from MIN_DAYS different
    days, and the evidence bar moves per home: a strong thumbs-down on something Bud already leans toward raises
    it (Bud was too eager), a strong yes lowers it (Bud reads this person well)."""
    p = dict(persona or {})
    if not cuisine:
        return p, False
    scores = dict(p.get("learned") or {})
    if today is not None:
        last = p.get("learned_on")
        if last:
            try:
                gap = max(0, (today - date.fromisoformat(last)).days)
            except ValueError:
                gap = 0
            if gap:
                scores = {c: round(v * FADE_PER_DAY ** gap, 2) if v > 0 else v for c, v in scores.items()}
        p["learned_on"] = today.isoformat()
    before = scores.get(cuisine, 0.0)
    b = bar(p)
    if before >= b and abs(weight) >= 1.5:
        p["bar"] = round(max(BAR_MIN, b * 0.9) if weight > 0 else min(BAR_MAX, b * 1.4), 2)
    after = max(-5.0, min(20.0, before + weight))
    scores[cuisine] = round(after, 2)
    p["learned"] = scores
    if weight > 0:
        days = {c: list(v) for c, v in (p.get("days") or {}).items()}
        mark = today.isoformat() if today is not None else "?"
        seen = days.setdefault(cuisine, [])
        if mark not in seen:
            seen.append(mark)
        days[cuisine] = seen[-10:]
        p["days"] = days
    stated = {normalize(str(c)) for c in p.get("cuisines") or []}
    told = list(p.get("announced") or [])
    new = (after >= bar(p) and _days(p, cuisine) >= MIN_DAYS and cuisine not in stated and cuisine not in told)
    if new:
        p["announced"] = told + [cuisine]
    return p, new


# How much newness they want: a fuzzy dial, 0 = comfort food, 1 = always something new -------------------------
VARIETY_START = 0.4          # mostly familiar until Bud learns otherwise


def variety(persona: dict | None) -> float:
    return float((persona or {}).get("variety", VARIETY_START))


def nudge_variety(persona: dict | None, target: float, rate: float) -> dict:
    """Move the dial a little toward what they just showed (never all the way: one night isn't a personality)."""
    p = dict(persona or {})
    v = variety(p)
    p["variety"] = round(max(0.05, min(0.95, v + rate * (target - v))), 3)
    return p


def _tri(x: float, a: float, b: float, c: float) -> float:
    if x <= a or x >= c:
        return 1.0 if (x <= a and a == b) or (x >= c and b == c) else 0.0
    return (x - a) / (b - a) if x < b else (c - x) / (c - b)


def describe(v: float) -> str:
    """Fuzzy, not a box: someone at 0.45 is 'mostly familiar, open to new', not just 'balanced'."""
    sets = {"comfort-first": _tri(v, 0.0, 0.0, 0.5), "a balance of familiar and new": _tri(v, 0.25, 0.5, 0.75),
            "adventurous": _tri(v, 0.5, 1.0, 1.0)}
    ranked = sorted(sets.items(), key=lambda kv: -kv[1])
    (top, a), (second, b) = ranked[0], ranked[1]
    if b > 0.25 and a - b < 0.35:
        lean = {"comfort-first": "leaning familiar", "adventurous": "leaning new",
                "a balance of familiar and new": "with room to explore"}[second]
        return f"{top}, {lean}" if top != "a balance of familiar and new" else f"a balance, {lean}"
    return top


def history(store, household_id: int, today) -> dict[str, int]:
    """Dishes they've cooked with Bud -> days since they last did."""
    from datetime import date as _date
    out: dict[str, int] = {}
    for e in store.events(household_id, "made"):
        name = str(e["payload"].get("recipe") or "").lower()
        if name:
            out[name] = (today - _date.fromisoformat(e["at"][:10])).days
    return out


def context(store, h, today) -> dict:
    """Everything a pick needs to know about this home's taste, ready to pass to recipes.suggest."""
    p = h.persona or {}
    return {"favourites": p.get("loved"), "taste": weights(p), "variety": variety(p),
            "history": history(store, h.id, today)}
