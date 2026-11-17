"""Where someone is, from what they type ("Vancouver", "I'm in Calgary", "timezone London Ontario").

Telegram doesn't tell a bot anyone's time zone, so Bud asks for a city once and turns it into a zone
(daylight saving included). SMS homes get a zone from their area code instead (see sms.timezone_for).
"""
from __future__ import annotations

import re
from zoneinfo import available_timezones

_TORONTO, _VAN, _EDM, _WPG, _REG, _HFX = ("America/Toronto", "America/Vancouver", "America/Edmonton",
                                          "America/Winnipeg", "America/Regina", "America/Halifax")
_NY, _CHI, _DEN, _LA = "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"

# Places people actually say that aren't zone names, plus regions and abbreviations. Canada first.
_SAID = {
    **dict.fromkeys(["ontario", "on", "kitchener", "waterloo", "kw", "cambridge", "guelph", "hamilton", "ottawa",
                     "mississauga", "brampton", "markham", "vaughan", "oakville", "burlington", "london ontario",
                     "london on", "windsor", "barrie", "kingston", "sudbury", "niagara", "niagara falls",
                     "st catharines", "oshawa", "gta", "scarborough", "north york", "etobicoke", "thunder bay",
                     "quebec", "qc", "montreal", "quebec city", "laval", "gatineau", "sherbrooke",
                     "eastern", "eastern time", "est", "edt", "et"], _TORONTO),
    **dict.fromkeys(["british columbia", "bc", "vancouver", "victoria", "surrey", "burnaby", "richmond bc",
                     "kelowna", "abbotsford", "nanaimo", "pacific", "pacific time", "pst", "pdt", "pt"], _VAN),
    **dict.fromkeys(["alberta", "ab", "calgary", "edmonton", "red deer", "lethbridge", "banff",
                     "mountain", "mountain time", "mst", "mdt", "mt", "yellowknife", "northwest territories"], _EDM),
    **dict.fromkeys(["manitoba", "mb", "winnipeg", "brandon", "central", "central time", "cst", "cdt", "ct"], _WPG),
    **dict.fromkeys(["saskatchewan", "sk", "regina", "saskatoon"], _REG),
    **dict.fromkeys(["nova scotia", "ns", "halifax", "dartmouth", "new brunswick", "nb", "moncton", "fredericton",
                     "saint john", "pei", "prince edward island", "charlottetown", "atlantic", "ast", "adt"], _HFX),
    **dict.fromkeys(["newfoundland", "nl", "st johns", "st john's", "saint johns"], "America/St_Johns"),
    **dict.fromkeys(["yukon", "whitehorse"], "America/Whitehorse"),
    **dict.fromkeys(["new york", "nyc", "boston", "philadelphia", "philly", "washington dc", "dc", "miami", "atlanta",
                     "orlando", "charlotte", "pittsburgh", "baltimore", "new jersey", "nj"], _NY),
    **dict.fromkeys(["houston", "dallas", "austin", "san antonio", "minneapolis", "st louis", "nashville",
                     "kansas city", "milwaukee", "new orleans", "texas"], _CHI),
    **dict.fromkeys(["salt lake city", "colorado", "utah"], _DEN),
    **dict.fromkeys(["seattle", "san francisco", "sf", "bay area", "los angeles", "la", "san diego", "san jose",
                     "portland", "sacramento", "las vegas", "california"], _LA),
    **dict.fromkeys(["uk", "england", "united kingdom", "london uk", "london england", "manchester", "birmingham",
                     "scotland", "edinburgh", "glasgow", "wales", "cardiff"], "Europe/London"),
    **dict.fromkeys(["nigeria", "abuja", "lagos", "port harcourt", "ibadan", "enugu", "kano"], "Africa/Lagos"),
    **dict.fromkeys(["ghana", "accra", "kumasi"], "Africa/Accra"),
    **dict.fromkeys(["kenya", "nairobi"], "Africa/Nairobi"),
    **dict.fromkeys(["south africa", "johannesburg", "cape town", "durban"], "Africa/Johannesburg"),
    **dict.fromkeys(["india", "mumbai", "delhi", "new delhi", "bangalore", "bengaluru", "chennai", "hyderabad",
                     "pune", "kolkata"], "Asia/Kolkata"),
    **dict.fromkeys(["france", "paris"], "Europe/Paris"),
    **dict.fromkeys(["germany", "berlin", "munich"], "Europe/Berlin"),
    **dict.fromkeys(["uae", "dubai", "abu dhabi"], "Asia/Dubai"),
    **dict.fromkeys(["philippines", "manila"], "Asia/Manila"),
    **dict.fromkeys(["australia", "sydney"], "Australia/Sydney"),
}
# Zone city names that are also foods: only accepted when someone says "timezone X" outright.
_FOODS = {"lima", "cayenne", "brussels", "jamaica", "cancun"}

_EXPLICIT = re.compile(
    r"^\s*(?:my\s+)?(?:time\s*zone|tz|i'?m\s+in|i\s+am\s+in|i\s+live\s+in|we\s+live\s+in|we'?re\s+in|i'?m\s+based\s+in|"
    r"based\s+in|my\s+city\s+is|city|located\s+in|i\s+moved\s+to|we\s+moved\s+to|i'?m\s+from\s+and\s+live\s+in)"
    r"\s*[:\-]?\s*(.+?)\s*[.!]*\s*$", re.I)


def _zone_cities() -> dict[str, str]:
    """'vancouver' -> 'America/Vancouver' for every city in the zone database."""
    out: dict[str, str] = {}
    for zone in available_timezones():
        if "/" not in zone or zone.startswith(("Etc/", "SystemV/", "US/", "Canada/", "posix/", "right/")):
            continue
        out.setdefault(zone.rsplit("/", 1)[1].replace("_", " ").lower(), zone)
    return out


_CITIES = _zone_cities()
_BY_ZONE = {z.lower(): z for z in available_timezones()}


def _clean(text: str) -> str:
    text = text.lower().replace(".", "").replace("’", "'")
    text = re.sub(r"\b(?:canada|usa|the us|united states|area|region|right now|now|thanks|thank you)\b", " ", text)
    return re.sub(r"[^a-z' ,/]+", " ", text).strip(" ,")


def resolve(name: str, allow_foods: bool = True) -> str | None:
    """'Calgary' -> 'America/Edmonton'; 'Toronto, ON' -> 'America/Toronto'; None if it isn't a place we know."""
    raw = name.strip()
    if raw.lower() in _BY_ZONE:                       # "America/Vancouver"
        return _BY_ZONE[raw.lower()]
    text = re.sub(r"\s+", " ", _clean(raw)).strip()
    if not text:
        return None
    tries = [text.replace(",", " "), *[part.strip() for part in text.split(",")]]
    for t in tries:
        t = re.sub(r"\s+", " ", t).strip()
        if not t:
            continue
        if t in _SAID:
            return _SAID[t]
        if t in _CITIES and (allow_foods or t not in _FOODS):
            return _CITIES[t]
    return None


def parse(text: str, bare: bool = False) -> tuple[str, str] | None:
    """(zone, place as they wrote it) for "timezone Calgary" / "I'm in Calgary", or for a bare "Calgary" when
    `bare` (Bud just asked which city). Otherwise None, so ordinary messages are never mistaken for a city."""
    m = _EXPLICIT.match(text or "")
    if m:
        zone = resolve(m.group(1))
        return (zone, m.group(1).strip(" .!")) if zone else None
    if bare and text and len(text.split()) <= 4:
        zone = resolve(text, allow_foods=False)
        return (zone, text.strip(" .!")) if zone else None
    return None


def label(zone: str) -> str:
    """'America/St_Johns' -> 'St Johns'."""
    return zone.rsplit("/", 1)[-1].replace("_", " ")
