"""Where to buy a shopping list: which nearby store most likely has most of it, and where the specialty bits are.

Instacart's public API lists the retailers near a postal code but not their shelves, so this is a reasoned guess,
always worded as one ("most likely"): everyday items go to the first big supermarket near them, and items that
mainstream stores often don't stock (egusi, fish sauce, paneer, scotch bonnet...) go to the kind of grocer that
does, by name if one of those is near them on Instacart.
"""
from __future__ import annotations

import re

# (kind of store, items that usually need it, retailer names that are that kind of store)
SPECIALTY: list[tuple[str, str, str]] = [
    ("an African grocery", r"egusi|ogbono|\biru\b|ogiri|locust bean|palm oil|crayfish|stockfish|garri|fufu|semovita|"
                           r"yam flour|bitter ?leaf|\bugu\b|uziza|suya|dawadawa|ponmo|kpomo|ehuru|uda\b|utazi",
     r"african|afro|naija|tropical"),
    ("an Asian grocery", r"fish sauce|oyster sauce|thai basil|holy basil|bird'?s eye|lemongrass|galangal|kaffir|"
                         r"gochujang|kimchi|miso|mirin|bok choy|rice noodle|nori|shaoxing|doubanjiang|hoisin|"
                         r"sambal|tamarind|dashi|bonito|glutinous",
     r"t ?& ?t|h ?mart|99 ranch|nations|galleria|oceans|asian|seafood city|lucky|mitsuwa|uwajimaya"),
    ("a South Asian grocery", r"paneer|ghee|garam masala|curry lea|asafoetida|\bhing\b|toor dal|urad|chana dal|"
                              r"methi|amchur|basmati|atta|jaggery",
     r"patel|iqbal|subzi|india|desi|apna|nations|bombay"),
    ("a Caribbean or Latin grocery", r"scotch bonnet|ackee|callaloo|jerk seasoning|sorrel|pimento|tomatillo|"
                                     r"masa harina|queso fresco|epazote|plantain|cassava|yuca|sazon|adobo",
     r"caribbean|latin|fiesta|mexican|tropical|island|sabor"),
]
MAINSTREAM = (r"no frills|freshco|food basics|walmart|real canadian superstore|superstore|costco|loblaws|metro|sobeys|"
              r"farm boy|longo|zehrs|fortinos|safeway|kroger|aldi|target|whole foods|giant tiger|save-on|"
              r"independent|valu-mart|foodland|publix|wegmans|meijer|h-e-b|heb|stop & shop|food lion|shoprite")


def split(items: list[str]) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """(everyday items, [(kind of store, its items)])"""
    everyday: list[str] = []
    special: dict[str, list[str]] = {}
    for item in items:
        kind = next((k for k, words, _ in SPECIALTY if re.search(words, item, re.I)), None)
        if kind:
            special.setdefault(kind, []).append(item)
        else:
            everyday.append(item)
    return everyday, [(k, special[k]) for k, _, _ in SPECIALTY if k in special]


def plan(items: list[str], retailers: list[str]) -> dict:
    """Which store for what. retailers = names of stores near them on Instacart, in Instacart's order."""
    everyday, special = split(items)
    main = next((r for r in retailers if re.search(MAINSTREAM, r, re.I)), retailers[0] if retailers else None)
    specialty = []
    for kind, names in special:
        pattern = next(p for k, _, p in SPECIALTY if k == kind)
        near = next((r for r in retailers if re.search(pattern, r, re.I)), None)
        specialty.append({"kind": kind, "items": names, "store": near})
    return {"main": main, "everyday": everyday, "specialty": specialty}


POSTAL = re.compile(r"\b([A-Za-z]\d[A-Za-z]) ?(\d[A-Za-z]\d)\b|\b(\d{5})(?:-\d{4})?\b")


def postal(text: str) -> tuple[str, str] | None:
    """('N2G 1A1', 'CA') or ('94103', 'US') from whatever they typed."""
    m = POSTAL.search(text or "")
    if not m:
        return None
    if m.group(3):
        return m.group(3), "US"
    return f"{m.group(1).upper()} {m.group(2).upper()}", "CA"
