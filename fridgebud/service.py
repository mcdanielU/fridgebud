"""The app around the engine: onboarding, households, voice test, Sunday receipt, paywall.

    inbound text  -> Service.handle_message() -> list of replies
    every minute  -> Service.tick()           -> list of (phone, text) to send
    web signup    -> Service.signup_web()

The engine (engine.py) still makes every food decision. This layer adds what a
launch needs and nothing else. All state changes happen under one lock, so
the web server can call it from several threads.
"""
from __future__ import annotations

import re
import threading
import time
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import habit, stores, taste
from . import recipes as recipes_mod, links, messages, places, plan, receipt
from .accounts import Accounts, Member, Profile
from .audio import Audio
from .audiocache import AudioCache, recipe_key
from .imagery import PhotoCache, dish_key, dish_prompt, youtube_thumbnail
from .capture import CapturedItem, FixedExtractor
from .commands import ALWAYS, service_command
from .engine import Engine
from .fallback import ReplyInterpreter, validate
from .models import Household
from .parser import parse
from .recipebook import Recipe, RecipeBook, match_local, recipe_from_dict, same_food
from .sms import normalize_phone, timezone_for
from .video import youtube_id
from .voice import budify

_WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}
_JOIN = re.compile(r"^\s*join\s+([a-z0-9]{2,8})\s*[.!]*$", re.I)
_REFERRAL = re.compile(r"^\s*(?:hi|hey|hello)\s+([a-z0-9]{2,8})\s*[.!]*$", re.I)
_OUTCOMES = ("made", "used", "tossed", "still_good")
_MEALS = ("breakfast", "brunch", "lunch", "dinner", "snacks")


def _at(event: dict) -> datetime:
    return datetime.fromisoformat(event["at"])


_DOLLARS = re.compile(r"^\$?\s*(\d{1,4}(?:\.\d{1,2})?)\s*(?:dollars?|bucks)?[.!]*$", re.I)


@dataclass
class Config:
    number: str = ""             # FridgeBud's own phone number, shown in invites and receipts
    price: str = ""              # e.g. "$6.99 a month"; shown on the landing page and the paywall text
    payment_link: str = ""       # the paywall is off until this is set
    trial_days: int = 14
    grace_days: int = 3          # unpaid households keep getting texts this long after the paywall text
    receipt_hour: int = 11       # Sunday receipt goes out at this local hour
    fallback_daily_cap: int = 5  # free-form replies sent to the model per household per day
    agent_daily_cap: int = 60    # free-form messages handled by the agent per household per day
    ai_idea_daily_cap: int = 3   # AI-written dinner ideas per household per day (when the dish list fits the fridge poorly)
    force_voice: str = ""        # 'bud' or 'plain' for everyone; empty keeps the 50/50 voice test
    checkin_hour: int = 20       # evening "how did it go?" text, local time, only after an unanswered 4:30 text
    quiet_hour: int = 21         # Bud never starts a conversation at or after this local hour
    public_url: str = ""         # e.g. https://fridgebud.fly.dev: turns on the shopping list's export button


def _size(text: str) -> int | None:
    t = text.strip().lower()
    if len(t) > 30:
        return None
    if re.fullmatch(r"(?:just |only )?(?:me|myself)(?: for now)?[.!]*", t):
        return 1
    m = re.search(r"\b(\d{1,2})\b", t)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 15 else None
    for word, n in _WORD_NUMBERS.items():
        if re.search(rf"\b{word}\b", t):
            return n
    return None


class Service:
    def __init__(self, engine: Engine, config: Config | None = None, interpreter: ReplyInterpreter | None = None,
                 chef=None, agent=None, fetch=None, watcher=None, instacart=None):
        self.engine = engine
        engine.reward = self._made_reward
        engine.skip_today = self._skip_today
        engine.idea_fallback = self._ai_idea
        self._noticed: dict[int, str] = {}
        self._woken: dict[int, bool] = {}
        self.store = engine.store
        self.accounts = Accounts(self.store)
        self.book = RecipeBook(self.store)
        self.audio_cache = AudioCache(self.store)
        self.photo_cache = PhotoCache(self.store)
        self._replying: dict[str, tuple[float, str]] = {}
        self.config = config or Config()
        self.interpreter = interpreter
        self.chef = chef            # chef.Chef: recipes, matching, narration (needs an API key)
        self.agent = agent          # agent.Agent: free-form messages (needs an API key)
        self.fetch = fetch or links.fetch
        self.instacart = instacart  # instacart.Instacart: turns the shopping list into an Instacart link (needs a key)
        self.watcher = watcher      # video.GeminiWatcher: watches YouTube links and video files (needs a Gemini key)
        self.lock = threading.RLock()

    # helpers ----------------------------------------------------------
    def _voice(self, prof: Profile) -> str:
        return self.config.force_voice or prof.voice

    def _say(self, prof: Profile, text, seed: int = 0):
        if not isinstance(text, str) or isinstance(text, messages.Card):  # Audio, or a message with buttons
            return text
        return budify(text, seed) if self._voice(prof) == "bud" else text

    def _profile(self, h: Household, now: datetime, source: str = "legacy") -> Profile:
        """Every household has a profile; ones created before this layer existed get one lazily."""
        prof = self.accounts.get(h.id)
        if prof is None:
            prof = self.accounts.create(h.id, source, now, stage="active" if h.opted_in else "new",
                                        consented_at=now if h.opted_in else None)
            self.store.log(h.id, now, "voice_assigned", voice=prof.voice)
        return prof

    def _recipients(self, h: Household) -> list[str]:
        return [h.phone] + self.accounts.active_member_phones(h.id)

    # web signup -------------------------------------------------------
    def signup_web(self, raw_phone: str, now: datetime, ip: str = "", consent_wording: str = "",
                   ref: str = "") -> tuple[bool, str]:
        """Returns (True, phone) when a welcome text should be sent, else (False, reason)."""
        phone = normalize_phone(raw_phone)
        if phone is None:
            return False, "That doesn't look like a Canadian or US mobile number."
        with self.lock:
            if self.store.get_household_by_phone(phone) or self.accounts.member(phone):
                return False, messages.ALREADY_SIGNED_UP
            h = self.store.create_household(phone)
            h.opted_in = False                  # texts still wait for a YES reply
            h.timezone = timezone_for(phone)
            self.store.save_household(h)
            referrer = self.accounts.by_code(ref) if ref else None
            self.store.log(h.id, now, "signup", source="web")
            # CASL record: who agreed, when, from where, and the exact words they agreed to.
            self.store.log(h.id, now, "consent_web", ip=ip, wording=consent_wording)
            prof = self.accounts.create(h.id, "web", now, referred_by=referrer.household_id if referrer else None)
            self.store.log(h.id, now, "voice_assigned", voice=prof.voice)
            if referrer:
                self.store.log(referrer.household_id, now, "referral", new_household=h.id)
            return True, phone

    # inbound ----------------------------------------------------------
    def handle_message(self, phone: str, text: str, now: datetime, captured: list[CapturedItem] | None = None,
                       has_media: bool = False) -> list[str]:
        """One inbound text. `captured` is what the vision model found in any photo (already extracted)."""
        with self.lock:
            member = self.accounts.member(phone)
            home = self.store.get_household(member.household_id) if member else self.store.get_household_by_phone(phone)
            said = (text or "").strip()
            if home is not None:
                self.store.log(home.id, now, "heard")
                if (home.persona or {}).get("snoozed"):
                    home.persona = {k: v for k, v in home.persona.items() if k != "snoozed"}
                    self.store.save_household(home)
                    self.store.log(home.id, now, "woke")
                    self._woken[home.id] = True
            if home is not None and home.opted_in and said and len(said) < 200 and "http" not in said:
                self._signal(home, taste.cuisine_of(said), 0.5, now)       # "craving suya" counts a little
        replies = self._handle_message(phone, text, now, captured, has_media)
        with self.lock:
            home = self.store.get_household(member.household_id) if member else self.store.get_household_by_phone(phone)
            noticed = self._noticed.pop(home.id, None) if home is not None else None
            back = self._woken.pop(home.id, False) if home is not None else False
        if noticed:
            replies = list(replies) + [messages.noticed(noticed)]
        if back:
            replies = list(replies) + [messages.WELCOME_BACK]
        return replies

    def _signal(self, h: Household, cuisine: str | None, weight: float, now: datetime) -> None:
        """One taste signal (see taste.py). Remembers to say so if it just tipped a cuisine into "theirs"."""
        if not cuisine:
            return
        h = self.store.get_household(h.id)
        if weight >= 1 and taste.weights(h.persona).get(cuisine, 0.0) <= 0 and cuisine not in recipes_mod.MAINSTREAM:
            h.persona = taste.nudge_variety(h.persona, 0.8, 0.05)        # they went looking for something new themselves
        persona, new = taste.learn(h.persona, cuisine, weight, now.date())
        h.persona = persona
        self.store.save_household(h)
        self.store.log(h.id, now, "taste_signal", cuisine=cuisine, weight=weight)
        if new:
            self._noticed[h.id] = cuisine

    def _handle_message(self, phone: str, text: str, now: datetime, captured: list[CapturedItem] | None = None,
                        has_media: bool = False) -> list[str]:
        text = text or ""
        with self.lock:
            member = self.accounts.member(phone)
            if member:
                return self._member_message(member, text, now, captured, has_media)

            h = self.store.get_household_by_phone(phone)
            if h is None:
                join = _JOIN.match(text)
                owner = self.accounts.by_code(join.group(1)) if join else None
                if owner:
                    self.accounts.invite_member(phone, owner.household_id, now)
                    self.store.log(owner.household_id, now, "member_invited", phone=phone)
                    return [messages.JOIN_PROMPT]
            return self._household_message(phone, text, now, captured, has_media)

    def _member_message(self, member: Member, text: str, now: datetime, captured, has_media) -> list[str]:
        kind = parse(text).kind
        if kind == "stop":  # leaves this person only; the household carries on
            self.accounts.set_member_status(member.phone, "stopped", now)
            self.store.log(member.household_id, now, "member_opt_out", phone=member.phone)
            return [messages.STOPPED]
        if member.status != "active":
            if kind == "start":
                self.accounts.set_member_status(member.phone, "active", now)
                self.store.log(member.household_id, now, "member_consent", phone=member.phone, method="reply_yes")
                return [messages.MEMBER_IN]
            return [messages.JOIN_PROMPT]
        owner = self.store.get_household(member.household_id)
        if not owner.opted_in:
            return ["This fridge's texts are paused right now."]
        return self._household_message(owner.phone, text, now, captured, has_media, sender=member.phone)

    def _household_message(self, phone: str, text: str, now: datetime, captured, has_media,
                           sender: str | None = None) -> list[str]:
        h = self.store.get_household_by_phone(phone)
        was_in = bool(h and h.opted_in)
        prof = self._profile(h, now) if h else None
        intent = parse(text)

        # Answers to questions we asked, which the engine's parser doesn't know about.
        # "got any eggs?" is a question, though the engine's parser reads "got ..." as "bought ...".
        asked = service_command(text)
        question = bool(asked and asked[0] == "have" and (text.strip().endswith("?") or re.match(r"\s*got any\b", text, re.I)))
        if was_in and not has_media:
            picked = self._menu_pick(h, text, intent.kind, now)   # answering "which dish?" after a menu photo
            if picked is not None:
                self.store.log(h.id, now, "inbound", text=text, media=0)
                return [self._say(prof, s) for s in picked]
        if was_in and not has_media:
            looped = self._loop_reply(h, prof, text, now)
            if looped is not None:
                self.store.log(h.id, now, "inbound", text=text, media=0)
                return [self._say(prof, s) for s in looped]
        if was_in and not has_media and (intent.kind == "unknown" or question or (asked and asked[0] in ALWAYS)):
            special = self._answer(h, prof, text, now)
            if special is not None:
                self.store.log(h.id, now, "inbound", text=text, media=0)
                return [self._say(prof, s) for s in special]
        if was_in and not has_media and intent.kind == "start" and text.strip().lower().strip(" .!") in {"yes", "y"}:
            self.store.log(h.id, now, "inbound", text=text, media=0)
            handled = self._agent(h, text, now) if self.agent is not None else None
            return [self._say(prof, s) for s in (handled or [messages.ALREADY_IN])]
        if was_in and not has_media and intent.kind in {"liked", "dislike"} and intent.items:
            rated = self._rate_recipe(h, intent.kind, intent.items, now)
            if rated is not None:
                self.store.log(h.id, now, "inbound", text=text, media=0)
                return [self._say(prof, s) for s in rated]

        if has_media:
            self.engine.extractor = FixedExtractor(list(captured or []))
        captures_before = len(self.store.events(h.id, "capture")) if h else 0
        replies = self.engine.handle_inbound(phone, text, now, media=[b"photo"] if has_media else None)
        h = self.store.get_household_by_phone(phone)

        if prof is None:  # brand-new number
            ref = _REFERRAL.match(text)
            referrer = self.accounts.by_code(ref.group(1)) if ref else None
            h.timezone = timezone_for(phone)
            self.store.save_household(h)
            source = "telegram" if phone.startswith("tg:") else "sms"
            prof = self.accounts.create(h.id, source, now, referred_by=referrer.household_id if referrer else None)
            self.store.log(h.id, now, "voice_assigned", voice=prof.voice)
            if referrer:
                self.store.log(referrer.household_id, now, "referral", new_household=h.id)

        if not was_in and h.opted_in:  # they just said YES
            prof.consented_at, prof.stage = now, "size"
            self.accounts.save(prof)
            return [messages.ASK_SIZE]

        if was_in and replies == [messages.UNKNOWN] and self.agent is not None:
            handled = self._agent(h, text, now)
            if handled:
                replies = handled
            h = self.store.get_household_by_phone(phone)
        elif was_in and replies == [messages.UNKNOWN]:
            command = self._interpret(h, text, now)
            if command and service_command(command):
                replies = self._command(h, command, now)
            elif command:
                replies = self.engine.handle_inbound(phone, command, now)
            h = self.store.get_household_by_phone(phone)
        if was_in and replies == [messages.UNKNOWN]:
            # "unknown" is logged before the agent gets a go; this is the one that means Bud really failed them
            self.store.log(h.id, now, "misunderstood", text=text[:200])

        if was_in and prof.stage == "size":
            # They skipped the household-size question by getting on with it. Don't block them.
            prof.stage = "active"
            self.accounts.save(prof)
            if h.opted_in and (has_media or intent.kind == "add"):
                replies.append(messages.invite_nudge(prof.code, self.config.number))
        # The moment food is added: what to use first and a dinner for tonight. Sent for every
        # photo and for the first typed list, so a new home gets value in seconds, not days.
        captures = self.store.events(h.id, "capture")
        if h.opted_in and len(captures) > captures_before:
            last = captures[-1]["payload"]
            ticked = self.book.tick_bought(h.id, list(last.get("tracked", [])) + list(last.get("untracked", [])), now)
            if ticked:
                replies.append(messages.shop_ticked(ticked))
        if len(captures) > captures_before and any(c.source_kind == "receipt" for c in (captured or [])):
            self.store.log(h.id, now, "receipt_capture")
        if h.opted_in and len(captures) > captures_before and captures[-1]["payload"].get("tracked"):
            if has_media or len(captures) == 1:
                replies.append(self._plan(h, now))
            if len(captures) == 1 and not h.persona and not self.store.events(h.id, "persona_asked"):
                replies.append(messages.ASK_TASTE)   # first food in: now ask how they eat (instead of the age question)
                self.store.log(h.id, now, "persona_asked", when="first_capture")
            elif any(c.source_kind == "fridge" for c in (captured or [])):
                replies.append(messages.FRIDGE_AGE)
        if sender:
            self.store.log(h.id, now, "member_reply", phone=sender)
        return [self._say(prof, r) for r in replies]

    def _plan(self, h: Household, now: datetime, avoid: str | None = None) -> str:
        today = self.engine.local(h, now).date()
        rows, more = plan.use_first(plan.entries(self.engine, h.id, today), today)
        pick = plan.tonight(self.engine, h, today, avoid=avoid)
        self.store.log(h.id, now, "plan", recipe=pick.title if pick else None)
        return messages.rescue_plan(rows, more, pick)

    def _keep(self, h: Household, name: str | None, now: datetime) -> str:
        """Put a recipe Bud showed into their saved recipes: the one they named, else the current one."""
        recipe = self.book.search(h.id, name) if name else self.book.current(h.id)
        if recipe is None:
            return messages.keep_which(name)
        if recipe.confirmed:
            return messages.already_kept(recipe.title)
        self.book.confirm(recipe.id)
        self.store.log(h.id, now, "recipe_kept", recipe=recipe.title)
        return messages.recipe_kept(recipe.title)

    def note_reply(self, phone: str, quoted: str) -> None:
        """They used Telegram's reply on one of Bud's messages: remember which, for the next free-form answer."""
        with self.lock:
            self._replying[phone] = (time.monotonic(), quoted[:600])

    def _quoted(self, phone: str) -> str | None:
        stamp, quoted = self._replying.pop(phone, (0.0, ""))
        return quoted if quoted and time.monotonic() - stamp < 120 else None

    def _recent_turns(self, h: Household, now: datetime, hours: int = 2, keep: int = 3) -> list[tuple[str, str]]:
        """The last few free-form exchanges, so the agent remembers what it just asked."""
        turns = [e for e in self.store.events(h.id, "agent")
                 if e["payload"].get("reply") and now - _at(e) <= timedelta(hours=hours)]
        return [(e["payload"]["text"], e["payload"]["reply"]) for e in turns[-keep:]]

    def _shopping_card(self, h: Household, names: list[str]):
        return self._with_export(h, messages.shopping_list(names))

    def _with_export(self, h: Household, text: str, recipe_id: int | None = None):
        """Any reply about the shopping list carries the Notes / Notion export button."""
        if not self.config.public_url:
            return text
        url = f"{self.config.public_url.rstrip('/')}/list/{self.book.list_token(h.id)}"
        return messages.Card(text, [(messages.EXPORT_BUTTON, url + (f"/{recipe_id}" if recipe_id else ""))])

    def _shopping_for(self, h: Household, dish: str, now: datetime):
        """'My egusi shopping list': just what that recipe still needs, and whether it's on the list yet."""
        recipe = self.book.search(h.id, dish)
        if recipe is None:
            return messages.no_recipe_for_list(dish)
        self.book.set_current(h.id, recipe.id)
        _, missing, _ = self._split(h, recipe)
        names = messages.unique_names([m.name for m in missing])
        listed = {n.lower() for n in messages.unique_names(self.book.shopping(h.id))}
        self.store.log(h.id, now, "shopping_for", recipe=recipe.title, items=len(names))
        if not names:
            return messages.recipe_list_done(recipe.title)
        return self._with_export(h, messages.recipe_shopping(recipe.title, names, [n.lower() in listed for n in names]),
                                 recipe.id)

    def shopping_page(self, token: str) -> tuple[str, list[str]] | None:
        """(title, items) behind an export link, or None if the link isn't one of ours.
        /list/<token> is the whole list; /list/<token>/<recipe id> is what one recipe still needs."""
        token, _, rid = token.partition("/")
        with self.lock:
            hid = self.book.household_for_token(token)
            if hid is None:
                return None
            if rid:
                recipe = next((r for r in self.book.all(hid) if str(r.id) == rid), None)
                h = self.store.get_household(hid)
                if recipe is None or h is None:
                    return None
                _, missing, _ = self._split(h, recipe)
                return f"Shopping for {recipe.title}", messages.unique_names([m.name for m in missing])
            return "Shopping list", messages.unique_names(self.book.shopping(hid))

    # their food persona ------------------------------------------------------
    def _taste(self, h: Household) -> str:
        """Diet notes plus what their voice note told us, for the recipe writer and the agent."""
        p = h.persona or {}
        bits = [h.diet_notes] if h.diet_notes else []
        if p.get("likes"):
            bits.append("loves " + ", ".join(p["likes"][:8]))
        if p.get("cuisines"):
            bits.append("favourite cuisines " + ", ".join(p["cuisines"][:6]))
        if taste.learned(p):
            bits.append("from what they ask for and cook, they lean " + ", ".join(taste.learned(p)[:4]))
        if p.get("loved"):
            bits.append("dishes they loved: " + ", ".join(p["loved"][-5:]))
        meals = p.get("meals") or {}
        if meals:
            bits.append("eats " + ", ".join(f"{m} at {meals[m]}" for m in _MEALS if m in meals))
        if p.get("main_meal"):
            bits.append(f"main meal is {p['main_meal']}")
        return "; ".join(bits)

    def set_persona(self, h: Household, info: dict, now: datetime) -> list[str]:
        """Merge what they told Bud about how they eat; move the daily text to suit their main meal."""
        p = dict(h.persona or {})
        for key in ("likes", "cuisines"):
            have = list(p.get(key) or [])
            for x in info.get(key) or []:
                x = str(x).strip()
                if x and x.lower() not in {y.lower() for y in have}:
                    have.append(x)
            if have:
                p[key] = have[:20]
        meals = dict(p.get("meals") or {})
        for meal, at in (info.get("meals") or {}).items():
            at = str(at or "").strip()
            if meal in _MEALS and re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", at):
                meals[meal] = at.zfill(5)
        if meals:
            p["meals"] = meals
        if info.get("main_meal") in ("breakfast", "brunch", "lunch", "dinner"):
            p["main_meal"] = info["main_meal"]
        avoid = [str(x).strip().lower() for x in info.get("avoid") or [] if str(x).strip()]
        h.dislikes += [a for a in avoid if a not in h.dislikes]
        diet = str(info.get("diet") or "").strip()
        if diet and diet.lower() not in h.diet_notes.lower():
            h.diet_notes = f"{h.diet_notes}; {diet}" if h.diet_notes else diet
        nudge = None
        main = p.get("main_meal") or ("dinner" if "dinner" in meals else None)
        if main in ("lunch", "dinner") and meals.get(main):   # ideas about two hours before they eat, 10am to 6pm
            hh, mm = (int(x) for x in meals[main].split(":"))
            h.nudge_hour, h.nudge_minute = divmod(max(10 * 60, min(18 * 60, hh * 60 + mm - 120)), 60)
            nudge = (h.nudge_hour, h.nudge_minute)
        if p == (h.persona or {}) and not avoid and not diet:
            return [messages.TASTE_EMPTY]
        h.persona = p
        self.store.save_household(h)
        self.store.log(h.id, now, "persona_set", likes=len(p.get("likes") or []), meals=sorted(meals), main=p.get("main_meal"))
        return [messages.persona_saved(p, avoid, diet, nudge)]

    def voice_note(self, phone: str, info: dict | None, now: datetime) -> list[str] | None:
        """A transcribed voice note. About their taste: saved here. A request ("what's for lunch?"): None, so the
        caller handles the transcript like a typed message."""
        if info is None:
            return [messages.VOICE_UNCLEAR]
        if not info.get("about_me"):
            return None
        with self.lock:
            member = self.accounts.member(phone)
            h = self.store.get_household(member.household_id) if member else self.store.get_household_by_phone(phone)
            if h is None or not h.opted_in:
                return None
            self.store.log(h.id, now, "inbound", text="(voice note) " + str(info.get("transcript", ""))[:200], media=1)
            return self.set_persona(h, info, now)

    def _typed_taste(self, h: Household, said: str, now: datetime) -> list[str]:
        """TASTE <how they eat>: the same persona as a voice note, for people who'd rather type."""
        if not said:
            return [self._notebook(h, now)] if h.persona else [messages.ASK_TASTE]
        info = None
        if self.watcher is not None:
            from .video import read_taste
            try:
                info = read_taste(self.watcher, said)
            except Exception as err:
                self.store.log(h.id, now, "voice_error", error=str(err)[:300])
        if info is None:   # no listener available: keep their words as diet notes so recipes still use them
            h.diet_notes = f"{h.diet_notes}; {said}" if h.diet_notes else said
            self.store.save_household(h)
            return ["Noted 📝 I'll cook with that in mind."]
        return self.set_persona(h, info, now)

    # the daily loop (see habit.py) --------------------------------------------
    def _loop_reply(self, h: Household, prof: Profile, text: str, now: datetime) -> list | None:
        """One-tap answers: 🔥/👌/👎 rate what they just cooked; 👍 locks today's pick, 👎 swaps it."""
        said = habit.clean(text)
        if prof.awaiting in ("order", "postal") and said:
            return self._order_reply(h, prof, text, said, now)
        if prof.awaiting == "rate_saved" and said:
            asked = self.store.events(h.id, "rate_asked")
            kind = ("liked" if said in habit.LOVE | habit.FINE | {"keeper", "a keeper", "keep it", "👍"} else
                    "dislike" if said in habit.NOPE | {"👎", "no", "bin it", "it's a no", "its a no"} else None)
            fresh = bool(asked) and now - _at(asked[-1]) <= timedelta(days=3)
            if kind and fresh:
                prof.awaiting = None
                self.accounts.save(prof)
                title = asked[-1]["payload"].get("recipe", "")
                self._signal(h, recipes_mod.cuisine_for(title) or taste.cuisine_of(title),
                             2.0 if kind == "liked" else -2.0, now)
                rated = self._rate_recipe(h, kind, [title], now)
                if rated is not None:
                    return rated
            elif not fresh:
                prof.awaiting = None
                self.accounts.save(prof)
        if prof.awaiting == "craving" and said:
            prof.awaiting = None
            self.accounts.save(prof)
            if not service_command(text) and len(said) <= 40 and said not in habit.SWAP | {"no", "nothing", "never mind"}:
                return self._command(h, f"RECIPE {text.strip()}", now)   # "pad thai" -> the recipe and what to buy
        if prof.awaiting and prof.awaiting.startswith(("offer_", "more")) and said:
            answered = self._cadence_reply(h, prof, said, now)
            if answered is not None:
                return answered
        if not said or len(said) > 40:
            return None
        if said in messages.ABOUT_ME:
            return [self._notebook(h, now)]
        if prof.awaiting == "rate":
            made = self.store.events(h.id, "made")
            recent = bool(made) and now - _at(made[-1]) <= timedelta(hours=14)
            kind = ("love" if said in habit.LOVE else "nope" if said in habit.NOPE else
                    "fine" if said in habit.FINE else None)
            if kind is None and recent:
                return None
            prof.awaiting = None
            self.accounts.save(prof)
            if kind is not None and recent:
                recipe = made[-1]["payload"].get("recipe") or "That one"
                self._rate_dish(h, kind, recipe, now)
                return [messages.rated(kind, recipe)]
        local = self.engine.local(h, now)
        sent = [e for e in self.store.events(h.id, "nudge") if self._day(h, e) == local.date()]
        recipe = (h.last_suggestion or {}).get("recipe")
        if not sent or not recipe or prof.awaiting:
            return None
        since = _at(sent[-1])
        if any(_at(e) > since for kind in _OUTCOMES + ("locked",) for e in self.store.events(h.id, kind)):
            return None
        meal = "lunch" if (h.persona or {}).get("main_meal") == "lunch" else "dinner"
        novelty = float((h.last_suggestion or {}).get("novelty", 0.5))
        if said in habit.LOCK:
            self._variety(h, novelty, 0.12, now)                         # they said yes to this much newness
            self.store.log(h.id, now, "locked", recipe=recipe)
            return [messages.locked_in(recipe, meal)]
        alt = (h.last_suggestion or {}).get("alt")
        if alt and (said in {"2", "2️⃣", "two", "option 2", "the other one", "other one", "the saved one"}
                    or said == messages.short_title(alt).lower()):
            self.store.log(h.id, now, "locked", recipe=alt, option=2)
            return self._command(self.store.get_household(h.id), f"RECIPE {alt}", now)   # its card, with what's missing
        if said in habit.SWAP:
            self.store.log(h.id, now, "pick_swapped", recipe=recipe)
            if novelty > 0.5:
                self._variety(h, 0.1, 0.12, now)                         # too new for them tonight
            else:
                self._variety(h, 0.75, 0.06, now)                        # too samey (or just not in the mood: gently)
            self._signal(h, recipes_mod.cuisine_for(recipe), -0.5, now)
            return self._command(self.store.get_household(h.id), meal, now)   # fresh copy: keep what was just learned
        return None

    # ordering: review, edit, confirm, then Instacart --------------------------------
    def _order_start(self, h: Household, prof: Profile, now: datetime) -> list:
        names = messages.unique_names(self.book.shopping(h.id))
        if not names:
            return [messages.ORDER_EMPTY]
        prof.awaiting = "order"
        self.accounts.save(prof)
        self.store.log(h.id, now, "order_started", items=len(names))
        return [messages.order_review(names)]

    def _order_reply(self, h: Household, prof: Profile, text: str, said: str, now: datetime) -> list | None:
        """While a list is waiting for their OK: edits, the yes, the no, or (first time) their postal code."""
        def done():
            prof.awaiting = None
            self.accounts.save(prof)
        if said in messages.ORDER_CANCEL:
            done()
            self.store.log(h.id, now, "order_cancelled")
            return [messages.ORDER_CANCELLED]
        if prof.awaiting == "postal":
            found = stores.postal(text)
            if not found:
                done()
                return None                          # they moved on to something else
            h = self.store.get_household(h.id)
            h.persona = dict(h.persona or {}, postal=list(found))
            self.store.save_household(h)
            done()
            return self._order_send(h, now)
        edit = re.match(r"^(remove|take off|delete|drop|minus|no more|add|plus|also add|also)\s+(.+)$", said)
        if edit:
            names = [n.strip() for n in re.split(r",|\band\b|&", edit.group(2).replace(" from the list", "")
                                                  .replace(" to the list", "")) if n.strip()]
            if edit.group(1) in ("add", "plus", "also add", "also"):
                changed = self.book.add_shopping(h.id, names, now)
                note = f"Added {', '.join(changed)} ✍️" if changed else "That's already on it 👍"
            else:
                changed = self.book.remove_shopping(h.id, names, now)
                note = f"Removed {', '.join(changed)} ✂️" if changed else f"I couldn't find {', '.join(names)} on the list."
            names_now = messages.unique_names(self.book.shopping(h.id))
            if not names_now:
                done()
                return [messages.ORDER_EMPTY]
            return [messages.order_review(names_now, note)]
        if said in messages.ORDER_CONFIRM:
            if self.instacart is not None and not (h.persona or {}).get("postal"):
                prof.awaiting = "postal"
                self.accounts.save(prof)
                return [messages.ASK_POSTAL]
            done()
            return self._order_send(h, now)
        done()                                       # anything else: drop the review and handle it normally
        return None

    def _order_send(self, h: Household, now: datetime) -> list:
        names = messages.unique_names(self.book.shopping(h.id))
        if not names:
            return [messages.ORDER_EMPTY]
        retailers, url = [], None
        postal = (h.persona or {}).get("postal")
        if self.instacart is not None:
            if postal:
                try:
                    retailers = self.instacart.nearby_retailers(postal[0], postal[1])
                except Exception as err:
                    self.store.log(h.id, now, "instacart_error", error=str(err)[:300], call="retailers")
            try:
                page = (f"{self.config.public_url.rstrip('/')}/list/{self.book.list_token(h.id)}"
                        if self.config.public_url else "")
                url = self.instacart.shopping_link(names, partner_url=page)
            except Exception as err:
                self.store.log(h.id, now, "instacart_error", error=str(err)[:300], call="link")
        plan = stores.plan(names, retailers)
        self.store.log(h.id, now, "order_sent", items=len(names), instacart=bool(url), store=plan["main"],
                       specialty=[s["kind"] for s in plan["specialty"]])
        text = messages.order_sent(plan, bool(url))
        if url:
            return [messages.Card(text, [(messages.ORDER_BUTTON, url)])]
        return [self._with_export(h, text)]

    # how often: MORE / LESS, going quiet, breakfast and lunch ---------------------
    def _settings(self, h: Household) -> dict:
        return dict(self.store.get_household(h.id).persona or {})

    def _save_settings(self, h: Household, p: dict) -> None:
        h = self.store.get_household(h.id)
        h.persona = p
        self.store.save_household(h)

    def _meal_minute(self, h: Household, meal: str) -> int:
        """When the breakfast or lunch text goes: a bit before they usually eat it, else 7:30 AM / 11 AM."""
        at = ((h.persona or {}).get("meals") or {}).get(meal)
        if at:
            hh, mm = (int(x) for x in at.split(":"))
            t = hh * 60 + mm
            return max(390, min(600, t - 30)) if meal == "breakfast" else max(600, min(750, t - 60))
        return 450 if meal == "breakfast" else 660

    def _ignored(self, h: Household) -> int:
        """Daily picks in a row (including the latest) with no word back."""
        heard = self.store.events(h.id, "heard")
        last = _at(heard[-1]) if heard else None
        return sum(1 for e in self.store.events(h.id, "nudge") if last is None or _at(e) > last)

    def _gone_quiet(self, h: Household) -> bool:
        """Three daily picks in a row with no word back."""
        nudges = self.store.events(h.id, "nudge")
        if len(nudges) < 3:
            return False
        heard = self.store.events(h.id, "heard")
        return not heard or _at(heard[-1]) < _at(nudges[-3])

    def _skip_today(self, h: Household, local: datetime) -> str | None:
        nudges = self.store.events(h.id, "nudge")
        if (h.persona or {}).get("snoozed"):
            return "snoozed"                             # they went quiet for good: wait for them to write
        if not nudges or (local.date() - self._day(h, nudges[-1])).days >= 2:
            return None                                  # yesterday was quiet anyway: today goes out
        if (h.persona or {}).get("cadence") == "alternate":
            return "less"
        if self._gone_quiet(h):
            return "quiet"
        return None

    def _more(self, h: Household, prof: Profile, now: datetime) -> str:
        p = self._settings(h)
        self.store.log(h.id, now, "cadence_set", to="more")
        if p.get("cadence") == "alternate":
            p["cadence"] = "daily"
            self._save_settings(h, p)
            return messages.BACK_TO_DAILY
        prof.awaiting = "more"
        self.accounts.save(prof)
        return messages.ASK_MORE

    def _less(self, h: Household, now: datetime) -> str:
        p = self._settings(h)
        self.store.log(h.id, now, "cadence_set", to="less")
        if p.get("extra"):
            p["extra"] = []
            self._save_settings(h, p)
            return messages.EXTRAS_OFF
        if p.get("cadence") == "alternate":
            return messages.ALREADY_LEAST
        p["cadence"] = "alternate"
        self._save_settings(h, p)
        return messages.EVERY_OTHER_DAY

    def _cadence_reply(self, h: Household, prof: Profile, said: str, now: datetime) -> list | None:
        """Answers to "breakfast, lunch or both?" and to a one-time breakfast/lunch offer."""
        state = prof.awaiting
        if state.startswith("offer_"):
            offers = self.store.events(h.id, "meal_offer")
            if not offers or now - _at(offers[-1]) > timedelta(hours=6):
                prof.awaiting = None                       # stale offer: let this message through normally
                self.accounts.save(prof)
                return None
        if state == "more":
            wanted = (["breakfast", "lunch"] if re.search(r"\bboth\b|\ball\b", said) else
                      [m for m in ("breakfast", "lunch") if m in said])
        else:
            meal = state[len("offer_"):]
            yes = said in habit.LOCK | {"yes please", "sure", "please", "yes pls", "ya", "yea"}
            no = said in habit.SWAP | {"no", "no thanks", "no thank you", "nope", "not really"}
            if not (yes or no):
                prof.awaiting = None
                self.accounts.save(prof)
                return None
            if no:
                prof.awaiting = None
                self.accounts.save(prof)
                self.store.log(h.id, now, "meal_offer_declined", meal=meal)
                return [messages.meal_offer_declined(meal)]
            wanted = [meal]
        if not wanted:
            prof.awaiting = None
            self.accounts.save(prof)
            return None
        p = self._settings(h)
        p["extra"] = [m for m in ("breakfast", "lunch") if m in set(p.get("extra") or []) | set(wanted)]
        self._save_settings(h, p)
        prof.awaiting = None
        self.accounts.save(prof)
        self.store.log(h.id, now, "cadence_set", to="extra", meals=wanted)
        return [messages.meals_on(wanted, {m: self._meal_minute(h, m) for m in wanted})]

    def _meal_pick(self, h: Household, meal: str, today):
        p = h.persona or {}
        return recipes_mod.suggest(self.engine.watchlist(h, today), self.store.active_items(h.id), h.dislikes, meal=meal,
                                   any_day=True, **taste.context(self.store, h, today))

    def _meal_texts(self, h: Household, prof: Profile, now: datetime, out: list) -> None:
        """The breakfast/lunch ideas they asked for, and (once each) the offer of them."""
        local = self.engine.local(h, now)
        minute, today = local.hour * 60 + local.minute, local.date()
        if local.hour >= self.config.quiet_hour or self._gone_quiet(h):
            return
        p = h.persona or {}
        for meal in p.get("extra") or []:
            start = self._meal_minute(h, meal)
            if not (start <= minute < start + 120):
                continue
            if any(e["payload"].get("meal") == meal and e["payload"].get("day") == today.isoformat()
                   for e in self.store.events(h.id, "meal_text")):
                continue
            pick = self._meal_pick(h, meal, today)
            self.store.log(h.id, now, "meal_text", meal=meal, day=today.isoformat(), sent=bool(pick))
            if pick:
                text = self._say(prof, messages.meal_idea(pick), seed=h.id + today.toordinal())
                out += [(ph, text) for ph in self._recipients(h)]
        # the one-time offers: breakfast from day 3, lunch from day 5, one a day at most, never on top of anything open
        if not prof.consented_at or prof.awaiting:
            return
        days = (today - prof.consented_at.astimezone(local.tzinfo).date()).days
        offered = {e["payload"].get("meal") for e in self.store.events(h.id, "meal_offer")}
        if any(self._day(h, e) == today for e in self.store.events(h.id, "meal_offer")):
            return
        eats = set((p.get("meals") or {}).keys())
        for meal, from_day in (("breakfast", 3), ("lunch", 5)):
            if meal in offered or meal in (p.get("extra") or []) or days < from_day:
                continue
            if eats and meal not in eats:              # their voice note says they don't eat it: don't ask
                continue
            start = self._meal_minute(h, meal)
            if not (start <= minute < start + 60):
                continue
            pick = self._meal_pick(h, meal, today)
            if not pick:
                continue                               # only ask when there's something good to show
            prof.awaiting = f"offer_{meal}"
            self.accounts.save(prof)
            self.store.log(h.id, now, "meal_offer", meal=meal, day=today.isoformat())
            text = self._say(prof, messages.meal_offer(meal, pick), seed=h.id + today.toordinal())
            out += [(ph, text) for ph in self._recipients(h)]
            return

    def _missing(self, h: Household, now: datetime) -> str:
        """"I don't have most of that": Bud's list is off. The dish they were looking at, and three ways on."""
        current = self.book.current(h.id)
        pick = (h.last_suggestion or {}).get("recipe")
        recent_card = [e for e in self.store.events(h.id, "recipe_shown") if now - _at(e) <= timedelta(hours=3)]
        if recent_card and current is not None:
            recipe, can_shop = current.title, True
        else:
            recipe, can_shop = pick, bool(current and pick and current.title.lower() == pick.lower())
        self.store.log(h.id, now, "fridge_mismatch", recipe=recipe)
        return messages.missing_most(recipe, can_shop)

    def _outside(self, h: Household, prof: Profile, now: datetime) -> str:
        """They want to cook what they feel like, not what's in the fridge: ask what, with ideas in their taste."""
        examples = {"West African": "jollof rice", "Thai": "pad thai", "Indian": "butter chicken", "Chinese": "fried rice",
                    "Mexican": "birria tacos", "Italian": "carbonara", "Caribbean": "jerk chicken",
                    "Middle Eastern": "shawarma", "Korean": "bibimbap", "Japanese": "ramen"}
        weights = taste.weights(h.persona)
        liked = [examples[c] for c, w in sorted(weights.items(), key=lambda kv: -kv[1]) if w > 0 and c in examples]
        picks = (liked + [x for x in ("jollof rice", "pad thai", "shawarma") if x not in liked])[:3]
        prof.awaiting = "craving"
        self.accounts.save(prof)
        self.store.log(h.id, now, "outside_fridge")
        return messages.ask_craving(picks)

    def _variety(self, h: Household, target: float, rate: float, now: datetime) -> None:
        h = self.store.get_household(h.id)
        h.persona = taste.nudge_variety(h.persona, target, rate)
        self.store.save_household(h)
        self.store.log(h.id, now, "variety", value=h.persona["variety"], toward=target)

    def _locked_today(self, h: Household, now: datetime) -> str | None:
        today = self.engine.local(h, now).date()
        locks = [e for e in self.store.events(h.id, "locked") if self._day(h, e) == today]
        return locks[-1]["payload"].get("recipe") if locks else None

    def _rate_dish(self, h: Household, kind: str, recipe: str, now: datetime) -> None:
        """The investment: what they thought changes what Bud picks next."""
        h = self.store.get_household(h.id)
        p = dict(h.persona or {})
        loved = [x for x in p.get("loved") or [] if x.lower() != recipe.lower()]
        if kind == "love":
            loved.append(recipe)
            h.dislikes = [d for d in h.dislikes if d != recipe.lower()]
            saved = next((r for r in self.book.all(h.id) if r.title.lower() == recipe.lower()), None)
            if saved is not None:
                self.book.confirm(saved.id)          # an AI recipe they loved is worth keeping
        elif kind == "nope" and recipe.lower() not in h.dislikes:
            h.dislikes.append(recipe.lower())
        if loved:
            p["loved"] = loved[-20:]
        else:
            p.pop("loved", None)
        h.persona = p
        self.store.save_household(h)
        self.store.log(h.id, now, "dish_rated", recipe=recipe, rating=kind)
        self._signal(h, recipes_mod.cuisine_for(recipe), {"love": 2.0, "fine": 0.5, "nope": -2.0}[kind], now)
        rewards = self.store.events(h.id, "reward")
        novelty = float(rewards[-1]["payload"].get("novelty", 0.5)) if rewards else 0.5
        if novelty > 0.5 and kind in ("love", "nope"):
            self._variety(h, 0.9 if kind == "love" else 0.1, 0.15, now)   # loved (or hated) something new

    def _month_saved(self, h: Household, today) -> float:
        return habit.month_saved(self.store.items_closed_between(h.id, today.replace(day=1), today))

    def _cooked_this_week(self, h: Household, today) -> int:
        return sum(1 for e in self.store.events(h.id, "made") if 0 <= (today - self._day(h, e)).days < 7)

    def _made_reward(self, h: Household, rescued: list, recipe: str | None, today, now: datetime) -> str:
        """MADE: a reward that's a little different every time, then the one-tap rating."""
        self._signal(h, recipes_mod.cuisine_for(recipe or ""), 1.0, now)
        h = self.store.get_household(h.id)
        worth = sum(habit.value(i.name, i.price, i.kind, i.portions) for i in rescued)
        month = self._month_saved(h, today)
        made = self.store.events(h.id, "made")
        milestone = None
        if len(made) in habit.MEAL_MARKS:
            milestone = messages.meal_milestone(len(made))
        else:
            mark = habit.crossed(month - worth, month, habit.DOLLAR_MARKS)
            if mark:
                milestone = messages.dollar_milestone(mark)
        teaser = None
        meal = "lunch" if (h.persona or {}).get("main_meal") == "lunch" else "dinner"
        try:
            tomorrow = today + timedelta(days=1)
            nxt = recipes_mod.suggest(self.engine.watchlist(h, tomorrow), self.store.active_items(h.id), h.dislikes,
                                      avoid=recipe, meal=meal, any_day=True, **taste.context(self.store, h, tomorrow))
            if nxt and nxt.title != recipe and nxt.minutes is not None:
                teaser = messages.tomorrow_teaser(nxt.title, [i.name for i in nxt.rescues])
        except Exception as err:   # a teaser is a nicety; never let it break MADE
            self.store.log(h.id, now, "teaser_error", error=str(err)[:200])
        prof = self._profile(h, now)
        prof.awaiting = "rate"
        self.accounts.save(prof)
        novelty = float((h.last_suggestion or {}).get("novelty", 0.5)) if (h.last_suggestion or {}).get("recipe") == recipe else 0.5
        self._variety(h, novelty, 0.1, now)
        self.store.log(h.id, now, "reward", worth=round(worth, 2), month=month, milestone=milestone, novelty=novelty)
        return messages.made_reward(habit.cheer(habit.seed_for(h.id, today, len(made))), recipe,
                                    [i.name for i in rescued], worth, month, self._cooked_this_week(h, today),
                                    milestone, teaser)

    def _tip(self, h: Household, prof: Profile, now: datetime) -> str | None:
        """Teach one feature, once, on the daily pick; skip what they already use; stop after four weeks."""
        if not prof.consented_at:
            return None
        local = self.engine.local(h, now)
        days = (local.date() - prof.consented_at.astimezone(local.tzinfo).date()).days
        if days < 1 or days > messages.TIP_DAYS:
            return None
        shown = self.store.events(h.id, "tip")
        if shown and (local.date() - self._day(h, shown[-1])).days < messages.TIP_GAP_DAYS:
            return None
        done = {e["payload"].get("key") for e in shown}
        for key, used, text in messages.TIPS:
            if key in done or any(self.store.events(h.id, kind) for kind in used):
                continue
            self.store.log(h.id, now, "tip", key=key)
            return text.format(code=prof.code)
        return None

    def _notebook(self, h: Household, now: datetime) -> str:
        today = self.engine.local(h, now).date()
        self.store.log(h.id, now, "notebook")
        h = self.store.get_household(h.id)
        return messages.notebook(h.persona or {}, h.dislikes, h.diet_notes, len(self.book.kept(h.id)),
                                 taste.learned(h.persona),
                                 self._month_saved(h, today), self._cooked_this_week(h, today))

    def _taste_ps(self, h: Household, now: datetime) -> str | None:
        """Homes that joined before Bud asked about taste get asked once, on a daily text."""
        if h.persona or self.store.events(h.id, "persona_asked") or not self.store.events(h.id, "capture"):
            return None
        self.store.log(h.id, now, "persona_asked", when="nudge")
        return messages.TASTE_PS

    def is_current_pick(self, phone: str, text: str) -> bool:
        """Is the idea in this Bud message the one on the table right now (not an older one they scrolled to)?"""
        with self.lock:
            member = self.accounts.member(phone)
            h = self.store.get_household(member.household_id) if member else self.store.get_household_by_phone(phone)
            title = ((h.last_suggestion or {}).get("recipe") if h else None) or ""
            return bool(title) and title.lower() in text.lower()

    def focus_recipe(self, phone: str, title: str) -> bool:
        """They replied to one of Bud's recipe messages: make that recipe the one "this" and "it" mean."""
        with self.lock:
            member = self.accounts.member(phone)
            h = self.store.get_household(member.household_id) if member else self.store.get_household_by_phone(phone)
            if h is None:
                return False
            exact = [r for r in self.book.all(h.id) if r.title.strip().lower() == title.strip().lower()]
            recipe = exact[0] if exact else self.book.search(h.id, title)   # the card's own title beats a near match
            if recipe is None:
                return False
            self.book.set_current(h.id, recipe.id)
            return True

    def _command(self, h: Household, text: str, now: datetime) -> list[str]:
        """DINNER, OLD, HAVE and ALL: the commands this layer adds to the engine's."""
        kind, names = service_command(text)
        today = self.engine.local(h, now).date()
        if kind == "have":
            return [self._have(h, names[0], today, now)]
        if kind == "keep":
            return [self._keep(h, names[0] if names else None, now)]
        if kind == "taste":
            return self._typed_taste(h, names[0] if names else "", now)
        if kind in {"link", "paste", "recipe", "steps", "recipes", "audio", "shop", "shopadd", "shopping", "shopfor", "order", "more", "less", "missing", "outside", "clearshop", "roast"}:
            return self._kitchen(h, kind, names, now)
        if kind == "all":
            entries = plan.entries(self.engine, h.id, today)
            tracked_ids = {e.item.id for e in entries}
            others = [i.name for i in self.store.active_items(h.id) if i.id not in tracked_ids]
            self.store.log(h.id, now, "asked_all")
            return [messages.everything([(e.item.name, plan.by_when(e.days_left, today)) for e in entries[:20]], others[:20])]
        if kind == "meal":   # breakfast, brunch, lunch or a snack: from their fridge first, else written for that meal
            meal = names[0]
            last = (h.last_suggestion or {}).get("recipe")
            pick = plan.tonight(self.engine, h, today, avoid=last, meal=meal)
            self.store.log(h.id, now, "meal_asked", meal=meal, recipe=pick.title if pick else None)
            if pick and pick.meal == meal:
                return [messages.dinner_idea(pick)]
            if self.chef is not None:
                return self._new_recipe(h, f"a {meal} idea", now, f"Nothing in your fridge fits {meal} yet, so here's one I wrote.")
            return [messages.NO_IDEA]
        if kind == "dinner":
            last = (h.last_suggestion or {}).get("recipe")
            before = {t.lower() for t in plan.shown_today(h, today)} | ({last.lower()} if last else set())
            pick = plan.tonight(self.engine, h, today, avoid=last)
            if pick is None or pick.weak:
                ai = self._ai_idea(h, "dinner", now, sorted(before))
                if ai:
                    pick = ai
                    h = self.store.get_household(h.id)
                    h.last_suggestion = dict(ai.as_state(), check_ids=(h.last_suggestion or {}).get("check_ids", []),
                                             shown=plan.shown_today(h, today) + [ai.title], shown_on=today.isoformat())
                    self.store.save_household(h)
            self.store.log(h.id, now, "dinner_asked", recipe=pick.title if pick else None)
            return [messages.dinner_idea(pick, same_again=pick.title.lower() in before) if pick else messages.NO_IDEA]
        active = self.store.active_items(h.id)
        matched = [m for m in (self.engine._match(h, n, active) for n in names) if m]
        moved = plan.mark_old(self.engine, h, matched, today)
        if not moved:
            return [f"I couldn't find {', '.join(names)} on your list, or it's already near the top. Reply LIST to see it."]
        self.store.log(h.id, now, "marked_old", items=[i.name for i in moved])
        return [messages.marked_old([i.name for i in moved]), self._plan(self.store.get_household(h.id), now)]

    def _have(self, h: Household, name: str, today, now: datetime) -> str:
        """"Do I have oranges left?" Answered from the list, never guessed."""
        found = plan.find(self.engine, self.store.active_items(h.id), name)
        self.store.log(h.id, now, "asked_have", item=name, found=len(found))
        if found:
            when = {e.item.id: plan.by_when(e.days_left, today) for e in plan.entries(self.engine, h.id, today)}
            return messages.have_yes([(i.name, f"added {i.added_on:%a %b} {i.added_on.day}", when.get(i.id)) for i in found[:4]])
        recent = self.store.items_closed_between(h.id, today - timedelta(days=14), today)
        gone = plan.find(self.engine, recent, name)
        last = max(gone, key=lambda i: i.closed_on) if gone else None
        return messages.have_no(name, (last.name, last.status, f"{last.closed_on:%b} {last.closed_on.day}") if last else None)

    def _answer(self, h: Household, prof: Profile, text: str, now: datetime) -> list[str] | None:
        found = places.parse(text, bare=prof.awaiting == "place")
        if found:
            return self._set_place(h, prof, *found, now)
        if service_command(text):
            if prof.stage == "size":
                prof.stage = "active"
                self.accounts.save(prof)
            return self._command(h, text, now)
        if prof.stage == "size":
            size = _size(text)
            if size:
                prof.size, prof.stage = size, "active"
                self.store.log(h.id, now, "onboarded", size=size)
                if self._needs_place(h):   # Telegram can't tell us their time zone, so ask once
                    prof.awaiting = "place"
                    self.accounts.save(prof)
                    self.store.log(h.id, now, "place_asked", when="onboarding")
                    return [messages.size_ack(size, prof.code, self.config.number), messages.ASK_PLACE]
                self.accounts.save(prof)
                return [messages.size_ack(size, prof.code, self.config.number), messages.PHOTO_ASK]
        if prof.awaiting == "dollars":
            m = _DOLLARS.match(text)
            if m:
                prof.awaiting = None
                self.accounts.save(prof)
                self.store.log(h.id, now, "self_reported_savings", amount=float(m.group(1)))
                return [messages.dollars_logged(float(m.group(1)))]
        return None

    def _interpret(self, h: Household, text: str, now: datetime) -> str | None:
        if self.interpreter is None:
            return None
        today = self.engine.local(h, now).date().isoformat()
        used = sum(1 for e in self.store.events(h.id, "fallback") if e["payload"].get("day") == today)
        if used >= self.config.fallback_daily_cap:
            return None
        names = [i.name for i in self.store.active_items(h.id)]
        try:
            command = self.interpreter.interpret(text, names, (h.last_suggestion or {}).get("recipe"))
        except Exception as err:  # a model outage must never break the conversation
            self.store.log(h.id, now, "fallback_error", error=str(err)[:300])
            return None
        if command and parse(command).kind == "start":
            command = None  # only a literal YES can opt someone in
        self.store.log(h.id, now, "fallback", day=today, text=text, command=command)
        return command

    # outbound ---------------------------------------------------------
    # where they are --------------------------------------------------------
    def _needs_place(self, h: Household) -> bool:
        return h.phone.startswith("tg:") and not self.store.events(h.id, "timezone_set")

    def _set_place(self, h: Household, prof: Profile, zone: str, said: str, now: datetime) -> list[str]:
        h.timezone = zone
        self.store.save_household(h)
        if prof.awaiting == "place":
            prof.awaiting = None
            self.accounts.save(prof)
        self.store.log(h.id, now, "timezone_set", zone=zone, said=said[:60])
        replies = [messages.place_set(said, zone, h.nudge_hour, h.nudge_minute)]
        if not self.store.events(h.id, "capture") and not self.store.active_items(h.id):
            replies.append(messages.PHOTO_ASK)   # the step onboarding held back while it asked the city
        return replies

    def _place_ps(self, h: Household, prof: Profile, now: datetime) -> str | None:
        """Telegram homes that joined before Bud asked for a city get asked once, on their 4:30 text."""
        if not self._needs_place(h) or self.store.events(h.id, "place_asked"):
            return None
        prof.awaiting = "place"
        self.accounts.save(prof)
        self.store.log(h.id, now, "place_asked", when="nudge")
        return messages.PLACE_PS

    # Bud reaching out -------------------------------------------------------
    # At most two texts a day (the 4:30 text, then the 8 PM check-in), never at or after quiet_hour local.
    def _day(self, h: Household, event: dict):
        return _at(event).astimezone(ZoneInfo(h.timezone)).date()

    def _open(self, h: Household, local: datetime) -> bool:
        return (h.nudge_hour, h.nudge_minute) <= (local.hour, local.minute) and local.hour < self.config.quiet_hour

    def _list_age(self, h: Household, local: datetime) -> int | None:
        captures = self.store.events(h.id, "capture")
        return (local.date() - self._day(h, captures[-1])).days if captures else None

    def _refresh_due(self, h: Household, local: datetime, min_days: int) -> int | None:
        """Days since the last fridge photo, if that's at least min_days and Bud hasn't asked for one this week."""
        days = self._list_age(h, local)
        if days is None or days < min_days:
            return None
        asks = self.store.events(h.id, "refresh_ask")
        if asks and (local.date() - self._day(h, asks[-1])).days < 7:
            return None
        return days

    def _stale_note(self, h: Household, now: datetime) -> str | None:
        """Rides on the 4:30 text: the picks are only as good as the list."""
        local = self.engine.local(h, now)
        days = self._refresh_due(h, local, 7)
        if days is None:
            return None
        self.store.log(h.id, now, "refresh_ask", day=local.date().isoformat(), days=days, standalone=False)
        return messages.stale_list(days)

    def _refresh_ask(self, h: Household, prof: Profile, now: datetime, out: list) -> None:
        """No 4:30 text today (nothing at risk) because the list is old or empty: ask for a fresh photo instead."""
        local = self.engine.local(h, now)
        if h.last_nudge_on != local.date() or not self._open(h, local):
            return
        if any(self._day(h, e) == local.date() for e in self.store.events(h.id, "nudge")):
            return
        empty = not self.store.active_items(h.id)
        days = self._refresh_due(h, local, 3 if empty else 7)
        if days is None:
            return
        self.store.log(h.id, now, "refresh_ask", day=local.date().isoformat(), days=days, standalone=True, empty=empty)
        text = self._say(prof, messages.stale_list(days, standalone=True, empty=empty), seed=h.id)
        out += [(p, text) for p in self._recipients(h)]

    def _activation_chase(self, h: Household, prof: Profile, now: datetime, out: list) -> None:
        """Said yes but never told Bud what's in the fridge: ask on day 1 and day 3 at their 4:30, then stop."""
        if not prof.consented_at or self.store.events(h.id, "capture") or self.store.active_items(h.id):
            return
        local = self.engine.local(h, now)
        if not self._open(h, local):
            return
        days = (local.date() - prof.consented_at.astimezone(local.tzinfo).date()).days
        chases = self.store.events(h.id, "activation_chase")
        if len(chases) >= 2 or days < (3 if chases else 1):
            return
        if chases and self._day(h, chases[-1]) == local.date():
            return
        n = len(chases) + 1
        self.store.log(h.id, now, "activation_chase", n=n, day=local.date().isoformat())
        text = self._say(prof, messages.activation_chase(n), seed=h.id + n)
        out += [(p, text) for p in self._recipients(h)]

    def _checkin_time(self, h: Household) -> tuple[int, str]:
        """(minutes after midnight, meal) for the "how did it go?" text: ~90 min after their main meal if Bud knows
        it, else 8 PM. Never later than half an hour before quiet hours."""
        p = h.persona or {}
        meals = p.get("meals") or {}
        main = p.get("main_meal") or ("dinner" if "dinner" in meals else None)
        start = self.config.checkin_hour * 60
        if main in ("lunch", "dinner") and meals.get(main):
            hh, mm = (int(x) for x in meals[main].split(":"))
            start = hh * 60 + mm + 90
        return min(start, self.config.quiet_hour * 60 - 30), ("lunch" if main == "lunch" else "dinner")

    def _still_on(self, h: Household, prof: Profile, now: datetime, out: list) -> None:
        """About an hour after the pick, if they haven't said a word: are you making it? Once, before they eat."""
        local = self.engine.local(h, now)
        today = local.date()
        sent = [e for e in self.store.events(h.id, "nudge") if self._day(h, e) == today]
        state = h.last_suggestion or {}
        if not sent or not state.get("recipe") or local.hour >= self.config.quiet_hour or self._gone_quiet(h):
            return
        if any(self._day(h, e) == today for e in self.store.events(h.id, "followup")):
            return
        since = _at(sent[-1])
        if now - since < timedelta(minutes=60) or now - since > timedelta(hours=3):
            return
        if any(_at(e) > since for e in self.store.events(h.id, "heard")):
            return                                   # they replied (or said anything): no chasing
        meal_at, meal = self._checkin_time(h)
        meal_at -= 90                                # _checkin_time is meal + 90 min; the meal itself is this
        if local.hour * 60 + local.minute > meal_at - 20:
            return                                   # too close to (or past) when they eat
        self.store.log(h.id, now, "followup", recipe=state["recipe"])
        text = self._say(prof, messages.still_on(state["recipe"], meal, state.get("alt")), seed=h.id + today.toordinal())
        out += [(p, text) for p in self._recipients(h)]

    def _evening_checkin(self, h: Household, prof: Profile, now: datetime, out: list) -> None:
        """8 PM on a day the 4:30 text went out and nobody said what happened: ask once, when they know."""
        local = self.engine.local(h, now)
        start, meal = self._checkin_time(h)
        if not (start <= local.hour * 60 + local.minute and local.hour < self.config.quiet_hour):
            return
        today = local.date()
        sent = [e for e in self.store.events(h.id, "nudge") if self._day(h, e) == today]
        if not sent or any(self._day(h, e) == today for e in self.store.events(h.id, "checkin")):
            return
        since = _at(sent[-1])
        if now - since < timedelta(hours=2):   # a late 4:30 text (server was down) mustn't get a check-in on its heels
            return
        if any(_at(e) > since for kind in _OUTCOMES for e in self.store.events(h.id, kind)):
            return
        nudged = [e for e in self.store.events(h.id, "followup") if self._day(h, e) == today]
        if nudged and not any(_at(e) > _at(nudged[-1]) for e in self.store.events(h.id, "heard")):
            return                                   # they ignored the follow-up too: a busy evening, leave it
        state = h.last_suggestion or {}
        asked = set(state.get("item_ids", [])) | set(state.get("check_ids", []))
        active = self.store.active_items(h.id)
        names = [i.name for i in active if i.id in asked] or [e.item.name for e in self.engine.watchlist(h, today)][:3]
        recipe = state.get("recipe")
        if not names and not recipe:
            return
        self.store.log(h.id, now, "checkin", day=today.isoformat(), recipe=recipe, items=names)
        text = self._say(prof, messages.evening_checkin(recipe, names, meal), seed=h.id + today.toordinal())
        out += [(p, text) for p in self._recipients(h)]

    def tick(self, now: datetime) -> list[tuple[str, str]]:
        """Call every minute. Returns (phone, text) pairs to send now."""
        out: list[tuple[str, str]] = []
        with self.lock:
            for h in self.store.all_households():
                if h.opted_in:
                    self._paywall(h, self._profile(h, now), now, out)
            for phone, text in self.engine.due_nudges(now, self.config.quiet_hour):
                h = self.store.get_household_by_phone(phone)
                prof = self._profile(h, now)
                text = self._say(prof, text, seed=h.id + self.engine.local(h, now).toordinal())
                hint = self._saved_recipe_hint(h, self.engine.local(h, now).date())
                if hint and messages.PICK_FOOTER in text:
                    text = messages.with_saved_option(text, *hint)
                    hh = self.store.get_household(h.id)
                    hh.last_suggestion = dict(hh.last_suggestion or {}, alt=hint[0])
                    self.store.save_household(hh)
                    self.store.log(h.id, now, "decision_alt", policy=self.engine.POLICY, recipe=hint[0],
                                   cuisine=recipes_mod.cuisine_for(hint[0]) or taste.cuisine_of(hint[0]))
                elif hint:
                    text += "\n\n" + messages.saved_recipe_hint(*hint)
                ignored = self._ignored(h)
                if ignored >= messages.IGNORED_FINAL_AT:
                    hh = self.store.get_household(h.id)
                    hh.persona = dict(hh.persona or {}, snoozed=True)
                    self.store.save_household(hh)
                    self.store.log(h.id, now, "snoozed", ignored=ignored)
                    text = messages.STEPPING_BACK
                    out += [(p, text) for p in self._recipients(h)]
                    continue
                extras = [x for x in (self._stale_note(h, now), self._place_ps(h, prof, now)) if x]
                if ignored in messages.IGNORED_PS:
                    self.store.log(h.id, now, "ignored_ps", level=ignored)
                    extras = [messages.IGNORED_PS[ignored]]       # this one beats any other P.S.
                if not extras:   # at most one P.S. a day
                    extras = [x for x in (self._taste_ps(h, now),) if x]
                if not extras:
                    extras = [x for x in (self._tip(h, prof, now),) if x]
                for extra in extras:
                    text += "\n\n" + extra
                out += [(p, text) for p in self._recipients(h)]
            for h in self.store.all_households():
                if h.opted_in and not (h.persona or {}).get("snoozed"):   # stepped back: nothing until they write
                    prof = self._profile(h, now)
                    self._activation_chase(h, prof, now, out)
                    self._refresh_ask(h, prof, now, out)
                    self._still_on(h, prof, now, out)
                    self._evening_checkin(h, prof, now, out)
                    self._sunday_receipt(h, prof, now, out)
                    self._meal_texts(h, prof, now, out)
        return out

    def _sunday_receipt(self, h: Household, prof: Profile, now: datetime, out: list) -> None:
        local = self.engine.local(h, now)
        if local.weekday() != 6 or local.hour < self.config.receipt_hour or prof.last_receipt_on == local.date():
            return
        prof.last_receipt_on = local.date()
        built = receipt.build(self.store, h, local.date(), self._voice(prof), self.config.number, prof.code)
        if built:
            text, asks = built
            cooked = self._cooked_this_week(h, local.date())
            if cooked:
                fave = ((h.persona or {}).get("loved") or [None])[-1]
                text += "\n\n" + f"🍳 {cooked} home-cooked meal{'' if cooked == 1 else 's'} with Bud this week" + (
                    f" · ⭐ {fave}" if fave else "")
            text += "\n\nPlanning ahead? 🎙️ Send a voice note of what you're craving this week."
            if asks:
                prof.awaiting = "dollars"
            self.store.log(h.id, now, "receipt", text=text)
            out += [(p, text) for p in self._recipients(h)]
        self.accounts.save(prof)

    def _paywall(self, h: Household, prof: Profile, now: datetime, out: list) -> None:
        """Day 14: one text with the price and link. Unpaid homes pause after the grace period."""
        cfg = self.config
        if not cfg.payment_link or prof.paid_at or not prof.consented_at:
            return
        local = self.engine.local(h, now)
        if prof.paywall_sent_at is None:
            if now >= prof.consented_at + timedelta(days=cfg.trial_days) and local.hour >= 12:
                prof.paywall_sent_at = now
                self.accounts.save(prof)
                text = messages.paywall(cfg.price or "the household plan", cfg.payment_link, cfg.trial_days, cfg.grace_days)
                self.store.log(h.id, now, "paywall", text=text)
                out.append((h.phone, text))
        elif now >= prof.paywall_sent_at + timedelta(days=cfg.grace_days) and h.last_nudge_on != local.date():
            h.last_nudge_on = local.date()  # marks today as handled, so the engine sends nothing
            self.store.save_household(h)
            self.store.log(h.id, now, "nudge_skipped", reason="unpaid")

    def mark_paid(self, raw_phone: str, now: datetime) -> str | None:
        """Record a payment (by hand for now). Returns the phone to thank, or None if unknown."""
        phone = normalize_phone(raw_phone)
        with self.lock:
            h = self.store.get_household_by_phone(phone) if phone else None
            if h is None:
                return None
            prof = self._profile(h, now)
            prof.paid_at = now
            self.accounts.save(prof)
            self.store.log(h.id, now, "paid")
            return h.phone

    # recipe book, shopping list, audio, roast --------------------------
    def _as_of(self, h: Household, now: datetime) -> str | None:
        """When the fridge list was last updated, so answers say how fresh they are."""
        captures = self.store.events(h.id, "capture")
        if not captures:
            return None
        zone = ZoneInfo(h.timezone)
        at = datetime.fromisoformat(captures[-1]["at"]).astimezone(zone)
        what = "photo" if captures[-1]["payload"].get("source") == "photo" else "update"
        text = f"your last {what} ({at:%a %b} {at.day}, {at:%I:%M %p}".replace(", 0", ", ") + "):"
        days = (now.astimezone(zone).date() - at.date()).days
        if days >= 3:
            text += f"\n(That was {days} days ago. Send a new photo if things have changed.)"
        return text

    def _match(self, need: list, active: list) -> list:
        """Which fridge item covers each ingredient. The model is stricter and smarter; code is the fallback."""
        if not need:
            return []
        if self.chef is not None and active:
            try:
                names = self.chef.match([i.name for i in need], [a.name for a in active])
                if names is not None:
                    by_name = {a.name.lower(): a for a in active}
                    return [by_name.get(n.lower()) if n else None for n in names]
            except Exception as err:
                self.store.log(None, datetime.now(ZoneInfo("UTC")), "match_error", error=str(err)[:200])
        return match_local(need, active)

    @staticmethod
    def _source_line(r: Recipe) -> str:
        if r.source == "ai":
            return messages.AI_LABEL
        note = r.note
        if note in messages.LEGACY_NOTES:
            note = getattr(messages, messages.LEGACY_NOTES[note])
        if r.source == "pasted":
            return "From what you sent me." + (f" {note}" if note else "")
        if r.source == "photo":
            return "From your screenshot." + (f" {note}" if note else "")
        if r.source == "video":
            return note or "From the video you sent me."
        where = f"🔗 {messages.clean_url(r.url)}" if r.url else f"From {r.site or 'your link'}."
        return where + (f"\n{note}" if note else "")

    def _split(self, h: Household, r: Recipe) -> tuple[list, list, list]:
        """(fridge items you have, missing ingredients, staples assumed) for a recipe, right now."""
        need = [i for i in r.ingredients if not i.staple]
        matched = self._match(need, self.store.active_items(h.id))
        have = [m for m in matched if m]
        missing = [ing for ing, m in zip(need, matched) if m is None]
        return have, missing, [i.name for i in r.ingredients if i.staple]

    def _card(self, h: Household, r: Recipe, now: datetime, lead: str = "") -> str:
        have, missing, staples = self._split(h, r)
        self.book.set_current(h.id, r.id)
        h = self.store.get_household(h.id)
        # MADE after this closes the perishables the recipe used.
        h.last_suggestion = {"recipe": r.title, "item_ids": sorted({i.id for i in have if i.key}), "breakfast_ids": [],
                             "check_ids": (h.last_suggestion or {}).get("check_ids", [])}
        self.store.save_household(h)
        self.store.log(h.id, now, "recipe_shown", recipe=r.title, source=r.source, have=len(have), missing=len(missing))
        seen = []
        for item in have:
            if item.name not in seen:
                seen.append(item.name)
        card = messages.recipe_card(r.title, self._source_line(r), r.servings, r.minutes, seen,
                                    [m.name for m in missing],
                                    staples, self._as_of(h, now), lead)
        return messages.Card(card, photo=self._picture(r))

    @staticmethod
    def _picture(r: Recipe) -> dict | None:
        """The video's own thumbnail for YouTube recipes; a labelled AI photo for AI-written ones; else nothing."""
        vid = youtube_id(r.url) if r.url else None
        if vid:
            return {"url": youtube_thumbnail(vid), "caption": f"▶️ {r.title}"}
        if r.source == "ai":
            return {"prompt": dish_prompt(r.title), "key": dish_key(r.title), "caption": r.title}   # the card under it already says the recipe is AI-written
        return None

    def _rate_recipe(self, h: Household, kind: str, names: list[str], now: datetime) -> list | None:
        """LIKED/DISLIKE naming something that matches a saved recipe title: handle the recipe book here
        (confirm it, or bin it if it was never confirmed) instead of the plain ingredient dislikes list."""
        hit = self.book.search(h.id, " ".join(names))
        if hit is None:
            return None
        if kind == "liked":
            self.book.confirm(hit.id)
            self.store.log(h.id, now, "recipe_liked", recipe=hit.title)
            return [messages.liked([hit.title], [])]
        if not hit.confirmed:
            self.book.delete(hit.id)
            self.store.log(h.id, now, "recipe_declined", recipe=hit.title)
            return [messages.recipe_declined(hit.title)]
        return None  # a recipe they've already kept and now dislike: fall through to the plain dislikes list

    def _ai_idea(self, h: Household, meal: str, now: datetime, shown: list[str] | None = None):
        """The dish list fits this fridge poorly (one ingredient, or nothing): have the AI write a proper meal, using
        some of what they have if it helps and happy to need things they don't. Capped per household per day."""
        if self.chef is None:
            return None
        today = self.engine.local(h, now).date()
        used = sum(1 for e in self.store.events(h.id, "ai_idea") if self.engine.local(h, _at(e)).date() == today)
        if used >= self.config.ai_idea_daily_cap:
            return None
        items = self.store.active_items(h.id)
        ask = (f"a {meal} idea. Build it around one or two things from their fridge only if that makes a good dish; "
               "it is fine, even better, to need ingredients they do not have yet. "
               + (f"Not any of these, they have seen them: {', '.join(shown)}." if shown else ""))
        try:
            data = self.chef.generate(ask, [i.name for i in items], h.dislikes, self._taste(h))
        except Exception as err:
            self.store.log(h.id, now, "chef_error", error=str(err)[:300])
            return None
        recipe = recipe_from_dict(data, h.id, "ai") if data else None
        if recipe is None:
            return None
        saved = self.book.save(recipe, now)
        self.book.set_current(h.id, saved.id)
        uses = [i for i in items if any(same_food(ing.name, i.name) for ing in saved.ingredients if not ing.staple)]
        self.store.log(h.id, now, "ai_idea", recipe=saved.title, meal=meal, uses=len(uses))
        return recipes_mod.Suggestion(saved.title, saved.minutes, uses[:3], meal=meal, novelty=0.5, ai=True)

    def _new_recipe(self, h: Household, request: str, now: datetime, lead: str) -> list:
        """Have the AI write a recipe, save it labelled as AI-written, and show it."""
        if self.chef is None:
            return [messages.NEEDS_AI]
        try:
            data = self.chef.generate(request, [i.name for i in self.store.active_items(h.id)], h.dislikes, self._taste(h))
        except Exception as err:
            self.store.log(h.id, now, "chef_error", error=str(err)[:300])
            return [messages.AI_DOWN]
        recipe = recipe_from_dict(data, h.id, "ai") if data else None
        if recipe is None:
            return [messages.not_a_dish(request)]
        saved = self.book.save(recipe, now)
        self.store.log(h.id, now, "recipe_saved", recipe=saved.title, source="ai")
        prof = self._profile(h, now)
        if not prof.awaiting:
            prof.awaiting = "rate_saved"
            self.accounts.save(prof)
        self.store.log(h.id, now, "rate_asked", recipe=saved.title)
        return [self._card(h, saved, now, lead), messages.rate_ai_recipe(saved.title)]

    def _save_source(self, h: Household, text: str, site: str, url: str, source: str, now: datetime, what: str,
                     watched: bool = False, lead: str = "") -> list:
        self._last_saved = None
        if self.chef is None:
            return [messages.NEEDS_AI]
        try:
            data = self.chef.from_source(text, site)
        except Exception as err:
            self.store.log(h.id, now, "chef_error", error=str(err)[:300])
            return [messages.AI_DOWN]
        complete = bool((data or {}).get("complete"))
        if watched:
            note = messages.WATCHED if complete else messages.WATCHED_GAPS
        else:
            note = "" if complete else {"link": messages.LINK_PARTIAL, "photo": messages.SHOT_NOTE}.get(
                source, "AI filled in the parts that were missing.")
        recipe = recipe_from_dict(data, h.id, source, url, site, note) if data else None
        if recipe is None:
            return [messages.link_no_recipe(what)]
        saved = self.book.save(recipe, now)
        self._last_saved = saved
        self._signal(h, taste.cuisine_of(recipe.title), 1.5, now)
        self.store.log(h.id, now, "recipe_saved", recipe=saved.title, source=source, site=site, complete=complete, watched=watched)
        return [self._card(h, saved, now, lead or "Saved to your recipes.")]

    def _kitchen(self, h: Household, kind: str, args: list[str], now: datetime) -> list:
        book = self.book
        if kind == "link":
            url = args[0]
            known = book.by_url(h.id, url)
            if known:
                return [self._card(h, known, now, "You've saved this one already.")]
            page, problem = None, ""
            try:
                page = self.fetch(url)
            except Exception as err:
                problem = str(err)[:80]
                self.store.log(h.id, now, "link_error", site=links.site_of(url), error=str(err)[:200])
            if self.watcher is not None and self.chef is not None and youtube_id(url):
                # A YouTube video: watch it, rather than guessing from the title and caption.
                try:
                    notes = self.watcher.watch_youtube(url)
                    if notes:
                        title = page.title if page else ""
                        return self._save_source(h, f"{title}\n{notes}".strip(), "youtube.com", url, "link", now,
                                                 title or "that video", watched=True)
                    self.store.log(h.id, now, "watch_no_recipe", url=url)
                except Exception as err:
                    self.store.log(h.id, now, "watch_error", error=str(err)[:300])
            if page is None:
                blocked = re.search(r"answered (401|402|403|406|429|451)", problem)
                if blocked:  # the site refuses automated readers; open a window for screenshots instead
                    self.book.set_shots(h.id, "", None, now + timedelta(minutes=15))
                    return [messages.link_blocked(links.site_of(url), blocked.group(1), links.dish_from_url(url))]
                return [messages.link_failed(links.site_of(url), problem)]
            if page.recipe:  # the site published the full recipe: copy it, no AI involved
                recipe = recipe_from_dict(page.recipe, h.id, "link", url, page.site)
                if recipe:
                    saved = book.save(recipe, now)
                    self.store.log(h.id, now, "recipe_saved", recipe=saved.title, source="link", site=page.site, complete=True)
                    return [self._card(h, saved, now, "Saved to your recipes.")]
            if len(page.text.strip()) < 15:
                return [messages.link_failed(page.site, "it didn't show me any text")]
            return self._save_source(h, page.text, page.site, url, "link", now, page.title or page.site)
        if kind == "paste":
            return self._save_source(h, args[0], "", "", "pasted", now, "what you sent")
        if kind == "recipe":
            self._signal(h, taste.cuisine_of(args[0]), 1.0, now)
            found = book.search(h.id, args[0])
            if found:
                lead = "I wrote this one for you before." if found.source == "ai" else "Found it in your saved recipes."
                return [self._card(h, found, now, lead)]
            return self._new_recipe(h, args[0], now, f"You haven't saved a {args[0]} recipe, so I wrote one.")
        if kind == "recipes":
            saved = book.kept(h.id)
            if not saved:
                return [messages.NO_RECIPES]
            where = {"ai": "AI-generated", "pasted": "pasted by you", "video": "from a video you sent",
                     "photo": "from your screenshot"}
            return [messages.recipe_list([(r.title, where.get(r.source) or f"from {r.site or 'a link'}") for r in saved[:15]])]
        if kind == "shopfor":
            return [self._shopping_for(h, args[0], now)]
        if kind == "order":
            return self._order_start(h, self._profile(h, now), now)
        if kind == "missing":
            return [self._missing(h, now)]
        if kind == "outside":
            return [self._outside(h, self._profile(h, now), now)]
        if kind == "more":
            return [self._more(h, self._profile(h, now), now)]
        if kind == "less":
            return [self._less(h, now)]
        if kind == "shopping":
            names = messages.unique_names(book.shopping(h.id))
            if not names:
                return [messages.SHOPPING_EMPTY]
            return [self._shopping_card(h, names)]
        if kind == "clearshop":
            book.clear_shopping(h.id, now)
            return ["Shopping list cleared."]
        if kind == "shopadd":
            added = book.add_shopping(h.id, args, now)
            self.store.log(h.id, now, "shopping_added", items=added)
            return [self._with_export(h, messages.shop_added(added, len(book.shopping(h.id))))]
        if kind == "roast":
            names = [i.name for i in self.store.active_items(h.id)]
            if not names:
                return [messages.ROAST_EMPTY]
            if self.chef is None:
                return [messages.NEEDS_AI]
            try:
                self.store.log(h.id, now, "roast")
                return ["🔥 " + self.chef.roast(names)]
            except Exception as err:
                self.store.log(h.id, now, "chef_error", error=str(err)[:300])
                return [messages.AI_DOWN]

        current = book.current(h.id)
        if kind == "steps":
            want = self._locked_today(h, now)
            if want and (current is None or current.title.lower() != want.lower()):
                # COOK after locking in today's pick means that dish: find it saved, or write it (AI-labelled)
                before = current.id if current else None
                shown = self._kitchen(h, "recipe", [want], now)
                current = book.current(h.id)
                if current is None or current.id == before:
                    return shown
            if current is None:
                return self._command(h, "dinner", now)
            return [messages.recipe_steps(current.title, [(i.name, i.quantity) for i in current.ingredients],
                                          current.steps, self._source_line(current))]
        if current is None:
            return [messages.NO_CURRENT]
        have, missing, _ = self._split(h, current)
        if kind == "shop":
            if not missing:
                return [f"You have everything for {current.title} already."]
            added = book.add_shopping(h.id, messages.unique_names([m.name for m in missing]), now)
            self.store.log(h.id, now, "shopping_added", items=added, recipe=current.title)
            return [self._with_export(h, messages.shop_added(added, len(book.shopping(h.id))))]
        # audio: the script is about the dish (so it's made once and reused); what this home has goes in the caption
        pairs = [(i.name, i.quantity) for i in current.ingredients]
        key = recipe_key(current.title, pairs, current.steps)
        cached = self.audio_cache.script(key)
        if cached:
            script, ai_written = cached
        else:
            script, ai_written = messages.plain_script(current.title, pairs, current.steps), False
            if self.chef is not None:
                try:
                    meta = ", ".join(p for p in (f"serves {current.servings}" if current.servings else "",
                                                 f"{current.minutes} minutes" if current.minutes else "") if p)
                    written = self.chef.narrate(current.title, meta, [f"{q} {n}".strip() for n, q in pairs], current.steps)
                    if len(written) > 200:
                        script, ai_written = written, True
                except Exception as err:
                    self.store.log(h.id, now, "chef_error", error=str(err)[:300])
            script = re.sub(r"[^\w\s.,;:!?'\"()\-%/°&]", "", script)  # nothing a voice would stumble on
            if ai_written:   # a plain script costs nothing to remake; only keep the ones Claude wrote
                self.audio_cache.save_script(key, current.title, script, ai_written, now)
        self.store.log(h.id, now, "audio", recipe=current.title, ai_script=ai_written, script_reused=bool(cached))
        caption = messages.audio_caption(current.title, ai_written, messages.unique_names([i.name for i in have]), messages.unique_names([m.name for m in missing]))
        return [Audio(f"Cook-along: {current.title}", script, caption, key=key)]

    def save_video(self, phone: str, notes: str | None, now: datetime) -> list:
        """Someone sent a video file and the video model has watched it. `notes` is None if it wasn't cooking."""
        with self.lock:
            member = self.accounts.member(phone)
            h = self.store.get_household(member.household_id) if member and member.status == "active" \
                else self.store.get_household_by_phone(phone)
            if h is None or not h.opted_in:
                return [messages.WELCOME]
            prof = self._profile(h, now)
            self.store.log(h.id, now, "inbound", text="", media=1, video=True)
            out = [messages.VIDEO_NOT_RECIPE] if not notes else \
                self._save_source(h, notes, "", "", "video", now, "that video", watched=True)
            return [self._say(prof, r) for r in out]

    def menu_seen(self, phone: str, place: str, dishes: list[dict], now: datetime) -> list:
        """A menu or delivery-app screenshot was read. Always check which dish before writing anything."""
        with self.lock:
            h = self._resolve(phone)
            if h is None or not h.opted_in:
                return [messages.WELCOME]
            prof = self._profile(h, now)
            self.store.log(h.id, now, "inbound", text="", media=1, menu=len(dishes))
            if not dishes:
                return [self._say(prof, messages.MENU_NONE)]
            self.book.set_menu(h.id, place, dishes, now + timedelta(minutes=15))
            return [self._say(prof, messages.menu_ask(place, dishes))]

    def _menu_pick(self, h: Household, text: str, intent_kind: str, now: datetime) -> list | None:
        pending = self.book.menu(h.id, now)
        if pending is None:
            return None
        place, dishes = pending
        t = re.sub(r"^(?:the|a|an|number|option|#)\s*", "", text.strip().lower().rstrip(".!")).strip()
        choice = None
        if len(dishes) == 1 and (intent_kind == "start" or t in {"yeah", "yep", "sure", "ok", "okay", "do it", "go", "please", "1"}):
            choice = dishes[0]
        elif re.fullmatch(r"#?\d{1,2}", t) and 1 <= int(t.lstrip("#")) <= len(dishes):
            choice = dishes[int(t.lstrip("#")) - 1]
        else:
            named = [d for d in dishes if t and (t == d["name"].lower() or (len(t) > 3 and t in d["name"].lower()))]
            choice = named[0] if len(named) == 1 else None
        if choice is None:
            if t in {"no", "nope", "none", "cancel", "never mind", "nevermind"}:
                self.book.clear_menu(h.id)
                return ["No problem. Send another menu, or name the dish, e.g. RECIPE butter chicken."]
            return None  # something else entirely; let it be handled normally
        self.book.clear_menu(h.id)
        request = choice["name"] + (f" ({choice['description']})" if choice["description"] else "") + \
            ", as a home-kitchen version of the restaurant dish"
        self.store.log(h.id, now, "menu_pick", dish=choice["name"], place=place)
        return self._new_recipe(h, request, now, messages.menu_lead(choice["name"], place))

    def _resolve(self, phone: str) -> Household | None:
        member = self.accounts.member(phone)
        if member and member.status == "active":
            return self.store.get_household(member.household_id)
        return self.store.get_household_by_phone(phone)

    def wants_recipe_photo(self, phone: str, caption: str, now: datetime) -> bool:
        """Should the next photo be read as a recipe screenshot rather than as food?"""
        with self.lock:
            h = self._resolve(phone)
            if h is None or not h.opted_in or self.chef is None:
                return False
            return bool(re.search(r"\brecipe\b", caption or "", re.I)) or self.book.shots(h.id, now) is not None

    def save_recipe_shot(self, phone: str, text: str | None, now: datetime) -> list:
        """A recipe screenshot has been read. Shots sent within a few minutes are merged into one recipe."""
        with self.lock:
            h = self._resolve(phone)
            if h is None or not h.opted_in:
                return [messages.WELCOME]
            prof = self._profile(h, now)
            self.store.log(h.id, now, "inbound", text="", media=1, recipe_shot=True)
            if not text:
                return [self._say(prof, messages.SHOT_NOT_RECIPE)]
            open_session = self.book.shots(h.id, now)
            so_far, previous = open_session if open_session else ("", None)
            combined = f"{so_far}\n---\n{text}".strip("\n-") if so_far else text
            lead = "Added your next screenshot. Here's the full recipe now." if previous else ""
            out = self._save_source(h, combined, "", "", "photo", now, "that screenshot", lead=lead)
            saved = self._last_saved
            if saved is not None:
                if previous:
                    self.book.delete(previous)  # replaced by the merged version
                if saved.note:
                    out.append(messages.SHOT_MORE)
            self.book.set_shots(h.id, combined if saved else so_far, saved.id if saved else previous, now + timedelta(minutes=10))
            return [self._say(prof, r) for r in out]

    def _saved_recipe_hint(self, h: Household, today) -> tuple[str, list[str]] | None:
        """On the 4:30 text: if one of their own saved recipes would use what's about to turn, say so."""
        fresh = [e.item.name for e in self.engine.watchlist(h, today) if not e.past_window]
        best: tuple[int, str, list[str]] | None = None
        for r in self.book.all(h.id):
            uses = [name for name in fresh if any(same_food(i.name, name) for i in r.ingredients if not i.staple)]
            if uses and (best is None or len(uses) > best[0]):
                best = (len(uses), r.title, uses)
        return (best[1], best[2]) if best else None

    # the agent ---------------------------------------------------------
    def _agent(self, h: Household, text: str, now: datetime) -> list | None:
        """Free-form message: let Bud work out what's wanted and use the same functions the commands use."""
        today = self.engine.local(h, now).date()
        day = today.isoformat()
        if sum(1 for e in self.store.events(h.id, "agent") if e["payload"].get("day") == day) >= self.config.agent_daily_cap:
            return None
        outbox: list = []

        def shown(items: list) -> str:
            outbox.extend(items)
            summary = " / ".join((i if isinstance(i, str) else f"[audio: {i.title}]")[:400] for i in items)
            return "SHOWN to the person: " + summary

        def execute(name: str, args: dict) -> str:
            hh = self.store.get_household(h.id)
            if name == "get_fridge":
                entries = plan.entries(self.engine, hh.id, today)
                tracked = {e.item.id for e in entries}
                others = [i.name for i in self.store.active_items(hh.id) if i.id not in tracked]
                body = messages.everything([(e.item.name, plan.by_when(e.days_left, today)) for e in entries[:30]], others[:30])
                return f"{body}\nList last updated: {self._as_of(hh, now) or 'never; no photo or list yet'}"
            if name == "run_command":
                command = str(args.get("command", "")).strip()
                if service_command(command):
                    return shown(self._command(hh, command, now))
                if validate(command, 1.0):
                    return shown(self.engine.handle_inbound(hh.phone, command, now))
                return "That isn't a command I can run."
            if name == "create_recipe":
                return shown(self._new_recipe(hh, str(args.get("request", "")), now, "Here's one I wrote for that."))
            if name == "save_recipe_text":
                return shown(self._save_source(hh, str(args.get("text", "")), "", "", "pasted", now, "what you sent"))
            if name == "add_to_shopping_list":
                added = self.book.add_shopping(hh.id, [str(i) for i in (args.get("items") or [])][:30], now)
                return shown([self._with_export(hh, messages.shop_added(added, len(self.book.shopping(hh.id))))])
            return "Unknown tool."

        current = self.book.current(h.id)
        context = (f"Local time {self.engine.local(h, now):%a %b %d, %H:%M}. Current recipe: {current.title if current else 'none'}. "
                   f"Shopping list: {len(self.book.shopping(h.id))} items.")
        taste = self._taste(h)
        if taste:
            context += f" Their taste: {taste}."
        quoted = self._quoted(h.phone)
        if quoted:
            context += f' They are replying to this message of yours: "{quoted}"'
        if any(now - _at(e) <= timedelta(minutes=15) for e in self.store.events(h.id, "photo_unreadable")):
            context += (" Their latest photo showed no readable food, and they may be asking about it. Open by saying plainly"
                        " that no food showed in that photo, so you are going from the fridge list you already have (say when it"
                        " was last updated), then answer. Never imply the photo showed anything.")
        try:
            final, used = self.agent.run(text, execute, context, history=self._recent_turns(h, now))
        except Exception as err:  # a model outage must never break the conversation
            self.store.log(h.id, now, "agent_error", error=str(err)[:300])
            return None
        self.store.log(h.id, now, "agent", day=day, text=text[:300], tools=used, reply=(final or "")[:800])
        return (outbox + ([final] if final else [])) or None
