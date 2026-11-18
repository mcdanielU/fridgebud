"""Every word FridgeBud sends lives here, so tone can be tuned in one place.

Bud's voice: a warm, slightly cheeky kitchen sidekick, written for a five-second glance.
  - Lead with the point. Two or three short lines; lists as bullets, never a paragraph.
  - At most one emoji, and only where it carries meaning.
  - At most one "what to reply" hint, and only when the next step isn't obvious.
  - Dramatic about the food, never about the person. Never say a food is safe ("worth checking", not "still safe").
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .aisles import EMOJI, grouped, is_spice  # noqa: F401

from .recipes import Suggestion
from .watchlist import Entry


class Card(str):
    """A message with buttons. It is still a str, so every channel that can't show buttons just sends the text."""

    def __new__(cls, text: str, buttons: list[tuple[str, str]] | None = None, photo: dict | None = None):
        card = super().__new__(cls, text)
        card.buttons = list(buttons or [])   # (label, https url)
        card.photo = photo                   # {"url"} or {"prompt", "key"}, plus "caption": sent just before the text
        return card


EXPORT_BUTTON = "🛒 Open list · Export to Notes / Notion"


def recording(seconds: int) -> str:
    """Sent before a cook-along is voiced, so the wait isn't silent."""
    eta = "about a minute" if seconds >= 50 else f"about {max(10, round(seconds, -1))} seconds"
    return f"🎧 Recording your cook-along… {eta}."


WATCHING = "🎬 Watching your video… give me about a minute."

# Their food persona ------------------------------------------------------------
ASK_TASTE = ("🎙️ Help me get your taste right: send a voice note about the foods you love (any cuisine), "
             "anything you avoid, and when you usually eat. Or type it after TASTE.")
TASTE_PS = "P.S. 🎙️ Send me a voice note about the foods you love and when you eat, and I'll plan around you."
VOICE_NEEDS_KEY = "I can't listen to voice notes yet 🙉 Type it instead, e.g. TASTE spicy food, big lunch at 1pm."
VOICE_UNCLEAR = "I couldn't make that voice note out 🙉 Mind trying again, or typing it?"
TASTE_EMPTY = "I heard you, but didn't catch any food details 🤔 Tell me what you love, what you avoid, and when you eat."


def _clock_short(hhmm: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    return f"{h % 12 or 12}{':%02d' % m if m else ''}{'pm' if h >= 12 else 'am'}"


def persona_saved(persona: dict, avoid: list[str], diet: str, nudge: tuple[int, int] | None) -> str:
    lines = ["Got it 😋 Your food persona:"]
    if persona.get("likes"):
        lines.append("• Loves: " + ", ".join(persona["likes"][:6]))
    if persona.get("cuisines"):
        lines.append("• Cuisines: " + ", ".join(persona["cuisines"][:5]))
    if avoid:
        lines.append("• Skips: " + ", ".join(avoid[:6]))
    if diet:
        lines.append("• Diet: " + diet)
    meals = persona.get("meals") or {}
    if meals:
        lines.append("• Eats: " + ", ".join(f"{m} {_clock_short(meals[m])}" for m in ("breakfast", "brunch", "lunch", "dinner", "snacks") if m in meals))
    if nudge:
        lines.append(f"I'll send meal ideas around {_clock(*nudge)} 🕐")
    return "\n".join(lines)


WELCOME = ("Hi, I'm Bud 👋 I help you use your food before it goes off, with one short text a day. "
           "Reply YES to start (STOP anytime).")
CONSENTED = ("You're in 🎉 Send a photo of your fridge or a receipt, or just type what you bought "
             "(e.g. \"bought spinach, chicken\").")
ALREADY_IN = "You're all set, you're already signed up. Text HELP to see what I can do."
STOPPED = "You're unsubscribed and won't get more texts. Reply START to come back."
HELP = ("Here's what I can do 👇\n"
        "• Send a fridge photo, or \"bought milk, eggs\"\n"
        "• Ask \"what's for dinner?\" or \"do I have eggs?\"\n"
        "• Send a recipe link to save it\n"
        "• On my daily idea: reply 👍 to cook it, 👎 for another\n"
        "• After you cook: reply MADE, then 🔥 / 👌 / 👎 so I learn your taste\n"
        "• \"What do you know about me?\" shows what I've learned\n"
        "• MORE for breakfast/lunch ideas too · LESS for fewer texts\n"
        "• SHOPPING for your list · STOP to pause")
UNKNOWN = "Hmm, I didn't catch that 🤔 Try \"what's for dinner?\" or send a fridge photo. HELP shows everything."
PHOTO_UNREADABLE = "Thanks for the photo. I couldn't make out the food in it. Try a closer shot, or just text what you bought."


def _when(entry: Entry) -> str:
    if entry.item.kind == "leftover":
        return f"day {entry.age}"
    return entry.label


_MEAL_LABELS = {"breakfast": "Breakfast", "brunch": "Brunch", "lunch": "For lunch", "snack": "Snack", "dinner": "Tonight"}


def _idea(s: Suggestion) -> str:
    uses = ", ".join(i.name for i in s.rescues[:3])
    label = _MEAL_LABELS.get(s.meal, "Tonight")
    if s.minutes is None:
        text = f"{label}: {s.title}, no cooking needed"
    elif uses:
        text = f"{label}: {s.title}, {s.minutes} min (uses {uses})"
    else:
        text = f"{label}: {s.title}, {s.minutes} min"
    if s.ai:
        text += "\n🧪 I wrote this one: your fridge alone didn't make a full meal. I'll add what's missing to your list."
    return text


PICK_FOOTER = "Reply 👍 to cook this\nReply 👎 for a different idea"


def daily_nudge(watchlist: list[Entry], suggestion: Suggestion | None) -> str | None:
    """The one daily text. A pick whenever the fridge can make something, so "what's for dinner?" is already
    answered before it's asked; None when there's nothing worth saying (no text beats a useless one)."""
    fresh = [e for e in watchlist if not e.past_window]
    stale = [e for e in watchlist if e.past_window]
    if not fresh and not stale and not suggestion:
        return None
    lines: list[str] = []
    if fresh:
        listed = ", ".join(f"{e.item.name} ({_when(e)})" for e in fresh[:3])
        more = f" +{len(fresh) - 3} more" if len(fresh) > 3 else ""
        lines += [f"Use soon: {listed}{more}", ""]
    if suggestion:
        lines.append(_idea(suggestion))
        if suggestion.breakfast:
            lines.append(_idea(suggestion.breakfast))
    if stale:
        lines += ["", f"Worth checking: {', '.join(e.item.name for e in stale[:3])}. Still good, or tossed?"]
    lines += ["", PICK_FOOTER if suggestion else f"Reply GOOD or TOSSED {(stale or fresh)[0].item.name}"]
    return "\n".join(lines)


def still_on(recipe: str, meal: str = "dinner", alt: str | None = None) -> str:
    """The one gentle follow-up when the pick got no reply at all."""
    when = "tonight" if meal == "dinner" else "lunch"
    lines = [f"Still on for {when}? 🍳", "", f"Planning to make the {recipe}?", "", "Reply 👍 if yes"]
    if alt:
        lines.append(f"Reply 2 for the {short_title(alt)} instead")
    lines.append("Reply 👎 for a different idea")
    return "\n".join(lines)


def locked_in(recipe: str, meal: str = "dinner") -> str:
    return f"Nice, {recipe} it is 👍\n\nReply COOK when you start and I'll walk you through it. I'll check in after {meal}."


def short_title(title: str) -> str:
    """'Efo Riro (Yoruba Spinach Stew)' -> 'Efo Riro'"""
    return title.split(" (")[0].strip() or title


def with_saved_option(text: str, title: str, uses: list[str]) -> str:
    """The daily pick, plus one of their own saved recipes as option 2 (no long title to type back)."""
    alt = f"⭐ Or one of your saved recipes: {title}" + (f"\n(uses your {', '.join(uses[:2])})" if uses else "")
    footer = f"Reply 👍 to cook this\nReply 2 for the {short_title(title)}\nReply 👎 for a different idea"
    return text.replace(PICK_FOOTER, alt + "\n\n" + footer)


def made_reward(cheer: str, recipe: str | None, rescued: list[str], worth: float, month: float, cooked: int,
                milestone: str | None, teaser: str | None) -> str:
    """MADE pays out: what it saved, the month so far, the week's count, sometimes a milestone, a peek at tomorrow."""
    lines = [f"{cheer} {recipe} done ✅" if recipe else f"{cheer} ✅", ""]
    if rescued:
        lines.append(f"🥬 Rescued: {', '.join(rescued[:4])}" + (f" (≈${worth:.0f})" if worth >= 1 else ""))
    if month >= 1:
        lines.append(f"💰 ≈${month:.0f} of food saved this month")
    if cooked >= 2:
        lines.append(f"🍳 {_ordinal(cooked)} home-cooked meal this week")
    if milestone:
        lines += ["", milestone]
    if teaser:
        lines += ["", teaser]
    lines += ["", "How was it? Reply 🔥 if you loved it, 👌 if it was fine, or 👎 if never again"]
    return "\n".join(lines)


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def dollar_milestone(amount: int) -> str:
    return f"🎉 You just passed ${amount} of food saved this month. That's real money, not compost."


def meal_milestone(n: int) -> str:
    return "🎉 First meal cooked with Bud. Many more to come." if n == 1 else f"🎉 {n} meals cooked with Bud. You're on a roll."


def tomorrow_teaser(title: str, uses: list[str]) -> str:
    return f"👀 Peek at tomorrow: maybe {title}" + (f" (your {', '.join(uses[:2])})" if uses else "") + "."


def rated(kind: str, recipe: str) -> str:
    return {"love": f"Noted 🔥 {recipe} is a favourite now. I'll bring it back when your fridge allows.",
            "fine": "Noted 👌 Thanks!",
            "nope": f"Got it 👋 {recipe} is off the rotation."}[kind]


ABOUT_ME = ("what do you know about me", "what do you remember about me", "my notebook", "notebook", "my profile",
            "what have you learned about me", "what do you know")


# One-time tips: each feature taught once, on the daily pick, and never if they've already used it.
# (key, events that mean "they already know this", text)
TIPS: list[tuple[str, tuple[str, ...], str]] = [
    ("receipt", ("receipt_capture",), "💡 Tip: after you shop, snap the receipt. I'll add everything in one go and tick it off your list."),
    ("link", ("recipe_saved",), "💡 Tip: paste any recipe link (YouTube, TikTok, a blog) and I'll save it and check it against your fridge."),
    ("have", ("asked_have",), "💡 Tip: at the store? Ask \"do I have eggs?\" before you buy doubles."),
    ("meals", ("meal_asked",), "💡 Tip: not just dinner. Ask for BREAKFAST, LUNCH or a SNACK idea any time."),
    ("shop", ("shopping_added", "shopping_for"), "💡 Tip: reply SHOP under any recipe and I'll add only what you're missing to your list."),
    ("leftovers", ("leftover_added",), "💡 Tip: made extra? Text \"leftovers chili x3\" and I'll remind you before they turn."),
    ("audio", ("audio",), "💡 Tip: reply AUDIO under a recipe for a hands-free cook-along while you cook."),
    ("voice", ("persona_set",), "💡 Tip: send a voice note about the food you love and when you eat. Your picks get way better."),
    ("moreless", ("cadence_set",), "💡 Tip: want breakfast or lunch ideas too? Reply MORE. Too many texts? Reply LESS."),
    ("notebook", ("notebook",), "💡 Tip: ask \"what do you know about me?\" to see what I've learned about your taste."),
    ("share", ("member_invited", "member_consent"), "💡 Tip: cook with someone? Have them text JOIN {code} so you share one fridge and one list."),
]
TIP_DAYS = 28        # after four weeks they know Bud; tips stop
TIP_GAP_DAYS = 2     # never two days running


# How often Bud texts: MORE / LESS, the one-time breakfast and lunch offers, the morning and midday ideas ----
_MEAL_EMOJI = {"breakfast": "☀️", "lunch": "🥪"}


def clock(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h % 12 or 12}{':%02d' % m if m else ''} {'PM' if h >= 12 else 'AM'}"


def meal_idea(s: Suggestion) -> str:
    """The opt-in morning or midday text."""
    return (f"{_MEAL_EMOJI.get(s.meal, '🍽️')} {_idea(s)}\n\n"
            f"Want another idea? Reply {s.meal.upper()}\nToo many texts? Reply LESS")


def meal_offer(meal: str, s: Suggestion) -> str:
    """Asked once, at that meal's time, with a real idea in hand: value first, then the question."""
    opener = "Morning! " if meal == "breakfast" else ""
    every = "every morning" if meal == "breakfast" else "every day around now"
    return (f"{_MEAL_EMOJI.get(meal, '🍽️')} {opener}{_idea(s)}\n\n"
            f"Want a {meal} idea like this {every}?\nReply 👍 for yes or 👎 for no thanks")


def meals_on(meals: list[str], times: dict[str, int]) -> str:
    when = " and ".join(f"a {m} idea around {clock(times[m])}" for m in meals)
    return f"Done ✅ You'll get {when}, plus your usual dinner pick.\nReply LESS any time."


def meal_offer_declined(meal: str) -> str:
    return f"No problem 👍 No {meal} texts. (Changed your mind? Reply MORE.)"


ASK_MORE = "Happy to 😊 Want daily ideas for breakfast, lunch, or both? (Your dinner pick stays as is.)"
BACK_TO_DAILY = "Back to a pick every day 👍 Reply MORE again for breakfast or lunch ideas too."
EXTRAS_OFF = "Done 👍 No more breakfast or lunch texts. Your dinner pick stays daily. (LESS again for every other day.)"
EVERY_OTHER_DAY = "Got it 👍 I'll send your pick every other day now. Reply MORE to go back to daily."
ALREADY_LEAST = "You're on every other day already, the quietest I go. STOP pauses me completely."


def missing_most(recipe: str | None, can_shop: bool) -> str:
    """They don't have what Bud thought they had: own it, then give three ways forward."""
    lines = ["Ah, my picture of your fridge must be out of date 🙈", ""]
    lines.append("• 👎 for an idea from what you do have")
    if recipe:
        lines.append(f"• SHOP to add what {recipe} needs to your list" if can_shop else
                     f"• RECIPE {recipe} for the full recipe and a shopping list")
    lines.append("• 📸 a quick fridge photo and I'll re-plan from what's really there")
    return "\n".join(lines)


def ask_craving(examples: list[str]) -> str:
    return ("Let's go off-script 😄 What are you in the mood for?\n\n"
            f"Name a dish or a cuisine (like {', '.join(examples[:3])}) and I'll get you the recipe and a shopping list.")


# Ignored picks: playful about Bud, never about them (no food or money guilt), then Bud steps back --------------
IGNORED_PS = {
    4: "P.S. Three ideas and not a peep 🥺 Even a 👎 would make my day.",
    5: "P.S. I'm starting to think my ideas are bad 😅 Reply 👎 and I'll do better, promise.",
    6: "P.S. Last call 🫣 If I don't hear back, I'll stop sending these so I'm not cluttering your chat.",
}
IGNORED_FINAL_AT = 7
STEPPING_BACK = ("These daily ideas don't seem to be landing, so I'll stop sending them for now 🫡\n\n"
                 "I'm still here: text me \"what's for dinner?\" or send a fridge photo any time and I'll pick right back up.")
WELCOME_BACK = "👋 Welcome back! Your daily ideas are back on."


def noticed(cuisine: str) -> str:
    return (f"🧠 You've been asking about a lot of {cuisine} food lately, so I'll suggest a bit more of it. "
            "Just a guess though. Tell me anytime what you'd rather eat.")


def notebook(persona: dict, dislikes: list[str], diet: str, saved: int, learned: list[str], month: float,
             cooked_week: int) -> str:
    """What Bud has learned, shown back: the more it knows, the better the picks (and the more it's theirs)."""
    p = persona or {}
    lines = ["📓 What I know about your kitchen", ""]
    if p.get("likes"):
        lines.append("❤️ Loves: " + ", ".join(p["likes"][:6]))
    if p.get("cuisines"):
        lines.append("🌍 Cuisines: " + ", ".join(p["cuisines"][:5]))
    if learned:
        lines.append("🧠 Picked up that you love: " + ", ".join(learned[:4]))
    if "variety" in p:
        from .taste import describe
        lines.append("🎲 How adventurous: " + describe(float(p["variety"])))
    meals = p.get("meals") or {}
    if meals:
        lines.append("🕒 Eats: " + ", ".join(f"{m} {_clock_short(t)}" for m, t in meals.items()))
    if p.get("loved"):
        lines.append("⭐ Favourites: " + ", ".join(p["loved"][:5]))
    if dislikes:
        lines.append("🚫 Skips: " + ", ".join(dislikes[:6]))
    if diet:
        lines.append("🥗 Diet: " + diet)
    lines.append("")
    lines.append(f"📚 {saved} saved recipe{'' if saved == 1 else 's'} · 🍳 {cooked_week} cooked this week"
                 + (f" · 💰 ≈${month:.0f} saved this month" if month >= 1 else ""))
    lines += ["", "Tell me more any time: a voice note about what you love, or TASTE <how you eat>."]
    return "\n".join(lines)


def watchlist_text(watchlist: list[Entry], untracked_count: int) -> str:
    if not watchlist:
        return "Nothing at risk right now 👌"
    return "At risk this week:\n" + "\n".join(f"• {e.item.name}: {_when(e)}" for e in watchlist)


def added(names: list[str], untracked: list[str]) -> str:
    msg = f"Got it: {', '.join(names)} ✅" if names else "Got it ✅"
    if untracked:
        msg += f"\n(Skipping {', '.join(untracked)}: keeps fine.)"
    return msg + "\nI'll ping you before anything turns."


def photo_added(added_reply: str, unsure: list[str], has_prices: bool) -> str:
    parts = [added_reply] if added_reply else []
    if has_prices:
        parts.append("Saved your prices too, so I can show what you keep.")
    if unsure:
        names = ", ".join(f'"{u}"' for u in unsure[:3])
        parts.append(f"I skipped {names} because I couldn't read it clearly. If it's food, text e.g. \"bought baby spinach\".")
    return " ".join(parts) if parts else PHOTO_UNREADABLE


def _n(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def weekly_receipt(used: int, tossed: int, leftovers_finished: int, kept: float = 0.0, priced: int = 0) -> str:
    total = used + tossed
    rate = f"{round(100 * used / total)}%" if total else "n/a"
    head = f"Your week: ${kept:.2f} of food kept out of the bin, at your own receipt prices. " if kept else "Your week: "
    tail = ("" if kept and priced == used else
            "\nRoughly what did the rest save you? Reply with a dollar amount.")
    return (
        f"{head}{_n(used, 'item')} used before {'it' if used == 1 else 'they'} turned, "
        f"{_n(leftovers_finished, 'leftover')} finished, {tossed} tossed. Rescue rate {rate}.{tail}"
    )


# Onboarding, household invites, payments (used by service.py) ---------------
ASK_SIZE = "You're in 🎉 Quick one: how many people eat from your fridge?"
PHOTO_ASK = ("Last step: send a photo of your fridge (or a receipt). "
             "Or just type it, e.g. \"bought spinach, chicken\".")
JOIN_PROMPT = (
    "Hi, I'm FridgeBud. You've been invited to share a fridge. I text once a day, around 4:30 PM, with "
    "what to use up and a dinner idea. Reply YES to get the texts (reply STOP anytime)."
)
MEMBER_IN = (
    "You're in. You'll get the same 4:30 text as the rest of your fridge. Reply anytime: MADE, "
    "TOSSED <item>, \"bought milk, kale\", or send a photo."
)
ALREADY_SIGNED_UP = "That number is already signed up. Reply YES to the welcome text to start."


def _to(number: str) -> str:
    return f"to {number}" if number else "to this number"


def size_ack(size: int, code: str, number: str) -> str:
    if size <= 1:
        return "Got it, just you."
    return (f"Got it, {size} of you. Anyone else who eats from this fridge can get the 4:30 text too: "
            f"have them text JOIN {code} {_to(number)}.")


def invite_nudge(code: str, number: str) -> str:
    return f"Who else eats from this fridge? They can get the 4:30 text too: have them text JOIN {code} {_to(number)}."


def dollars_logged(amount: float) -> str:
    return f"Thanks. Logged about ${amount:.0f} kept this week (your estimate)."


def paywall(price: str, link: str, trial_days: int, grace_days: int) -> str:
    return (f"Your {trial_days} free days end today. To keep the 4:30 texts, the household plan is {price}: {link}\n"
            f"If you'd rather not, do nothing: the daily texts pause in {grace_days} days.")


PAID_THANKS = "Payment received, thank you. Your 4:30 texts carry on as usual."


# Instant value: the rescue plan, dinner on demand, and "this is already old" ----
NO_IDEA = ("I don't have a dinner that fits what's on your list yet. Text what else you have "
           "(e.g. \"bought eggs, tortillas\") and ask again.")
FRIDGE_AGE = "Anything in there already a few days old? Reply e.g. OLD spinach and I'll move it up."


def rescue_plan(rows: list[tuple[str, str]], more: int, suggestion: Suggestion | None) -> str:
    """rows = (item name, 'today' | 'tomorrow' | 'Thu' ...), soonest first."""
    lines = ["Use first:"] + [f"• {name}: {'today' if when == 'today' else 'by ' + when}" for name, when in rows]
    if more > 0:
        lines.append(f"(+{more} more that keep longer)")
    if suggestion:
        lines.append(_idea(suggestion))
        lines += ["", PICK_FOOTER]
    return "\n".join(lines)


def dinner_idea(suggestion: Suggestion, same_again: bool = False) -> str:
    lead = "That's the only thing your fridge makes right now, so same again:\n" if same_again else ""
    return lead + _idea(suggestion) + "\n\n" + PICK_FOOTER


def marked_old(names: list[str]) -> str:
    return f"Moved up: {', '.join(names)}."


def disliked(names: list[str]) -> str:
    return f"Noted, no more {', '.join(names)} 👍"


def liked(names: list[str], removed: list[str]) -> str:
    return f"Love that 😋 More like {', '.join(names)} coming up." + (f" (Back on the menu: {', '.join(removed)}.)" if removed else "")


def diet_set(note: str) -> str:
    return f"Got it, I'll cook with that in mind: {note}. Text DIET any time to update it."


def recipe_kept(title: str) -> str:
    return f"Saved {title} 📚 RECIPES shows them all."


def already_kept(title: str) -> str:
    return f"{title} is already in your saved recipes 👍"


def keep_which(name: str | None) -> str:
    if name:
        return (f"I couldn't find {name} among the recipes I've shown you. Ask for it first (e.g. RECIPE {name}), "
                "then reply SAVE.")
    return "Which recipe? Ask for one first (e.g. RECIPE egusi soup), or reply SAVE to the recipe message itself."


def recipe_declined(title: str) -> str:
    return f"Binned the {title} 🗑️ Ask me for another any time."


def rate_ai_recipe(title: str) -> str:
    return "Untested recipe 🧪 Once you've tried it:\nReply 👍 if it's a keeper\nReply 👎 if it's a no"


def have_yes(rows: list[tuple[str, str, str | None]]) -> str:
    """rows = (name, 'added Mon Nov 16', 'by Thu' or None when it isn't a tracked perishable)."""
    if len(rows) == 1:
        name, added_on, when = rows[0]
        tail = f", use {'today' if when == 'today' else 'by ' + when}" if when else ""
        return f"On your list: {name} ({added_on}{tail}) 👍\n(From what you've sent me, so check the fridge if it matters.)"
    return ("On your list:\n" + "\n".join(f"• {n} ({a}{', use by ' + w if w else ''})" for n, a, w in rows)
            + "\n(From what you've sent me, so check the fridge if it matters.)")


def have_no(name: str, closed: tuple[str, str, str] | None) -> str:
    """closed = (name, 'used' | 'tossed', 'Nov 15') if they recently finished some."""
    if closed:
        return f"Not on your list now: the {closed[0]} was {closed[1]} on {closed[2]}. Got more? Text \"bought {name}\"."
    return f"I don't have {name} on your list. Got some? Text \"bought {name}\"."


def everything(tracked: list[tuple[str, str]], others: list[str]) -> str:
    if not tracked and not others:
        return "Your list is empty. Send a photo of your fridge or text what you bought."
    lines = []
    if tracked:
        lines.append("On your list, soonest first:")
        lines += [f"- {name}: {'today' if when == 'today' else 'by ' + when}" for name, when in tracked]
    if others:
        lines.append(("Also have (keeps well or not tracked): " if tracked else "On your list (keeps well or not tracked): ")
                     + ", ".join(others) + ".")
    return "\n".join(lines)


# Recipe book, shopping list, audio ------------------------------------------
AI_LABEL = "🧪 AI-written recipe: treat the amounts as a starting point."
LINK_PARTIAL = "AI filled in the amounts and steps (the link only showed a title and caption)."
# Notes already stored on saved recipes in the old wording: shown in the new, shorter wording.
LEGACY_NOTES = {
    "Taken from the video itself: Google's Gemini watched it and wrote down what was said and shown.": "WATCHED",
    "Taken from the video itself: Google's Gemini watched it and wrote down what was said and shown. "
    "Where the video skipped an amount or a step, AI filled it in.": "WATCHED_GAPS",
    "The link only gave me its title and caption (I can't watch videos), so AI filled in the amounts and steps.":
        "LINK_PARTIAL",
}
NEEDS_AI = "I can't do that one right now: my AI key isn't switched on."
AI_DOWN = "My recipe brain didn't answer just now. Try again in a minute."
NO_CURRENT = "Pick a recipe first: send me a link, or ask for one, e.g. RECIPE jollof rice."
NO_RECIPES = "You haven't saved any recipes yet. Send me a link (a recipe site, YouTube, TikTok) or ask e.g. RECIPE jollof rice."
SHOPPING_EMPTY = "Your shopping list is empty."
ROAST_EMPTY = "There's nothing in here to roast yet. Send me a photo of your fridge first."


def link_failed(site: str, why: str) -> str:
    return (f"I couldn't read that link ({site or 'the site'}: {why}). Some sites, Instagram especially, hide posts "
            f"unless you're logged in. Paste the caption or the recipe text here and I'll save it.")


def link_no_recipe(what: str) -> str:
    return f"I opened it, but I couldn't find a dish to cook in \"{what[:80]}\". If it is a recipe, paste the text here and I'll save it."


def not_a_dish(dish: str) -> str:
    return f"I couldn't turn \"{dish[:60]}\" into a recipe. Try the name of a dish, e.g. RECIPE jollof rice."


_TRACKING = {"si", "fbclid", "igsh", "igshid", "feature", "ref", "ref_src", "mibextid", "s", "t"}


def tidy(name: str) -> str:
    """What a shopper needs to see: 'basmati rice, washed and uncooked' -> 'basmati rice',
    'sumac (shawarma spice mix)' -> 'sumac', 'garlic powder or minced garlic' -> 'garlic powder'."""
    n = re.sub(r"\([^)]*\)", "", name)
    n = re.split(r"\s+or\s+", n.split(",")[0])[0]
    return re.sub(r"\s+", " ", n).strip(" .-:")


def unique_names(names: list[str]) -> list[str]:
    out, seen = [], set()
    for raw in names:
        t = tidy(raw)
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def clean_url(url: str) -> str:
    """The real link, minus share trackers (?si=, utm_*), so it is short enough to read."""
    try:
        p = urlsplit(url)
    except ValueError:
        return url
    if not p.scheme:
        return url
    keep = [(k, v) for k, v in parse_qsl(p.query) if not (k.startswith("utm_") or k in _TRACKING)]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(keep), ""))


def recipe_card(title: str, source_line: str, servings: str, minutes: int | None, have: list[str], missing: list[str],
                staples: list[str], as_of: str | None, lead: str = "") -> str:
    """Built for a tired person: the first thing after the title answers 'can I make this now?'.
    Notes in brackets, duplicates and prep words are dropped; spices share one line; details live in COOK."""
    meta = " · ".join(p for p in ((servings if "serv" in servings.lower() else f"serves {servings}") if servings else "",
                                  f"{minutes} min" if minutes else "") if p)
    have, missing = unique_names(have), unique_names(missing)
    gone = {x.lower() for x in missing}
    staples = [x for x in unique_names(staples) if x.lower() not in gone]
    lines = ([lead] if lead else []) + [f"📖 {title}", source_line] + ([meta[0].upper() + meta[1:]] if meta else []) + [""]
    many = len(missing) > 3
    if as_of is None:
        lines.append(f"🛒 You'll need {len(missing)} things:" if many else "🛒 You'll need: " + ", ".join(missing))
    elif not missing:
        lines.append("✅ You have everything for this.")
    elif not many:
        lines.append("🛒 Almost there. Just missing: " + ", ".join(missing))
    else:
        lines.append(f"🛒 Needs a shop: {len(missing)} things missing.")
    if have and missing and as_of is not None:
        lines.append("✅ You have: " + ", ".join(have[:6]) + (f" +{len(have) - 6} more" if len(have) > 6 else ""))
    if many:
        lines.append("")
        main = [x for x in missing if not is_spice(x)]
        spices = [x for x in missing if is_spice(x)]
        if as_of is not None:
            lines.append("Need:")
        lines += [f"• {x}" for x in main[:15]]
        if len(main) > 15:
            lines.append(f"• +{len(main) - 15} more")
        if spices:
            lines.append("• Spices: " + ", ".join(spices))
    lines.append("")
    if staples:
        lines.append("Assumes you have: " + ", ".join(staples) + ".")
    if as_of is not None:
        first, *rest = as_of.split("\n")
        lines.append("🧊 Checked against " + first.rstrip(":"))
        lines += rest
    lines.append("")
    if many:
        lines.append("Reply SHOP to add them all · COOK for steps · AUDIO to listen")
    elif missing:
        lines.append("COOK for steps · SHOP for the missing bits · AUDIO to listen")
    else:
        lines.append("COOK for steps · AUDIO to listen")
    return "\n".join(lines)


def recipe_steps(title: str, ingredients: list[tuple[str, str]], steps: list[str], source_line: str) -> str:
    lines = [f"📖 {title}", source_line, "", "Ingredients:"]   # source line carries the AI label: never drop it
    lines += [f"• {qty + ' ' if qty else ''}{name}" for name, qty in ingredients]
    lines += ["", "Steps:"]
    for n, s in enumerate(steps, 1):                          # a gap between steps: easy to find your place mid-cook
        lines += ([""] if n > 1 else []) + [f"{n}. {s}"]
    lines += ["", "Done? Reply MADE · Prefer to listen? AUDIO"]
    return "\n".join(lines)


def recipe_list(rows: list[tuple[str, str]]) -> str:
    """rows = (title, 'from youtube.com' | 'AI-generated' | 'pasted by you')"""
    return "📚 Your recipes:\n\n" + "\n".join(f"• {title} ({where})" for title, where in rows) + "\n\nAsk for one by name."


def shop_added(added: list[str], total: int) -> str:
    if not added:
        return f"Already on your list 👍 ({total} items). SHOPPING shows it."
    return "\n".join(["🛒 Added to your shopping list:", ""] + _by_aisle(added)
                     + ["", f"{total} on the list now. SHOPPING shows it all."])


def _by_aisle(names: list[str]) -> list[str]:
    """Short lists stay flat; longer ones get aisle headings with a gap between, so they never read as a wall."""
    if len(names) <= 6:
        return [f"• {n}" for n in names]
    lines: list[str] = []
    for name, rows in grouped(names):
        lines += ["", f"{EMOJI[name]} {name}"] + [f"• {n}" for n in rows]
    return lines[1:]


def shopping_list(names: list[str]) -> str:
    return "\n".join([f"🛒 Your shopping list ({len(names)})", ""] + _by_aisle(names)
                     + ["", "Shopped? Send the receipt and I'll tick things off.", "Want it delivered? Reply ORDER"])


def recipe_shopping(title: str, names: list[str], listed: list[bool]) -> str:
    todo = [n for n, on in zip(names, listed) if not on]
    lines = [f"🛒 For {title}:", ""] + [f"• {n}" + (" ✓" if on else "") for n, on in zip(names, listed)] + [""]
    if todo and len(todo) < len(names):
        lines.append(f"✓ = already on your list. Reply SHOP to add the other {len(todo)}.")
    elif todo:
        lines.append("Reply SHOP to add them to your list.")
    else:
        lines.append("All on your list ✓ · SHOPPING shows everything.")
    return "\n".join(lines)


def recipe_list_done(title: str) -> str:
    return f"You've got everything for {title} 🎉 Reply COOK when you're ready."


def no_recipe_for_list(dish: str) -> str:
    return f"I don't have a {dish} recipe saved yet. Reply RECIPE {dish} and I'll pull one up, then SHOP."


def shop_ticked(names: list[str]) -> str:
    return f"✔️ Ticked off: {', '.join(names)}"


def plain_script(title: str, ingredients: list[tuple[str, str]], steps: list[str]) -> str:
    """A cook-along script without the AI: reads the recipe straight."""
    need = ", ".join(f"{qty} {name}".strip() for name, qty in ingredients)
    body = " ".join(f"Step {n}. {s}" for n, s in enumerate(steps, 1))
    return f"Let's cook {title}. You will need: {need}. {body} That's it. Enjoy."


def audio_caption(title: str, ai_written: bool, have: list[str] | None = None, missing: list[str] | None = None) -> str:
    text = f"🎧 Cook-along: {title}." + (" Script written by AI from the recipe." if ai_written else "")
    if have:
        text += f"\nYou have: {', '.join(have[:8])}."
    if missing:
        text += f"\nStill need: {', '.join(missing[:8])}. Reply SHOP to add them to your list."
    return text


def saved_recipe_hint(title: str, uses: list[str]) -> str:
    return f"From your saved recipes: {title} would use your {', '.join(uses[:3])}. Reply RECIPE {title}."


# Videos ----------------------------------------------------------------------
WATCHED = "🎬 Taken from the video (AI watched it)."
WATCHED_GAPS = "🎬 Taken from the video (AI watched it and filled small gaps)."
VIDEO_NEEDS_KEY = ("I can't watch videos yet: that needs a Gemini key (GEMINI_API_KEY in .env). "
                   "Until then, send me the link or paste the caption.")
VIDEO_TOO_BIG = "That video is too big for me (about 14 MB is my limit). Trim it, or send the link if it's on YouTube."
VIDEO_NOT_RECIPE = "I watched it, but I couldn't see anyone cooking a dish in that video."


def video_failed(why: str) -> str:
    return f"I couldn't watch that video ({why[:100]}). Try again in a minute, or paste the recipe text."


# Blocked sites and recipe screenshots -------------------------------------------
SHOT_NOT_RECIPE = "I couldn't find a recipe in that picture. Send a screenshot that shows the ingredients and the steps."
SHOT_MORE = "If the recipe carries on, send the next screenshot and I'll merge it in."
SHOT_NOTE = "AI filled in the parts the screenshot didn't show."


def link_blocked(site: str, code: str, dish: str) -> str:
    text = (f"{site} blocks automated readers like me (it answered {code}), so I can't open that page. "
            f"Two easy ways round it: send me a screenshot of the recipe (ingredients and steps, more than one is fine), "
            f"or paste the text.")
    if dish:
        text += f" Or reply RECIPE {dish} and I'll write my own version, labelled as AI-written."
    return text


# Menus: a home version of a restaurant dish ---------------------------------------
MENU_NONE = "I couldn't pick out any dishes on that menu. Try a closer screenshot, or just tell me the dish, e.g. RECIPE butter chicken."
MENU_EXPIRED = "Which dish did you mean? Send the menu again, or name it, e.g. RECIPE butter chicken."


def menu_ask(place: str, dishes: list[dict]) -> str:
    where = f" at {place}" if place else ""
    if len(dishes) == 1:
        d = dishes[0]
        about = f" ({d['description']})" if d["description"] else ""
        return (f"I can see {d['name']}{about}{where}. Want me to write a home version you can cook? "
                f"Reply YES, or tell me if I've read it wrong.")
    lines = [f"I can see {len(dishes)} dishes{where}. Which one should I turn into a home recipe?"]
    lines += [f"{n}. {d['name']}" for n, d in enumerate(dishes, 1)]
    lines.append("Reply with the number.")
    return "\n".join(lines)


def menu_lead(dish: str, place: str) -> str:
    return (f"Here's my home version of {dish}" + (f" from {place}" if place else "") +
            ". It's my own take on the dish, not the restaurant's recipe.")


# Ordering: the list is always shown back for a yes before anything goes to Instacart ----------------
ORDER_BUTTON = "🛒 Shop on Instacart"
ORDER_EMPTY = "Your list is empty, so there's nothing to order yet. Reply SHOP under a recipe to fill it."
ORDER_CANCELLED = "No problem, nothing sent 👍 Your list is still here: SHOPPING"
ASK_POSTAL = "Quick one before I send it 📍 What's your postal code? I'll check which stores near you deliver."
ORDER_CONFIRM = {"✅", "✔", "yes", "y", "yep", "yeah", "send", "send it", "go", "go ahead", "confirm", "looks good",
                 "perfect", "ok", "okay", "👍", "do it", "order it", "sounds good", "that's it", "thats it", "all good"}
ORDER_CANCEL = {"cancel", "no", "nah", "not now", "never mind", "nevermind", "don't", "dont", "❌", "wait"}


def order_review(names: list[str], note: str = "") -> str:
    lines = ([note, ""] if note else []) + [f"🛒 Before I send this to Instacart, check your list ({len(names)}):", ""]
    lines += _by_aisle(names)
    lines += ["", "Reply ✅ to send it", "Reply \"remove eggs\" or \"add milk\" to change it", "Reply CANCEL to stop"]
    return "\n".join(lines)


def order_sent(plan: dict, on_instacart: bool) -> str:
    lines = (["✅ Sent to Instacart. Tap below, pick your store and check out there. I never see your payment."]
             if on_instacart else
             ["Instacart isn't switched on for me yet, so here's your list as a page you can shop from (or share)."])
    tips = []
    if plan.get("main"):
        tips.append(f"🏪 Most likely to have most of it: {plan['main']}")
    for sp in plan.get("specialty") or []:
        what = ", ".join(sp["items"][:4])
        tips.append(f"🧺 {what}: try {sp['store']}" if sp.get("store") else
                    f"🧺 {what}: {sp['kind']} is your best bet (big supermarkets often don't stock these)")
    if tips:
        lines += [""] + tips + ["(Store picks are my best guess from what's near you, not live stock.)"]
    lines += ["", "Shopped? Send me the receipt and I'll update your fridge."]
    return "\n".join(lines)


def instacart_line(url: str) -> str:
    return f"Shop on Instacart: {url}"


# where they are ------------------------------------------------------------
ASK_PLACE = ("One more question: what city are you in? Then my daily 4:30 text arrives at 4:30 your time. "
             "Just reply with the city, e.g. \"Vancouver\".")
PLACE_PS = "P.S. What city are you in? I'm on Toronto time for you right now. Just reply with your city."


def _clock(hour: int, minute: int) -> str:
    return f"{hour % 12 or 12}:{minute:02d} {'PM' if hour >= 12 else 'AM'}"


def place_set(said: str, zone: str, hour: int, minute: int) -> str:
    name = zone.rsplit("/", 1)[-1].replace("_", " ") if "/" in said else said.strip().title()
    msg = f"Got it, {name} time. Your daily text comes at {_clock(hour, minute)} there."
    lowered = said.lower()
    if zone == "Europe/London" and "london" in lowered and not any(w in lowered for w in ("uk", "england")):
        msg += " Meant London, Ontario? Reply TIMEZONE London Ontario."
    return msg + " Moved? Text TIMEZONE and your city any time."


# Bud reaching out ----------------------------------------------------------
def evening_checkin(recipe: str | None, names: list[str], meal: str = "dinner") -> str:
    """After their meal, following a daily idea nobody answered: ask when the answer is actually known."""
    first = names[0] if names else "it"
    if recipe:
        return f"How was {meal}? 🍽️ Made the {recipe}? Reply MADE\nDidn't get to it? USED / TOSSED / GOOD {first}"
    return f"Quick one 👋 Did the {', '.join(names[:3])} get used?\nReply USED, TOSSED or GOOD {first}"


def activation_chase(n: int) -> str:
    """A home that said yes but never told Bud what's in the fridge."""
    if n <= 1:
        return "Want tonight's dinner sorted? 📸 Send me one photo of your fridge."
    return "Last nudge from me 🙂 One fridge photo and I'll start flagging food before it goes off."


def stale_list(days: int, standalone: bool = False, empty: bool = False) -> str:
    """The list Bud plans from is old or empty, so its picks are guesses."""
    if empty:
        return "Your fridge list is empty on my side 🫙 Send a photo after your next shop."
    if standalone:
        return f"It's been {days} days since your last fridge photo 📸 Send a fresh one?"
    return f"P.S. Your fridge list is {days} days old. A fresh photo keeps these picks right."
