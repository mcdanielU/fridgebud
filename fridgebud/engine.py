"""The FridgeBud loop: inbound replies in, daily nudges out.

Channel-agnostic: a messaging adapter (iMessage/SMS webhook) calls
`handle_inbound` and sends whatever comes back; a scheduler calls
`due_nudges` every few minutes. Time is always passed in, so the whole loop
can be simulated a week at a time.
"""
from __future__ import annotations

import difflib
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import taste, messages, recipes, watchlist
from .capture import CapturedItem, ItemExtractor, NoopExtractor
from .models import Household, Item
from .parser import parse
from .shelf_life import ShelfLifeTable, normalize
from .store import Store


class Engine:
    def __init__(self, store: Store, table: ShelfLifeTable | None = None, extractor: ItemExtractor | None = None):
        self.store = store
        self.table = table or ShelfLifeTable()
        self.extractor = extractor or NoopExtractor()

    # helpers ----------------------------------------------------------
    @staticmethod
    def local(h: Household, now: datetime) -> datetime:
        return now.astimezone(ZoneInfo(h.timezone))

    def _today(self, h: Household, now: datetime) -> date:
        return self.local(h, now).date()

    def watchlist(self, h: Household, today: date) -> list[watchlist.Entry]:
        return watchlist.build(self.store, self.table, h.id, today)

    def _match(self, h: Household, name: str, pool: list[Item] | None = None) -> Item | None:
        items = pool if pool is not None else self.store.active_items(h.id)
        if not items:
            return None
        target = normalize(name)
        rule = self.table.lookup(name)
        if rule:  # same food family beats string similarity ("spinach" vs "baby spinach")
            same = [i for i in items if i.key == rule.key]
            if same:
                return same[0]
        names = {normalize(i.name): i for i in items}
        close = difflib.get_close_matches(target, list(names), n=1, cutoff=0.6)
        if close:
            return names[close[0]]
        contains = [i for n, i in names.items() if target and (target in n or n in target)]
        return contains[0] if contains else None

    # inbound ----------------------------------------------------------
    def handle_inbound(self, phone: str, text: str, now: datetime, media: list[bytes] | None = None,
                       media_types: list[str] | None = None) -> list[str]:
        h = self.store.get_household_by_phone(phone)
        if h is None:
            h = self.store.create_household(phone)
            h.opted_in = False
            self.store.save_household(h)
            self.store.log(h.id, now, "signup")
            if parse(text).kind == "start":
                return self._consent(h, now)
            return [messages.WELCOME]

        self.store.log(h.id, now, "inbound", text=text, media=len(media or []))
        today = self._today(h, now)
        intent = parse(text)

        if intent.kind == "stop":
            h.opted_in = False
            self.store.save_household(h)
            self.store.log(h.id, now, "opt_out")
            return [messages.STOPPED]
        if not h.opted_in:
            if intent.kind == "start":
                return self._consent(h, now)
            return [messages.WELCOME]
        if media:
            return [self._photo(h, media, media_types or [], today, now)]

        if intent.kind == "start":
            return [messages.CONSENTED]
        if intent.kind == "help":
            return [messages.HELP]
        if intent.kind == "list":
            wl = self.watchlist(h, today)
            others = len(self.store.active_items(h.id)) - len(wl)
            return [messages.watchlist_text(wl, others)]
        if intent.kind == "add":
            return [self._add(h, intent.items, today, now, source="text")]
        if intent.kind == "leftover":
            name = intent.items[0] if intent.items and intent.items[0] else "leftovers"
            self.store.add_item(h.id, name, None, "leftover", "leftover", today, intent.portions)
            self.store.log(h.id, now, "leftover_added", name=name, portions=intent.portions)
            portions = f" ({intent.portions} portions)" if intent.portions else ""
            return [f"Noted: {name}{portions}. I'll remind you before it's past day 3."]
        if intent.kind == "made":
            return [self._made(h, today, now)]
        if intent.kind in {"tossed", "good", "used"}:
            return [self._close(h, intent.kind, intent.items, today, now)]
        if intent.kind == "dislike":
            return [self._dislike(h, intent.items, now)]
        if intent.kind == "liked":
            return [self._liked(h, intent.items, now)]
        if intent.kind == "diet":
            return [self._diet(h, ", ".join(intent.items), now)]
        self.store.log(h.id, now, "unknown", text=text)
        return [messages.UNKNOWN]

    def _consent(self, h: Household, now: datetime) -> list[str]:
        h.opted_in = True
        self.store.save_household(h)
        self.store.log(h.id, now, "consent", method="reply_yes")
        return [messages.CONSENTED]

    def _photo(self, h: Household, media: list[bytes], media_types: list[str], today: date, now: datetime) -> str:
        captured: list[CapturedItem] = []
        for i, blob in enumerate(media):
            mtype = media_types[i] if i < len(media_types) else "image/jpeg"
            try:
                captured.extend(self.extractor.extract(blob, mtype))
            except Exception as err:  # a failed photo must never break the conversation
                self.store.log(h.id, now, "capture_error", error=str(err)[:300])
                print(f"(photo read failed: {type(err).__name__}: {str(err)[:300]})", flush=True)   # shows up in `fly logs`
        if not captured:
            self.store.log(h.id, now, "photo_unreadable")   # lets a follow-up question say so instead of going quiet about it
            return messages.PHOTO_UNREADABLE
        sure = [c for c in captured if c.confident]
        unsure = [c for c in captured if not c.confident]
        reply = self._add(h, sure, today, now, source="photo") if sure else ""
        if unsure:
            self.store.log(h.id, now, "capture_unsure", items=[c.name for c in unsure])
        return messages.photo_added(reply, [c.name for c in unsure], any(c.price for c in sure))

    def _add(self, h: Household, entries: list[str | CapturedItem], today: date, now: datetime, source: str) -> str:
        tracked, untracked = [], []
        for entry in entries:
            item = entry if isinstance(entry, CapturedItem) else CapturedItem(entry)
            name = normalize(item.name) or item.name.strip()
            if not name:
                continue
            rule = self.table.lookup(item.name)
            self.store.add_item(h.id, name, rule.key if rule else None, rule.category if rule else None,
                                "grocery", today, price=item.price)
            (tracked if rule else untracked).append(name)
        self.store.log(h.id, now, "capture", source=source, tracked=tracked, untracked=untracked)
        return messages.added(tracked, untracked)

    def _made(self, h: Household, today: date, now: datetime) -> str:
        ids = (h.last_suggestion or {}).get("item_ids", [])
        active = {i.id: i for i in self.store.active_items(h.id)}
        rescued = [active[i] for i in ids if i in active]
        for item in rescued:
            self.store.close_item(item.id, "used", today)
            watchlist.learn_from_reply(self.store, self.table, item, "used", today)
        self.store.log(h.id, now, "made", recipe=(h.last_suggestion or {}).get("recipe"), rescued=[i.name for i in rescued])
        if getattr(self, "reward", None) is not None:
            return self.reward(h, rescued, (h.last_suggestion or {}).get("recipe"), today, now)
        if not rescued:
            return "Nice. If you made leftovers, text e.g. \"leftovers chili x3\"."
        return f"Nice, that rescued {', '.join(i.name for i in rescued)}. Any leftovers? Text e.g. \"leftovers chili x3\"."

    _FILLER = {"it", "that", "this", "the dish", "dinner", "it all", "the recipe"}

    def _dislike(self, h: Household, names: list[str], now: datetime) -> str:
        cleaned = [raw.strip() for raw in (names or []) if raw.strip() and raw.strip().lower() not in self._FILLER]
        added = [raw for raw in cleaned if raw.lower() not in h.dislikes]
        for raw in added:
            h.dislikes.append(raw.lower())
        if not added:
            return "Tell me what to skip, e.g. \"dislike mushrooms\"."
        self.store.save_household(h)
        self.store.log(h.id, now, "disliked", items=added)
        return messages.disliked(added)

    def _liked(self, h: Household, names: list[str], now: datetime) -> str:
        cleaned = [raw.strip() for raw in (names or []) if raw.strip() and raw.strip().lower() not in self._FILLER]
        if not cleaned:
            return "Tell me what you liked, e.g. \"liked jollof rice\"."
        removed = [raw for raw in cleaned if raw.lower() in h.dislikes]
        for raw in removed:
            h.dislikes.remove(raw.lower())
        self.store.save_household(h)
        self.store.log(h.id, now, "liked", items=cleaned)
        return messages.liked(cleaned, removed)

    def _diet(self, h: Household, note: str, now: datetime) -> str:
        note = note.strip()
        if not note:
            return "Tell me what to go by, e.g. \"diet vegetarian, no pork, loves Nigerian and Thai food\"."
        h.diet_notes = note
        self.store.save_household(h)
        self.store.log(h.id, now, "diet_set", note=note)
        return messages.diet_set(note)

    def _close(self, h: Household, kind: str, names: list[str], today: date, now: datetime) -> str:
        outcome = {"tossed": "tossed", "good": "still_good", "used": "used"}[kind]
        active = self.store.active_items(h.id)
        if not names:
            # A bare "good"/"tossed" refers to whatever the last nudge asked about.
            asked = (h.last_suggestion or {}).get("check_ids", [])
            pool = [i for i in active if i.id in asked]
            if not pool:
                return f"Which item? e.g. \"{kind.upper()} spinach\"."
            matched = pool
        else:
            matched = [m for m in (self._match(h, n, active) for n in names) if m]
        if not matched:
            return f"I couldn't find {', '.join(names)} on your list. Reply LIST to see it."
        for item in matched:
            if outcome == "still_good":
                watchlist.learn_from_reply(self.store, self.table, item, outcome, today)
                self.store.confirm_good(item.id, today)
            else:
                watchlist.learn_from_reply(self.store, self.table, item, outcome, today)
                self.store.close_item(item.id, "used" if outcome == "used" else "tossed", today)
        self.store.log(h.id, now, outcome, items=[i.name for i in matched])
        listed = ", ".join(i.name for i in matched)
        return {
            "tossed": f"Logged {listed} as tossed. I'll flag that kind of thing a little earlier next time.",
            "still_good": f"Good to know: {listed}. I'll check back in a couple of days.",
            "used": f"Logged {listed} as used. Nice.",
        }[outcome]

    # outbound ---------------------------------------------------------
    def due_nudges(self, now: datetime, quiet_hour: int | None = None) -> list[tuple[str, str]]:
        """Call every few minutes. Returns (phone, text) pairs to send now.
        quiet_hour: if the server was down at 4:30 and only comes back late, skip today rather than text at 11 PM."""
        out = []
        for h in self.store.all_households():
            if not h.opted_in:
                continue
            local = self.local(h, now)
            if h.last_nudge_on == local.date():
                continue
            if (local.hour, local.minute) < (h.nudge_hour, h.nudge_minute):
                continue
            if quiet_hour is not None and local.hour >= quiet_hour:
                continue
            skip = getattr(self, "skip_today", None)
            reason = skip(h, local) if skip else None
            if reason:                      # LESS, or they've gone quiet: every other day
                h.last_nudge_on = local.date()
                self.store.save_household(h)
                self.store.log(h.id, now, "nudge_skipped", reason=reason)
                continue
            text = self.compose_nudge(h, local.date(), now)
            h = self.store.get_household(h.id)  # compose_nudge saved the suggestion
            h.last_nudge_on = local.date()
            self.store.save_household(h)
            if text:
                self.store.log(h.id, now, "nudge", text=text)
                self._log_decision(h, now, local)
                out.append((h.phone, text))
            else:
                self.store.log(h.id, now, "nudge_skipped", reason="nothing at risk")
        return out

    POLICY = "rules_v2"

    def _log_decision(self, h: Household, now: datetime, local: datetime) -> None:
        """Training data for later: what Bud knew when it picked (context), what it picked (action), under which
        policy. The outcome (cooked, swapped, rated, ignored) is joined on afterwards by fridgebud.dataset."""
        state = h.last_suggestion or {}
        title = state.get("recipe")
        if not title:
            return
        p = h.persona or {}
        w = taste.weights(p)
        self.store.log(
            h.id, now, "decision", policy=self.POLICY, recipe=title,
            cuisine=recipes.cuisine_for(title) or taste.cuisine_of(title), novelty=state.get("novelty"),
            rescues=len(state.get("item_ids") or []), hour=local.hour, weekday=local.weekday(),
            variety=round(taste.variety(p), 3), bar=taste.bar(p),
            taste={c: round(v, 2) for c, v in sorted(w.items(), key=lambda kv: -kv[1])[:6]},
            on_hand=len(self.store.active_items(h.id)), at_risk=len(self.watchlist(h, local.date())),
            cadence=p.get("cadence"), main_meal=p.get("main_meal"))

    idea_fallback = None   # set by the service: writes an AI idea when nothing in the dish list fits the fridge well

    def compose_nudge(self, h: Household, today: date, now: datetime | None = None) -> str | None:
        wl = self.watchlist(h, today)
        last = (h.last_suggestion or {}).get("recipe")
        meal = "lunch" if (h.persona or {}).get("main_meal") == "lunch" else "dinner"   # their main meal's dishes
        pick = recipes.suggest(wl, self.store.active_items(h.id), h.dislikes, avoid=last, meal=meal, any_day=True,
                               **taste.context(self.store, h, today))
        if (pick is None or pick.weak) and self.idea_fallback and now is not None:
            alt = self.idea_fallback(h, meal, now, [last] if last else [])
            if alt:
                pick = alt
        state = pick.as_state() if pick else {}
        state["check_ids"] = [e.item.id for e in wl if e.past_window]
        h.last_suggestion = state
        self.store.save_household(h)
        return messages.daily_nudge(wl, pick)

    def weekly_receipt(self, h: Household, today: date) -> str:
        closed = self.store.items_closed_between(h.id, today - timedelta(days=6), today)
        used = sum(1 for i in closed if i.status == "used" and i.kind != "leftover")
        leftovers = sum(1 for i in closed if i.status == "used" and i.kind == "leftover")
        tossed = sum(1 for i in closed if i.status == "tossed")
        # Only confirmed-used items with a real price count toward dollars kept.
        kept = round(sum(i.price for i in closed if i.status == "used" and i.price), 2)
        priced = sum(1 for i in closed if i.status == "used" and i.price)
        return messages.weekly_receipt(used, tossed, leftovers, kept, priced)
