"""Which aisle a shopping item lives in, so long lists read in the order you walk a store."""
from __future__ import annotations

import re

_SPICES = {
    "paprika", "smoked paprika", "sweet paprika", "cumin", "ground cumin", "cumin seeds", "cinnamon", "ground cinnamon",
    "sumac", "turmeric", "ground turmeric", "coriander", "ground coriander", "oregano", "dried oregano", "thyme",
    "dried thyme", "cayenne", "cayenne pepper", "garam masala", "allspice", "nutmeg", "cardamom", "chili flakes",
    "chilli flakes", "red pepper flakes", "za'atar", "zaatar", "bay leaf", "bay leaves", "cloves", "star anise",
    "fennel seeds", "mustard seeds", "ground ginger", "dried basil", "dried rosemary", "dried parsley", "black pepper",
    "white pepper", "chili powder", "chilli powder", "curry powder", "five spice", "suya spice",
}
_NOT_SPICES = {"protein powder", "baking powder", "cocoa powder", "milk powder"}

def is_spice(name: str) -> bool:
    n = name.lower()
    return n in _SPICES or "seasoning" in n or (n.endswith(" powder") and n not in _NOT_SPICES)


AISLES = [   # (name, emoji, words); checked in this order, so "tomato paste" is pantry before "tomato" is produce
    ("Frozen", "🧊", r"frozen|ice cream"),
    ("Pantry", "🥫", r"paste|sauce|canned|tinned|juice|stock|broth|vinegar|oil|peanut butter|dried|cube"),
    ("Meat & fish", "🍗", r"chicken|beef|pork|lamb|goat|turkey|sausage|bacon|ham|steak|mince(?!d)|fish|salmon|tuna|cod|"
                         r"tilapia|shrimp|prawn|crab|thigh|drumstick|wing"),
    ("Dairy & eggs", "🥛", r"milk|yogh?urt|cheese|butter|cream|egg|paneer|kefir|ghee"),
    ("Bakery", "🍞", r"bread|tortilla|wrap|pita|bun|bagel|naan|roll|croissant"),
    ("Produce", "🥬", r"onion|garlic|tomato|lettuce|spinach|kale|pepper|carrot|potato|cucumber|lemon|lime|apple|"
                     r"banana|berr(y|ies)|avocado|parsley|cilantro|coriander leaves|basil|mint|ginger|mushroom|"
                     r"zucchini|courgette|broccoli|cauliflower|cabbage|celery|scallion|leek|herb|fruit|orange|grape|"
                     r"mango|plantain|chil(l)?i|jalape|squash|corn|bean sprout|salad|greens|pear|peach|melon(?! seed)|"
                     r"watermelon|cantaloupe|shallot|scotch bonnet|habanero|green bean|okra|yam|cassava|bitter ?leaf|ugu|pumpkin lea|bok choy|"
                     r"eggplant|aubergine|sweet potato|snow pea|lemongrass|galangal|thai basil|holy basil"),
    ("Pantry", "🥫", r"rice|pasta|spaghetti|noodle|flour|sugar|honey|mayo|ketchup|mustard|bean|lentil|chickpea|"
                    r"oat|cereal|nut|seed|pickle|salt|cracker|chips|coffee|tea|syrup|jam|cocoa|yeast|baking|egusi|ogbono|"
                    r"garri|fufu|semolina|semovita|bouillon|maggi|crayfish|stockfish|locust bean|iru|ogiri"),
]
ORDER = ["Produce", "Meat & fish", "Dairy & eggs", "Bakery", "Pantry", "Spices", "Frozen", "Other"]
EMOJI = {name: emoji for name, emoji, _ in AISLES} | {"Spices": "🧂", "Other": "🛍️"}


def aisle(item: str) -> str:
    if is_spice(item):
        return "Spices"
    low = item.lower()
    for name, _, words in AISLES:
        if re.search(rf"\b({words})", low):
            return name
    return "Other"


def grouped(items: list[str]) -> list[tuple[str, list[str]]]:
    by: dict[str, list[str]] = {}
    for item in items:
        by.setdefault(aisle(item), []).append(item)
    return [(name, by[name]) for name in ORDER if name in by]


