"""The household's recipe book and shopping list.

Recipes come from three places and each one is labelled so the household
always knows which it is looking at:
    link    saved from a web page or video link they sent
    pasted  typed or pasted by them
    video   a video file they sent, watched by a video model
    ai      written by the AI because nothing saved matched
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from .models import Item
from .shelf_life import _singular, normalize
from .store import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS saved_recipes (
    id INTEGER PRIMARY KEY,
    household_id INTEGER NOT NULL REFERENCES households(id),
    title TEXT NOT NULL,
    source TEXT NOT NULL,          -- link | pasted | video | ai
    url TEXT DEFAULT '',
    site TEXT DEFAULT '',
    servings TEXT DEFAULT '',
    minutes INTEGER,
    ingredients TEXT NOT NULL,     -- JSON [{name, quantity, staple}]
    steps TEXT NOT NULL,           -- JSON [str]
    note TEXT DEFAULT '',          -- anything the household should know about where it came from
    created_at TEXT NOT NULL,
    confirmed INTEGER DEFAULT 0    -- an AI-written recipe the household hasn't said LIKED to yet is 0
);
CREATE TABLE IF NOT EXISTS current_recipe (
    household_id INTEGER PRIMARY KEY,
    recipe_id INTEGER NOT NULL
);
-- Recipe screenshots in progress: several shots of one recipe are merged.
CREATE TABLE IF NOT EXISTS recipe_shots (
    household_id INTEGER PRIMARY KEY,
    text TEXT NOT NULL DEFAULT '',
    recipe_id INTEGER,
    expecting_until TEXT
);
-- A menu someone sent, waiting for them to say which dish they meant.
CREATE TABLE IF NOT EXISTS menu_choices (
    household_id INTEGER PRIMARY KEY,
    place TEXT NOT NULL DEFAULT '',
    dishes TEXT NOT NULL,
    until TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS list_links (
    household_id INTEGER PRIMARY KEY,
    token TEXT NOT NULL UNIQUE     -- the unguessable part of the shopping-list export page's address
);
CREATE TABLE IF NOT EXISTS shopping (
    id INTEGER PRIMARY KEY,
    household_id INTEGER NOT NULL REFERENCES households(id),
    name TEXT NOT NULL,
    added_at TEXT NOT NULL,
    bought_at TEXT
);
"""

_FILLER = {"recipe", "recipes", "the", "a", "an", "my", "for", "of", "with", "and", "some", "saved", "how", "to", "make",
           "delicacy", "delicacies", "dish", "meal", "food", "idea", "something"}
# Words that turn one food into a different product: "tomato paste" is not "tomatoes".
_CHANGES_FOOD = {"stock", "broth", "sauce", "paste", "powder", "vinegar", "oil", "juice", "seasoning", "cube", "cubes",
                 "flake", "flakes", "extract", "butter", "milk", "cream", "flour", "noodle", "noodles", "chips", "puree",
                 "syrup", "jam", "dried", "canned", "tinned", "frozen", "smoked", "crumbs", "wrapper", "wrappers"}
STAPLES = {"salt", "pepper", "black pepper", "water", "oil", "olive oil", "vegetable oil", "cooking oil", "sugar"}


@dataclass
class Ingredient:
    name: str
    quantity: str = ""
    staple: bool = False


@dataclass
class Recipe:
    household_id: int
    title: str
    source: str
    ingredients: list[Ingredient]
    steps: list[str]
    url: str = ""
    site: str = ""
    servings: str = ""
    minutes: int | None = None
    note: str = ""
    id: int | None = None
    created_at: datetime | None = None
    confirmed: bool = False


def _tokens(text: str) -> set[str]:
    return {_singular(w) for w in re.findall(r"[a-z]+", normalize(text))} - _FILLER


def same_food(ingredient: str, item_name: str) -> bool:
    """Deterministic check that a fridge item covers an ingredient. Careful rather than generous."""
    a, b = _tokens(ingredient), _tokens(item_name)
    if not a or not b or not (a <= b or b <= a):
        return False
    return not ((a ^ b) & _CHANGES_FOOD)


def match_local(ingredients: list[Ingredient], items: list[Item]) -> list[Item | None]:
    return [next((i for i in items if same_food(ing.name, i.name)), None) for ing in ingredients]


def recipe_from_dict(data: dict, household_id: int, source: str, url: str = "", site: str = "", note: str = "") -> Recipe | None:
    """Validate a recipe that came from a model or a web page. Returns None if it isn't usable."""
    if not isinstance(data, dict):
        return None
    title = str(data.get("title") or "").strip()[:120]
    ingredients = []
    for raw in (data.get("ingredients") or [])[:40]:
        if isinstance(raw, str):
            raw = {"name": raw}
        name = str((raw or {}).get("name") or "").strip()[:80]
        if name:
            staple = bool(raw.get("staple")) or normalize(name) in STAPLES
            ingredients.append(Ingredient(name, str(raw.get("quantity") or "").strip()[:40], staple))
    steps = [str(s).strip()[:700] for s in (data.get("steps") or [])[:30] if str(s).strip()]
    if not title or len(ingredients) < 2 or not steps:
        return None
    try:
        minutes = int(data.get("minutes")) if data.get("minutes") else None
    except (TypeError, ValueError):
        minutes = None
    return Recipe(household_id, title, source, ingredients, steps, url=url[:500], site=site[:80],
                  servings=str(data.get("servings") or "").strip()[:30], minutes=minutes, note=note)


class RecipeBook:
    def __init__(self, store: Store):
        self.db = store.db
        self.db.executescript(SCHEMA)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(saved_recipes)")}
        if "confirmed" not in cols:  # recipe books created before the LIKED/keep-it flow
            self.db.execute("ALTER TABLE saved_recipes ADD COLUMN confirmed INTEGER DEFAULT 0")
            self.db.execute("UPDATE saved_recipes SET confirmed = 1 WHERE source != 'ai'")
            self.db.commit()

    # recipes ----------------------------------------------------------
    def save(self, r: Recipe, now: datetime) -> Recipe:
        cur = self.db.execute(
            "INSERT INTO saved_recipes (household_id, title, source, url, site, servings, minutes, ingredients, steps, note, created_at, confirmed)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (r.household_id, r.title, r.source, r.url, r.site, r.servings, r.minutes,
             json.dumps([vars(i) for i in r.ingredients]), json.dumps(r.steps), r.note, now.isoformat(),
             0 if r.source == "ai" else 1))
        self.db.commit()
        return self.get(cur.lastrowid)

    def kept(self, household_id: int) -> list["Recipe"]:
        """Saved recipes minus AI ones the household hasn't confirmed they want to keep yet."""
        rows = self.db.execute("SELECT * FROM saved_recipes WHERE household_id = ? AND confirmed = 1 ORDER BY id DESC", (household_id,))
        return [self._recipe(r) for r in rows]

    def confirm(self, recipe_id: int) -> None:
        self.db.execute("UPDATE saved_recipes SET confirmed = 1 WHERE id = ?", (recipe_id,))
        self.db.commit()

    def get(self, recipe_id: int) -> Recipe | None:
        row = self.db.execute("SELECT * FROM saved_recipes WHERE id = ?", (recipe_id,)).fetchone()
        return self._recipe(row) if row else None

    def all(self, household_id: int) -> list[Recipe]:
        rows = self.db.execute("SELECT * FROM saved_recipes WHERE household_id = ? ORDER BY id DESC", (household_id,))
        return [self._recipe(r) for r in rows]

    def by_url(self, household_id: int, url: str) -> Recipe | None:
        row = self.db.execute("SELECT * FROM saved_recipes WHERE household_id = ? AND url = ? ORDER BY id DESC",
                              (household_id, url)).fetchone()
        return self._recipe(row) if row else None

    def search(self, household_id: int, query: str) -> Recipe | None:
        """Best saved recipe for a dish name. Links and pasted recipes win over AI-written ones."""
        wanted = _tokens(query)
        if not wanted:
            return None
        best: tuple[float, Recipe] | None = None
        for r in self.all(household_id):
            overlap = len(wanted & _tokens(r.title)) / len(wanted)
            if overlap < 0.6:
                continue
            score = overlap + (0.2 if r.source != "ai" else 0)
            if best is None or score > best[0]:
                best = (score, r)
        return best[1] if best else None

    def set_current(self, household_id: int, recipe_id: int) -> None:
        self.db.execute("INSERT INTO current_recipe (household_id, recipe_id) VALUES (?,?) "
                        "ON CONFLICT(household_id) DO UPDATE SET recipe_id = excluded.recipe_id", (household_id, recipe_id))
        self.db.commit()

    def current(self, household_id: int) -> Recipe | None:
        row = self.db.execute("SELECT recipe_id FROM current_recipe WHERE household_id = ?", (household_id,)).fetchone()
        return self.get(row["recipe_id"]) if row else None

    @staticmethod
    def _recipe(row) -> Recipe:
        return Recipe(
            household_id=row["household_id"], title=row["title"], source=row["source"],
            ingredients=[Ingredient(**i) for i in json.loads(row["ingredients"])], steps=json.loads(row["steps"]),
            url=row["url"], site=row["site"], servings=row["servings"], minutes=row["minutes"], note=row["note"],
            id=row["id"], created_at=datetime.fromisoformat(row["created_at"]), confirmed=bool(row["confirmed"]))

    def delete(self, recipe_id: int) -> None:
        self.db.execute("DELETE FROM saved_recipes WHERE id = ?", (recipe_id,))
        self.db.commit()

    # recipe screenshots -----------------------------------------------
    def shots(self, household_id: int, now: datetime) -> tuple[str, int | None] | None:
        """(text so far, recipe saved from it) while a screenshot session is open, else None."""
        row = self.db.execute("SELECT * FROM recipe_shots WHERE household_id = ?", (household_id,)).fetchone()
        if not row or not row["expecting_until"] or datetime.fromisoformat(row["expecting_until"]) < now:
            return None
        return row["text"], row["recipe_id"]

    def set_shots(self, household_id: int, text: str, recipe_id: int | None, until: datetime) -> None:
        self.db.execute(
            "INSERT INTO recipe_shots (household_id, text, recipe_id, expecting_until) VALUES (?,?,?,?) "
            "ON CONFLICT(household_id) DO UPDATE SET text=excluded.text, recipe_id=excluded.recipe_id, "
            "expecting_until=excluded.expecting_until", (household_id, text, recipe_id, until.isoformat()))
        self.db.commit()

    # menus ------------------------------------------------------------
    def set_menu(self, household_id: int, place: str, dishes: list[dict], until: datetime) -> None:
        self.db.execute(
            "INSERT INTO menu_choices (household_id, place, dishes, until) VALUES (?,?,?,?) ON CONFLICT(household_id) "
            "DO UPDATE SET place=excluded.place, dishes=excluded.dishes, until=excluded.until",
            (household_id, place, json.dumps(dishes), until.isoformat()))
        self.db.commit()

    def menu(self, household_id: int, now: datetime) -> tuple[str, list[dict]] | None:
        row = self.db.execute("SELECT * FROM menu_choices WHERE household_id = ?", (household_id,)).fetchone()
        if not row or datetime.fromisoformat(row["until"]) < now:
            return None
        return row["place"], json.loads(row["dishes"])

    def clear_menu(self, household_id: int) -> None:
        self.db.execute("DELETE FROM menu_choices WHERE household_id = ?", (household_id,))
        self.db.commit()

    # shopping list ----------------------------------------------------
    def list_token(self, household_id: int) -> str:
        """The household's private export link token, made the first time it's needed."""
        import secrets
        row = self.db.execute("SELECT token FROM list_links WHERE household_id = ?", (household_id,)).fetchone()
        if row:
            return row["token"]
        token = secrets.token_urlsafe(12)
        self.db.execute("INSERT INTO list_links (household_id, token) VALUES (?, ?)", (household_id, token))
        self.db.commit()
        return token

    def household_for_token(self, token: str) -> int | None:
        row = self.db.execute("SELECT household_id FROM list_links WHERE token = ?", (token,)).fetchone()
        return row["household_id"] if row else None

    def shopping(self, household_id: int) -> list[str]:
        rows = self.db.execute("SELECT name FROM shopping WHERE household_id = ? AND bought_at IS NULL ORDER BY id",
                               (household_id,))
        return [r["name"] for r in rows]

    def add_shopping(self, household_id: int, names: list[str], now: datetime) -> list[str]:
        have = {n.lower() for n in self.shopping(household_id)}
        added = []
        for name in names:
            name = re.sub(r"\s+", " ", str(name)).strip()[:80]
            if name and name.lower() not in have:
                self.db.execute("INSERT INTO shopping (household_id, name, added_at) VALUES (?,?,?)",
                                (household_id, name, now.isoformat()))
                have.add(name.lower())
                added.append(name)
        self.db.commit()
        return added

    def tick_bought(self, household_id: int, bought: list[str], now: datetime) -> list[str]:
        """Someone bought things: tick matching items off the list."""
        ticked = []
        rows = self.db.execute("SELECT id, name FROM shopping WHERE household_id = ? AND bought_at IS NULL", (household_id,)).fetchall()
        for row in rows:
            if any(same_food(row["name"], b) for b in bought):
                self.db.execute("UPDATE shopping SET bought_at = ? WHERE id = ?", (now.isoformat(), row["id"]))
                ticked.append(row["name"])
        self.db.commit()
        return ticked

    def remove_shopping(self, household_id: int, names: list[str], now: datetime) -> list[str]:
        """Take things off the list (not bought, just not wanted)."""
        removed = []
        rows = self.db.execute("SELECT id, name FROM shopping WHERE household_id = ? AND bought_at IS NULL", (household_id,)).fetchall()
        for row in rows:
            if any(same_food(row["name"], n) or n.lower() in row["name"].lower() for n in names):
                self.db.execute("UPDATE shopping SET bought_at = ? WHERE id = ?", (now.isoformat(), row["id"]))
                removed.append(row["name"])
        self.db.commit()
        return removed

    def clear_shopping(self, household_id: int, now: datetime) -> int:
        cur = self.db.execute("UPDATE shopping SET bought_at = ? WHERE household_id = ? AND bought_at IS NULL",
                              (now.isoformat(), household_id))
        self.db.commit()
        return cur.rowcount
