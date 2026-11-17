"""Commands the app layer understands on top of the engine's parser.

    dinner, all, have, old                       the fridge
    link, paste, recipe, steps, recipes, audio   the recipe book
    keep                                         keep a recipe Bud showed ("save this recipe", "save egusi soup")
    shop, shopadd, shopping, clearshop           the shopping list
    shopfor                                      one recipe's part of it ("my egusi shopping list")
    roast                                        fun
"""
from __future__ import annotations

import re

from .links import first_url
from .parser import _items

_X = r"[\s?.!]*$"
_KEYWORDS = (
    ("meal", re.compile(
        r"^(?:(?:a|an|any|some)\s+)?(breakfast|brunch|lunch|snack)s?(?:\s+(?:idea|ideas|please|today|now|tomorrow))*" + _X + "|"
        r"^what(?:'s| is|s)? for (breakfast|brunch|lunch)(?: today| tomorrow)?" + _X + "|"
        r"^what (?:can|should|do) (?:i|we) (?:have|eat|make|cook) for (breakfast|brunch|lunch|a snack)(?: today| tomorrow)?" + _X + "|"
        r"^(?:something|anything) (?:to |i can |we can )?(snack) on" + _X, re.I)),
    ("dinner", re.compile(
        r"^(?:dinner|supper|tonight|idea|ideas|another|next|"
        r"what(?:'s| is|s) for (?:dinner|supper)|what (?:can|should|do) (?:i|we) (?:make|cook|eat)(?: tonight| for dinner)?|"
        r"dinner idea|another idea|any ideas?|something else)" + _X, re.I)),
    ("all", re.compile(
        r"^(?:all|everything|inventory|full list|show (?:me )?(?:all|everything)|"
        r"what(?:'s| is|s) in (?:my|the|our) fridge|what do (?:i|we) have(?: left| in (?:my|the|our) fridge)?|"
        r"what (?:food )?(?:have|do) (?:i|we) (?:got|have))" + _X, re.I)),
    ("steps", re.compile(r"^(?:cook|cook it|steps|the steps|show (?:me )?(?:the )?steps|full recipe|method|let'?s cook)" + _X, re.I)),
    ("more", re.compile(r"^(?:more|more ideas|more texts|text me more|more often|send more|"
                        r"i want (?:more|breakfast|lunch) ideas(?: too)?)" + _X, re.I)),
    ("less", re.compile(r"^(?:less|fewer|fewer texts|less texts|text me less|less often|too many texts|"
                        r"(?:you text|you're texting|youre texting) (?:too much|a lot)|not every day)" + _X, re.I)),
    ("order", re.compile(
        r"^(?:(?:can|could) you |please |pls )?(?:order|instacart|deliver|delivery|get (?:it|them|these|groceries|my groceries|the groceries|my list) delivered|"
        r"order (?:it|them|these|groceries|my groceries|the groceries|my list|the list|my shopping|the shopping|on instacart|"
        r"from instacart|online)(?: list)?(?: for me)?(?: (?:on|from|with|via) instacart)?|"
        r"(?:send|put) (?:it|this|my list|the list) (?:to|on) instacart|shop (?:it )?(?:on|with) instacart|"
        r"deliver (?:it|them|my groceries|my list))(?: please)?" + _X, re.I)),
    ("clearshop", re.compile(r"^clear (?:my |the |our )?(?:shopping list|shopping|list)" + _X, re.I)),
    ("shop", re.compile(
        r"^(?:shop|(?:add|put) (?:them|those|it|these|the missing(?: ones| items| ingredients| stuff)?|missing|what(?:'s| is) missing|"
        r"what i(?:'m| am) missing)(?: (?:to|on) (?:my |the |our )?(?:shopping |grocery )?list)?|"
        r"add to (?:my |the |our )?(?:shopping |grocery )?list)" + _X, re.I)),
    ("shopping", re.compile(
        r"^(?:shopping|shopping list|groceries|grocery list|my list|what do (?:i|we) need(?: to (?:buy|get))?|"
        r"(?:(?:can|could) (?:i|you) |please |pls )?(?:show|see|view|send|give|open|get)(?: me)?(?: (?:my|the|our))? (?:shopping|grocery) list(?: please)?|"
        r"(?:my|the|our) (?:shopping|grocery) list|"
        r"export(?: (?:my|the|our))?(?: (?:shopping|grocery))?(?: list)?(?: to (?:notes|apple notes|notion|my notes))?|"
        r"what(?:'s| is) on (?:my|the|our) (?:shopping |grocery )?list)" + _X, re.I)),
    ("audio", re.compile(
        r"^(?:audio|listen|read it(?: to me| out)?|read (?:it )?aloud|podcast|play(?: it)?|say it|cook ?along|"
        r"talk me through it)" + _X, re.I)),
    ("recipes", re.compile(
        r"^(?:recipe|recipe book|my recipe book|"
        r"(?:(?:can|could) (?:i|you) |please |pls )?(?:(?:see|show|list|view|open|get|give|send)(?: me)? )?"
        r"(?:what are |what's |whats |which are )?(?:all )?(?:(?:my|our|the) )?(?:saved |kept |favou?rite )?recipes"
        r"(?: (?:i|we)(?:'ve| have)? (?:saved|kept|have))?(?: (?:so far|please))?|"
        r"what (?:recipes )?(?:have|did) (?:i|we) (?:saved?|kept|keep)|what recipes do (?:i|we) have|"
        r"which recipes (?:have|did) (?:i|we) (?:saved?|kept))" + _X, re.I)),
    ("roast", re.compile(r"^(?:roast|roast me|roast it|roast my fridge|fridge roast)" + _X, re.I)),
)

_RECIPE = (
    re.compile(r"^(?:recipe|recipe for|find recipe|get recipe)\s*:?\s+(?:for )?(.+)$", re.I),
    re.compile(r"^(?:(?:can|could|would) you |please |pls )*(?:fetch|get|find|give|show|send|pull up|look up|make|write)"
               r"(?: me)?(?: (?:a|an|the|my|some|any))? (.+?) recipes?" + _X, re.I),
    re.compile(r"^(?:do you have|have you got|is there|got)(?: (?:a|an|any|my|the))? (.+?) recipes?" + _X, re.I),
    re.compile(r"^how (?:do|can|would|should) (?:i|we|you) (?:make|cook|prepare) (.+?)" + _X, re.I),
    re.compile(r"^(?:i|we) (?:want|wanna|would like|'d like) to (?:make|cook) (.+?)" + _X, re.I),
    re.compile(r"^((?:[\w'-]+ ){0,4}[\w'-]+) recipes?" + _X, re.I),
)
# Keeping a recipe Bud already showed. Checked before "paste" so "save this recipe" with nothing after it
# means "keep the one you just showed me", not "here comes a recipe".
_KEEP = re.compile(
    r"^(?:(?:no|ok|okay|yes|yeah|yep|sure|great|nice|perfect)[,.!]* )?(?:(?:can|could|would) you |please |pls |just )*"
    r"(?:(?:save|keep)(?: (?:this|that|it|the|this one|that one))?(?: (?:recipe|one|dish))?"
    r"|(?:i )?(?:just )?(?:want|need|would like|'d like) (?:it|this|that)(?: one| recipe)? (?:saved|kept)"
    r"|(?:i )?(?:just )?(?:want|need|would like|'d like) to (?:save|keep) (?:it|this|that)(?: one| recipe)?"
    r"|add (?:it|this|that)(?: one| recipe)? to (?:my )?(?:saved )?recipes(?: book)?)"
    r"(?: (?:for me|for later|to my (?:recipes|recipe book|saved recipes)|in my recipes|please))*" + _X, re.I)
_KEEP_NAMED = re.compile(r"^(?:please )?(?:save|keep) (?:the |my )?((?:[\w'()-]+ ){0,7}[\w'()-]+?)(?: recipe)?" + _X, re.I)
_OLD = re.compile(r"^(?:old|older)\s*:?\s+(?:the\s+)?(.+)$", re.I)
_SHOP_FOR = (
    re.compile(r"^(?:(?:i mean|no|nah|show me|send me|give me|can i see|can i get|what(?:'s| is) on|open)[,]? )*"
               r"(?:my |the |our )?(.+?) (?:shopping|grocery|groceries) list" + _X, re.I),
    re.compile(r"^(?:shopping|grocery|groceries)(?: list)? (?:for )?(?:my |the |our )?(.+?)" + _X, re.I),
    re.compile(r"^what (?:do|else do) (?:i|we) (?:need|have) to (?:buy|get) for (?:my |the |our )?(.+?)" + _X, re.I),
    re.compile(r"^what (?:do|else do) (?:i|we) need (?:to buy |to get )?for (?:my |the |our )?(.+?)" + _X, re.I),
)
# "I don't have most of that": Bud's picture of their fridge is off for this dish
_MISSING = re.compile(
    r"^(?:(?:but|well|um|hmm|no|nah|ah|oh)[,.!]* )?(?:i|we) (?:don'?t|do not|dont|didn'?t) (?:actually )?(?:have|got)"
    r"(?: (?:most|many|half|all|any|some|much|a lot|enough))?(?: of)?(?: (?:the|those|these|that|its|it's|it|them))?"
    r"(?: (?:ingredients|stuff|things|items))?(?: for (?:it|that|this|that one|this one|the \w+))?(?: (?:anymore|any more|at home|here))?" + _X +
    r"|^(?:i'?m|im|we'?re|we are) (?:missing|out of) (?:most|a lot|half|many|loads)(?: of (?:it|them|those|the ingredients|that))?" + _X +
    r"|^(?:i|we) (?:have|got) (?:none|nothing|barely any)(?: of (?:it|them|those|that|the ingredients))?" + _X, re.I)
# "Something that isn't from my fridge": they want to cook what they feel like, not what they have
_OUTSIDE = re.compile(
    r"^(?:(?:can|could) (?:i|you) |please |pls )?(?:i (?:want|wanna|would like|'d like) |give me |show me |suggest |how about )?"
    r"(?:something|anything|a (?:dish|meal|recipe)|food)(?: else)? "
    r"(?:not (?:from|in|using|with)|outside(?: of)?|other than|beyond|that'?s not (?:from|in)|that isn'?t (?:from|in)|without using) "
    r"(?:my |the |what'?s in (?:my |the )?|what i have(?: in)?(?: my)? ?)?(?:fridge|ingredients|food|stuff|groceries|kitchen)?" + _X +
    r"|^(?:i want|i'?d like|give me) (?:something|anything) (?:totally |completely |entirely )?(?:different|new)" + _X +
    r"|^(?:forget|ignore) (?:my|the) fridge" + _X, re.I)
# "I'm craving suya", "I feel like pasta tonight", "in the mood for shawarma"
_CRAVING = re.compile(
    r"^(?:(?:i'?m|im|i am|we'?re|we are) )?(?:really |kinda |kind of |so )?(?:craving|in the mood for|fancying|feeling like) "
    r"(?:some |a |an |for )?(.+?)(?: (?:tonight|today|right now|now|for dinner|for lunch|for breakfast))?" + _X +
    r"|^(?:i|we) (?:really )?(?:feel like|want|wanna|fancy) (?:having |eating |making |cooking |some |a |an )*(.+?)"
    r"(?: (?:tonight|today|right now|now|for dinner|for lunch|for breakfast))?" + _X, re.I)
_NOT_CRAVED = re.compile(r"^(?:to\b|it\b|this\b|that\b|something|anything|more|less|breakfast|lunch|dinner|a snack|"
                         r"snack|ideas?\b|recipes?\b|help\b|you\b|my\b|the list|groceries|stop|food)", re.I)
_NOT_A_DISH = {"my", "the", "our", "full", "whole", "entire", "current", "new", "this", "that", "it", "me", "us",
               "today", "tonight", "this week", "the week", "dinner", "the store", "a shop", "please", "pls", "done",
               "bought", "now", "thanks", "again", "finished", "sorted", "here", "updated", "app", "page"}
_SHOP_ADD = re.compile(r"^(?:add|put) (.+?) (?:to|on) (?:my |the |our )?(?:shopping |grocery )?list" + _X, re.I)
_HAVE = (
    re.compile(r"^(?:do|did) (?:i|we) (?:still )?have (?:any |some |more |the )?(.+)$", re.I),
    re.compile(r"^(?:have|got) (?:(?:i|we) )?(?:got )?(?:any |some |the )?(.+)$", re.I),
    re.compile(r"^(?:is|are) there (?:still )?(?:any |some |more )?(.+)$", re.I),
    re.compile(r"^any (?:more )?(.+)$", re.I),
)
_HAVE_TAIL = re.compile(r"(?:\s+(?:left|still|in (?:the|my|our) fridge|at home|in there|around))+$", re.I)

# Kinds that win even when the engine's parser thinks it understood the text
# ("add them to my list" is not a purchase of "them to my list").
ALWAYS = {"meal", "taste", "keep", "link", "paste", "recipe", "steps", "shop", "shopadd", "shopping", "shopfor", "order", "more", "less", "missing", "outside", "clearshop", "audio", "recipes", "roast"}


def _asked_about(text: str) -> str | None:
    looks_like_question = text.rstrip().endswith("?") or bool(re.search(r"\bleft\b", text, re.I))
    for i, pattern in enumerate(_HAVE):
        m = pattern.match(text)
        if not m:
            continue
        if i in (1, 3) and not looks_like_question and not re.match(r"have \S+$", text, re.I):
            continue  # "got milk and eggs" is a purchase and "have a good day" is small talk
        name = _HAVE_TAIL.sub("", m.group(1).strip(" ?.!")).strip().lower()
        if name and not name.startswith("to "):
            return name
    return None


def service_command(text: str) -> tuple[str, list[str]] | None:
    """(kind, arguments) for a message this layer handles, or None."""
    raw = (text or "").strip()
    url = first_url(raw)
    if url:
        return "link", [url]
    one_line = re.sub(r"\s+", " ", raw)
    if "\n" not in raw and _KEEP.match(one_line):
        return "keep", []
    if re.match(r"save (?:this )?recipe\b", raw, re.I) or (len(raw) > 250 and re.search(r"ingredient", raw, re.I)):
        return "paste", [re.sub(r"^save (?:this )?recipe\s*:?\s*", "", raw, flags=re.I)]
    t = re.sub(r"\s+", " ", raw)
    taste = re.match(r"^(?:taste|my taste|about me|food persona|my persona)\b\s*:?\s*(.*)$", t, re.I)
    if taste:
        return "taste", [taste.group(1).strip()] if taste.group(1).strip() else []
    named = _KEEP_NAMED.match(t) if "\n" not in raw else None
    if named and not re.match(r"(?:me|us|money|time|room|space)\b", named.group(1), re.I):
        return "keep", [named.group(1)]
    for kind, pattern in _KEYWORDS:
        m = pattern.match(t)
        if m and kind == "meal":
            meal = next(g for g in m.groups() if g).lower()
            return "meal", ["snack" if "snack" in meal else meal]
        if m:
            return kind, []
    m = _SHOP_ADD.match(t)
    if m and _items(m.group(1).lower()):
        return "shopadd", _items(m.group(1).lower())
    for pattern in _SHOP_FOR:
        m = pattern.match(t)
        if m:
            dish = re.sub(r"\s+recipe$", "", m.group(1).strip(" ?.!"), flags=re.I).strip()
            if dish and dish.lower() not in _NOT_A_DISH and not re.search(r"\b(?:shopping|grocery|list)\b", dish, re.I):
                return "shopfor", [dish]
    if _MISSING.match(t):
        return "missing", []
    if _OUTSIDE.match(t):
        return "outside", []
    m = _CRAVING.match(t)
    if m:
        dish = (m.group(1) or m.group(2) or "").strip(" ?.!")
        if dish and len(dish) <= 40 and not _NOT_CRAVED.match(dish):
            return "recipe", [re.sub(r"^(?:a|an|the|some) ", "", dish, flags=re.I)]
    for pattern in _RECIPE:
        m = pattern.match(t)
        if m:
            dish = re.sub(r"^(?:a|an|the|my|some) ", "", m.group(1).strip(" ?.!"), flags=re.I).strip()
            if re.match(r"(?:what|which|how|show|list|see|my|our|all|any|saved|your)\b", dish, re.I):
                continue                      # "what are my saved" is a question, not a dish
            if dish and dish.lower() not in {"a", "the", "it", "this", "that", "dinner", "something"}:
                return "recipe", [dish]
    m = _OLD.match(t)
    if m and _items(m.group(1).lower()):
        return "old", _items(m.group(1).lower())
    asked = _asked_about(t)
    if asked:
        return "have", [asked]
    return None
