"""Meal picks (breakfast, brunch, lunch, dinner, snacks) from what is about to go to waste.

v0 is a small rule-based recipe bank so the loop works offline and costs
nothing. An LLM step can later rewrite the pick in the household's voice and
adapt it, but the choice of what to rescue stays here, in plain code.
Pantry staples (oil, salt, garlic, onion, rice, pasta, flour, spices) are
assumed to be on hand.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Item
from .watchlist import Entry


@dataclass(frozen=True)
class Recipe:
    name: str
    minutes: int
    needs: tuple[frozenset[str], ...]   # every group must be covered by something on hand
    uses: frozenset[str] = frozenset()  # optional extras it can soak up
    meals: frozenset[str] = frozenset({"dinner"})   # which meals it's a natural fit for
    cuisine: str = "Everyday"                        # see taste.py; picks lean toward the cuisines a home loves


MEALS = ("breakfast", "brunch", "lunch", "dinner", "snack")
MAINSTREAM = {"Everyday", "Italian", "Mexican", "Chinese"}   # fine for anyone before Bud knows their taste


def _r(name: str, minutes: int, needs: list[set[str]], uses: set[str] = frozenset(), meals: str = "dinner",
       cuisine: str = "") -> Recipe:
    return Recipe(name, minutes, tuple(frozenset(g) for g in needs), frozenset(uses), frozenset(meals.split()),
                  cuisine or _cuisine_of(name) or "Everyday")


from .taste import cuisine_of as _cuisine_of  # noqa: E402

PROTEIN = {"chicken", "beef", "pork", "tofu", "shrimp", "fish", "ground_meat", "sausage", "lamb"}
STIR_VEG = {"broccoli", "peppers", "kale", "green_beans", "mushrooms", "zucchini", "carrots", "spinach", "celery"}

BANK: list[Recipe] = [
    _r("Garlic spinach pasta", 15, [{"spinach", "kale"}], {"mushrooms", "firm_cheese", "soft_cheese", "herbs", "tomatoes", "sour_cream"}, meals="lunch dinner"),
    _r("Sheet-pan chicken and veg", 30, [{"chicken"}, {"peppers", "broccoli", "zucchini", "green_beans", "carrots", "mushrooms"}], {"herbs"}),
    _r("Stir-fry over rice", 20, [PROTEIN, STIR_VEG], {"herbs", "eggs"}, meals="lunch dinner"),
    _r("Veggie frittata", 20, [{"eggs"}, {"spinach", "peppers", "mushrooms", "zucchini", "kale", "broccoli", "herbs", "tomatoes"}],
       {"firm_cheese", "soft_cheese", "bacon", "deli_meat", "cooked_ham", "milk"}, meals="breakfast brunch lunch dinner"),
    _r("Tacos", 20, [{"ground_meat", "fish", "chicken", "beef", "shrimp"}, {"tortillas"}],
       {"leaf_lettuce", "tomatoes", "avocado", "sour_cream", "herbs", "firm_cheese", "peppers"}, meals="lunch dinner"),
    _r("Quesadillas", 10, [{"tortillas"}, {"firm_cheese", "soft_cheese"}],
       {"peppers", "spinach", "mushrooms", "chicken", "herbs", "avocado", "sour_cream"}, meals="lunch dinner snack"),
    _r("Big chopped salad", 10, [{"leaf_lettuce", "spinach", "kale"}],
       {"cucumber", "tomatoes", "avocado", "soft_cheese", "peppers", "herbs", "eggs", "chicken", "deli_meat"}, meals="lunch dinner"),
    _r("Smoothie or yogurt bowl", 5, [{"berries", "bananas", "yogurt"}], {"milk", "spinach", "kale", "berries", "bananas"}, meals="breakfast brunch snack"),
    _r("Overnight oats", 5, [{"milk", "yogurt"}], {"berries", "bananas"}, meals="breakfast"),
    _r("Clean-out-the-crisper soup", 35, [{"carrots", "celery", "kale", "zucchini", "broccoli", "mushrooms", "green_beans", "spinach", "tomatoes"}],
       {"chicken", "sausage", "herbs", "sour_cream"}, meals="lunch dinner"),
    _r("Baked pasta", 30, [{"ground_meat", "sausage", "beef"}, {"tomatoes", "spinach", "zucchini", "mushrooms", "peppers"}],
       {"firm_cheese", "soft_cheese", "cottage_cheese", "herbs"}),
    _r("Salmon or fish with greens", 20, [{"fish"}, {"green_beans", "broccoli", "spinach", "kale", "zucchini"}], {"herbs", "yogurt"}),
    _r("French toast", 15, [{"bread"}, {"eggs"}], {"milk", "berries", "bananas"}, meals="breakfast brunch"),
    _r("Grilled cheese and tomato soup", 15, [{"bread"}, {"firm_cheese", "soft_cheese"}], {"tomatoes", "deli_meat", "cooked_ham"}, meals="lunch dinner"),
    _r("Garlic shrimp pasta", 15, [{"shrimp"}], {"spinach", "herbs", "tomatoes", "sour_cream"}),
    _r("Breakfast-for-dinner hash", 20, [{"bacon", "sausage", "cooked_ham"}, {"eggs"}], {"peppers", "mushrooms", "spinach", "herbs"}, meals="brunch dinner"),
    _r("Quick curry", 30, [{"chicken", "tofu", "shrimp"}, {"spinach", "peppers", "zucchini", "broccoli", "kale", "tomatoes"}],
       {"yogurt", "sour_cream", "herbs"}),
    _r("Loaded sandwiches or wraps", 10, [{"bread", "tortillas"}, {"deli_meat", "cooked_ham", "chicken"}],
       {"leaf_lettuce", "tomatoes", "cucumber", "avocado", "firm_cheese", "hummus"}, meals="lunch"),
    # around the world: real dishes from tracked fridge food (rice, onions, spices and oils are pantry)
    _r("Jollof rice with chicken", 50, [{"chicken", "beef", "fish", "shrimp"}, {"tomatoes", "peppers"}], {"carrots", "green_beans"},
       meals="lunch dinner", cuisine="West African"),
    _r("Efo riro (spinach stew) with rice", 35, [{"spinach", "kale"}], {"beef", "chicken", "fish", "shrimp", "peppers", "tomatoes"},
       meals="lunch dinner", cuisine="West African"),
    _r("Nigerian pepper soup", 40, [{"chicken", "fish", "beef", "lamb"}], {"herbs", "peppers"}, cuisine="West African"),
    _r("Nigerian fried rice", 35, [{"carrots", "green_beans", "peppers"}], {"chicken", "shrimp", "beef", "eggs"},
       meals="lunch dinner", cuisine="West African"),
    _r("Suya-spiced chicken or beef with salad", 25, [{"beef", "chicken"}], {"tomatoes", "cucumber", "peppers", "leaf_lettuce"}, cuisine="West African"),
    _r("Nigerian egg sauce with bread", 15, [{"eggs"}, {"tomatoes", "peppers"}], {"bread", "spinach"},
       meals="breakfast brunch dinner", cuisine="West African"),
    _r("Jerk chicken with rice and peas", 45, [{"chicken", "pork"}], {"peppers", "herbs"}, cuisine="Caribbean"),
    _r("Thai basil stir-fry", 15, [{"chicken", "ground_meat", "pork", "shrimp", "beef"}],
       {"herbs", "peppers", "green_beans", "eggs"}, meals="lunch dinner", cuisine="Thai"),
    _r("Thai green curry", 30, [PROTEIN, {"zucchini", "peppers", "green_beans", "broccoli", "mushrooms", "spinach"}], {"herbs"},
       cuisine="Thai"),
    _r("Chicken tikka masala", 35, [{"chicken"}, {"tomatoes"}], {"yogurt", "sour_cream", "herbs", "peppers"}, cuisine="Indian"),
    _r("Palak dal (spinach lentils)", 30, [{"spinach", "kale"}], {"tomatoes", "yogurt", "herbs"}, meals="lunch dinner", cuisine="Indian"),
    _r("Egg curry", 25, [{"eggs"}, {"tomatoes"}], {"spinach", "peppers", "herbs"}, cuisine="Indian"),
    _r("Egg fried rice", 15, [{"eggs"}], {"peppers", "carrots", "shrimp", "chicken", "herbs", "green_beans", "mushrooms"},
       meals="lunch dinner", cuisine="Chinese"),
    _r("Beef and broccoli", 20, [{"beef"}, {"broccoli"}], {"peppers", "mushrooms", "carrots"}, cuisine="Chinese"),
    _r("Bibimbap", 30, [{"eggs"}, {"spinach", "carrots", "zucchini", "mushrooms"}], {"beef", "ground_meat", "cucumber"},
       meals="lunch dinner", cuisine="Korean"),
    _r("Teriyaki chicken or salmon", 25, [{"chicken", "fish"}], {"broccoli", "green_beans", "peppers"}, cuisine="Japanese"),
    _r("Chicken fajitas", 25, [{"chicken", "beef", "shrimp"}, {"peppers"}, {"tortillas"}], {"sour_cream", "avocado", "firm_cheese"},
       cuisine="Mexican"),
    _r("Chicken shawarma bowl", 30, [{"chicken", "lamb", "beef"}], {"cucumber", "tomatoes", "yogurt", "leaf_lettuce", "hummus"},
       meals="lunch dinner", cuisine="Middle Eastern"),
    # breakfast and brunch
    _r("Scrambled eggs on toast", 10, [{"eggs"}, {"bread"}], {"herbs", "firm_cheese", "tomatoes", "spinach", "mushrooms", "avocado"},
       meals="breakfast brunch"),
    _r("Avocado toast", 10, [{"avocado"}, {"bread"}], {"eggs", "tomatoes", "herbs", "soft_cheese"}, meals="breakfast brunch snack"),
    _r("Shakshuka", 25, [{"eggs"}, {"tomatoes", "peppers"}], {"herbs", "soft_cheese", "spinach"}, meals="breakfast brunch"),
    _r("Breakfast tacos", 15, [{"eggs"}, {"tortillas"}], {"peppers", "avocado", "firm_cheese", "bacon", "sausage", "herbs", "tomatoes"},
       meals="breakfast brunch"),
    _r("Omelette", 10, [{"eggs"}], {"spinach", "mushrooms", "peppers", "firm_cheese", "herbs", "tomatoes", "deli_meat", "cooked_ham"},
       meals="breakfast brunch lunch"),
    # lunch
    _r("Grain bowl", 20, [PROTEIN | {"eggs"}, {"spinach", "kale", "leaf_lettuce", "cucumber", "tomatoes", "peppers", "carrots", "broccoli"}],
       {"avocado", "herbs", "hummus", "yogurt"}, meals="lunch"),
    # snacks
    _r("Hummus and veg sticks", 5, [{"hummus"}, {"carrots", "cucumber", "celery", "peppers"}], meals="snack"),
    _r("Yogurt with fruit", 2, [{"yogurt"}, {"berries", "bananas", "grapes"}], meals="snack breakfast"),
    _r("Cheese and fruit plate", 5, [{"firm_cheese", "soft_cheese"}, {"grapes", "berries"}], {"deli_meat"}, meals="snack"),
    _r("Fruit salad", 5, [{"berries", "bananas", "grapes"}], {"yogurt", "herbs"}, meals="snack breakfast brunch"),
    _r("Cottage cheese bowl", 3, [{"cottage_cheese"}], {"berries", "bananas", "tomatoes", "cucumber"}, meals="snack breakfast"),
]


@dataclass
class Suggestion:
    title: str
    minutes: int | None
    rescues: list[Item]                     # at-risk items this dinner uses up
    breakfast: "Suggestion | None" = None   # optional extra for fruit/dairy a lunch or dinner can't use
    meal: str = "dinner"
    novelty: float = 0.5                    # 0 = a dish they know and love, 1 = a cuisine they've never touched

    def as_state(self) -> dict:
        # MADE closes only the dinner's items; breakfast items stay until USED.
        return {"recipe": self.title, "novelty": self.novelty, "item_ids": [i.id for i in self.rescues],
                "breakfast_ids": [i.id for i in self.breakfast.rescues] if self.breakfast else []}


def _best(meal: str, usable: list[Entry], on_hand: list[Item], disliked: set[str],
          exclude: set[int] = frozenset(), avoid: str | None = None, any_day: bool = False,
          favourites: frozenset = frozenset(), taste: dict | None = None, variety: float | None = None,
          history: dict | None = None) -> Suggestion | None:
    """any_day: on a day nothing is at risk, still pick something their fridge can make (the daily habit needs
    a daily pick). Favourites they rated 🔥 get a nudge up the order."""
    at_risk = {e.item.key: e for e in usable if e.item.key and e.item.id not in exclude}
    have = {i.key for i in on_hand if i.key}
    skip = {avoid.lower()} if isinstance(avoid, str) else {str(a).lower() for a in (avoid or ())}
    best: tuple[float, Recipe, list[Item]] | None = None
    for recipe in BANK:
        if meal not in recipe.meals or any(d in recipe.name.lower() for d in disliked):
            continue
        if not all(group & have for group in recipe.needs):
            continue
        keys = set().union(*recipe.needs) | recipe.uses
        rescued = [at_risk[k] for k in keys if k in at_risk]
        if rescued:
            if any(e.item.key in disliked or e.item.name.lower() in disliked for e in rescued):
                continue
            score = sum(e.urgency for e in rescued) - recipe.minutes / 60
            items = [e.item for e in sorted(rescued, key=lambda e: e.days_left)]
        elif any_day:
            items = [i for i in on_hand if i.key in keys and i.id not in exclude]
            if not items or any(i.key in disliked or i.name.lower() in disliked for i in items):
                continue
            score = 0.1 * len(items) - recipe.minutes / 60 - 5   # always ranks below anything that rescues food
        else:
            continue
        if recipe.name.lower() in skip:
            score -= 100  # they asked for another idea (or ignored it yesterday): anything else beats it, but it
                          # is still the answer if it is the only thing their fridge can make
        if recipe.name.lower() in favourites:
            score += 0.6
        fam = familiarity(recipe, taste, favourites, history)
        if variety is not None:
            score += 1.5 * (variety - 0.5) * (1 - 2 * fam)   # comfort cooks lean familiar, adventurous ones lean new
        if history and recipe.name.lower() in history:
            window = 2 + round(5 * (variety if variety is not None else 0.5))   # ~3 days for comfort, ~a week for adventurous
            if history[recipe.name.lower()] < window:
                score -= 1.0
        fit = (taste or {}).get(recipe.cuisine, 0.0)
        score += 0.4 * max(-3.0, min(fit, 6.0))         # up to +2.4 for a cuisine they love, down for one they don't
        if fit <= 0 and recipe.cuisine not in MAINSTREAM:  # an unfamiliar cuisine leads only for the adventurous
            score -= 0.4 if variety is None else 0.8 * max(0.0, 0.75 - variety)
        if best is None or score > best[0]:
            best = (score, recipe, items, fam)
    if best is None:
        return None
    _, recipe, items, fam = best
    return Suggestion(recipe.name, recipe.minutes, items, meal=meal, novelty=round(1 - fam, 2))


def familiarity(recipe: Recipe, taste: dict | None, favourites, history: dict | None) -> float:
    """How well they know this dish: 1 a favourite or something they've cooked, ~0.75 a cuisine they love,
    0.6 an everyday dish, 0.25 a cuisine they've never touched, 0 one they keep turning down."""
    name = recipe.name.lower()
    if name in (favourites or ()) or (history and name in history):
        return 1.0
    w = (taste or {}).get(recipe.cuisine, 0.0)
    if w < 0:
        return 0.0
    if w >= 2:
        return 0.75
    return 0.6 if recipe.cuisine in MAINSTREAM else 0.25


def suggest(watchlist: list[Entry], on_hand: list[Item], dislikes: list[str],
            avoid: str | None = None, meal: str = "dinner", any_day: bool = False,
            favourites: list[str] | None = None, taste: dict | None = None, variety: float | None = None,
            history: dict | None = None) -> Suggestion | None:
    """Pick an idea for `meal` (dinner by default). For lunch and dinner, add a breakfast idea if fruit or dairy
    would otherwise be missed; asked for breakfast, brunch or a snack, give just that.

    Leftovers come first: zero cooking beats any recipe. Items past their
    window are never cooked with.
    """
    usable = [e for e in watchlist if not e.past_window]
    past = {e.item.id for e in watchlist if e.past_window}
    on_hand = [i for i in on_hand if i.id not in past]  # never cook with past-window food
    disliked = {d.lower() for d in dislikes}
    faves = frozenset(f.lower() for f in favourites or [])
    if meal not in ("lunch", "dinner"):
        return _best(meal, usable, on_hand, disliked, avoid=avoid, any_day=any_day, favourites=faves, taste=taste,
                     variety=variety, history=history)
    leftovers = [e for e in usable if e.item.kind == "leftover"]
    if leftovers:
        e = leftovers[0]
        dinner = Suggestion(f"Finish the {e.item.name}" if meal == "lunch" else f"Leftover night: finish the {e.item.name}",
                            None, [e.item], meal=meal)
    else:
        dinner = _best(meal, usable, on_hand, disliked, avoid=avoid, any_day=any_day, favourites=faves, taste=taste,
                     variety=variety, history=history)
    taken = {i.id for i in dinner.rescues} if dinner else set()
    breakfast = _best("breakfast", usable, on_hand, disliked, exclude=taken, taste=taste, variety=variety, history=history)
    if dinner is None:
        return breakfast
    dinner.breakfast = breakfast
    return dinner


def cuisine_for(title: str) -> str | None:
    """A dish's cuisine: the bank's tag if it's one of ours, else read from the name."""
    hit = next((r for r in BANK if r.name.lower() == (title or "").lower()), None)
    if hit is not None:
        return hit.cuisine if hit.cuisine != "Everyday" else None
    return _cuisine_of(title or "")
