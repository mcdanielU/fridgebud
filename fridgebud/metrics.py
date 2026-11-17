"""Is FridgeBud being used, and is it solving the problem? Computed from the event log and the items table.

    scorecard()  the six launch gates (feeds the Weekly Scorecard)
    report()     usage, retention, value delivered, friction and a per-home breakdown
    render()     report() as a phone-sized text: what the owner gets for STATS and the morning digest

    python3 -m fridgebud.metrics            # the full report as text, for FRIDGEBUD_DB
    python3 -m fridgebud.metrics --json     # the same, as JSON
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .accounts import Accounts
from .store import Store


def _at(event: dict) -> datetime:
    return datetime.fromisoformat(event["at"])


def _pct(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def scorecard(store: Store, now: datetime) -> dict:
    accounts = Accounts(store)
    profiles = accounts.all()
    consented = [p for p in profiles if p.consented_at]
    events: dict[int, list[dict]] = {}
    for e in store.events():
        events.setdefault(e["household_id"], []).append(e)

    # Activation: a photo, receipt or typed list within 24 h of saying YES.
    activated = 0
    for p in consented:
        if any(e["type"] == "capture" and p.consented_at <= _at(e) <= p.consented_at + timedelta(hours=24)
               for e in events.get(p.household_id, [])):
            activated += 1

    # Week-2 replies: of homes at least 14 days in, how many replied on 3+ of days 8 to 14.
    week2_pool = week2_ok = 0
    for p in consented:
        start, end = p.consented_at + timedelta(days=7), p.consented_at + timedelta(days=14)
        if now < end:
            continue
        week2_pool += 1
        days = {_at(e).date() for e in events.get(p.household_id, []) if e["type"] == "inbound" and start <= _at(e) < end}
        week2_ok += len(days) >= 3

    # Food rescued: items marked used in the last 7 days, per opted-in home.
    since = (now - timedelta(days=6)).date().isoformat()
    rescued = []
    for h in store.all_households():
        if h.opted_in:
            row = store.db.execute(
                "SELECT COUNT(*) AS n FROM items WHERE household_id = ? AND status = 'used' AND closed_on >= ?",
                (h.id, since)).fetchone()
            rescued.append(row["n"])

    sharers = {p.referred_by for p in profiles if p.referred_by} | {
        p.household_id for p in consented if accounts.member_count(p.household_id)}

    # Bud vs plain: share of 4:30 texts that got any reply before the next one (24 h).
    voices = {}
    for voice in ("bud", "plain"):
        nudges = replied = 0
        for p in (q for q in consented if q.voice == voice):
            evs = events.get(p.household_id, [])
            inbound = [_at(e) for e in evs if e["type"] == "inbound"]
            for e in evs:
                if e["type"] == "nudge" and _at(e) + timedelta(hours=24) <= now:
                    nudges += 1
                    replied += any(_at(e) < t <= _at(e) + timedelta(hours=24) for t in inbound)
        voices[voice] = {"households": sum(1 for q in consented if q.voice == voice), "nudges": nudges,
                         "reply_rate_pct": _pct(replied, nudges)}

    return {
        "as_of": now.isoformat(timespec="minutes"),
        "signups": {"value": len(profiles), "target": 150,
                    "by_source": {s: sum(1 for p in profiles if p.source == s) for s in sorted({p.source for p in profiles})}},
        "consented": len(consented),
        "activation_pct": {"value": _pct(activated, len(consented)), "target": 60, "of": len(consented)},
        "week2_replies_pct": {"value": _pct(week2_ok, week2_pool), "target": 50, "of": week2_pool},
        "rescued_median_per_week": {"value": statistics.median(rescued) if rescued else None, "target": 2},
        "paying": {"value": sum(1 for p in profiles if p.paid_at), "target": 15},
        "shares_pct": {"value": _pct(len(sharers), len(consented)), "target": 10,
                       "note": "homes that invited a fridge-mate or referred a signup; screenshots can't be counted"},
        "voice_test": voices,
    }


LOCAL = ZoneInfo("America/Toronto")

# Which events mean a household used which part of Bud.
FEATURES = (
    ("Fridge photo or list", {"capture"}),
    ("Asked for a dinner idea", {"dinner_asked", "menu_pick"}),
    ("Looked up a recipe", {"recipe_shown"}),
    ("Saved their own recipe", {"recipe_saved:own"}),
    ("Cook-along audio", {"audio"}),
    ("Shopping list", {"shopping_added", "instacart_link"}),
    ("Free-form chat", {"agent"}),
    ("Told Bud their taste", {"diet_set", "disliked", "liked", "recipe_liked", "recipe_declined"}),
    ("Said what happened to food", {"used", "tossed", "still_good", "good", "made"}),
    ("👍 on a daily idea", {"locked"}),
    ("Rated a dish 🔥/👌/👎", {"dish_rated"}),
    ("Ordered (sent a list)", {"order_sent"}),
    ("MORE / LESS", {"cadence_set"}),
    ("Asked what Bud knows", {"notebook"}),
    ("Off-script / craving", {"outside_fridge"}),
)


def _loop(homes, events: dict[int, list[dict]], now: datetime) -> dict:
    """The daily loop, last 7 days: is each step of trigger → action → reward → investment working?"""
    from .taste import describe, learned, variety
    since = now - timedelta(days=7)
    recent = {hid: [e for e in evs if _at(e) >= since] for hid, evs in events.items()}
    c = Counter(e["type"] for evs in recent.values() for e in evs)

    def within(kind: str, answer: set[str], hours: float) -> tuple[int, int]:
        sent = answered = 0
        for evs in recent.values():
            for e in evs:
                if e["type"] != kind:
                    continue
                sent += 1
                t = _at(e)
                answered += any(x["type"] in answer and t < _at(x) <= t + timedelta(hours=hours) for x in evs)
        return answered, sent

    skipped = Counter(e["payload"].get("reason", "?") for evs in recent.values() for e in evs if e["type"] == "nudge_skipped")
    rated = Counter(e["payload"].get("rating") for evs in recent.values() for e in evs if e["type"] == "dish_rated")
    tips = [(hid, e) for hid, evs in recent.items() for e in evs if e["type"] == "tip"]
    from .messages import TIPS
    used_by = {key: set(kinds) for key, kinds, _ in TIPS}
    adopted = sum(1 for hid, t in tips if any(x["type"] in used_by.get(t["payload"].get("key"), set()) and _at(x) > _at(t)
                                              for x in events[hid]))
    personas = [h.persona or {} for h in homes]
    cuisines = Counter(c2 for p in personas for c2 in learned(p))
    dial = Counter(describe(variety(p)).split(",")[0] for p in personas if "variety" in p)
    orders_sent = [e for evs in recent.values() for e in evs if e["type"] == "order_sent"]
    return {
        "picks": c["nudge"], "skipped": dict(skipped),
        "cook": _ratio(*within("nudge", {"locked"}, 6)), "swap": _ratio(*within("nudge", {"pick_swapped"}, 6)),
        "option2": sum(1 for evs in recent.values() for e in evs if e["type"] == "locked" and e["payload"].get("option") == 2),
        "made_after": _ratio(*within("nudge", {"made"}, 24)),
        "followup": _ratio(*within("followup", {"heard"}, 3)),
        "rated": dict(rated), "rate_rate": _ratio(sum(rated.values()), c["made"]),
        "milestones": sum(1 for evs in recent.values() for e in evs if e["type"] == "reward" and e["payload"].get("milestone")),
        "est_saved": round(sum(e["payload"].get("worth", 0) for evs in recent.values() for e in evs if e["type"] == "reward")),
        "ignored_ps": _ratio(*within("ignored_ps", {"heard"}, 24)),
        "snoozed_now": sum(1 for p in personas if p.get("snoozed")), "woke": c["woke"],
        "quiet_now": sum(1 for hid, evs in events.items()
                         if sum(1 for e in evs if e["type"] == "nudge" and _at(e) > max(
                             [_at(x) for x in evs if x["type"] == "heard"] or [datetime.min.replace(tzinfo=timezone.utc)])) >= 3),
        "more": sum(1 for evs in recent.values() for e in evs if e["type"] == "cadence_set" and e["payload"].get("to") in ("more", "extra")),
        "less": sum(1 for evs in recent.values() for e in evs if e["type"] == "cadence_set" and e["payload"].get("to") == "less"),
        "extra_homes": dict(Counter(m for p in personas for m in p.get("extra") or [])),
        "every_other_day": sum(1 for p in personas if p.get("cadence") == "alternate"),
        "offers": _ratio(sum(1 for evs in recent.values() for e in evs
                             if e["type"] == "cadence_set" and e["payload"].get("to") == "extra"
                             and e["payload"].get("meals") and len(e["payload"]["meals"]) == 1), c["meal_offer"]),
        "offers_declined": c["meal_offer_declined"], "meal_texts": c["meal_text"],
        "tips": _ratio(adopted, len(tips)),
        "cuisines": dict(cuisines.most_common(5)), "noticed_homes": sum(1 for p in personas if learned(p)),
        "dial": dict(dial),
        "orders": _ratio(len(orders_sent), c["order_started"]), "order_cancelled": c["order_cancelled"],
        "instacart_links": sum(1 for e in orders_sent if e["payload"].get("instacart")),
        "shop_for": c["shopping_for"], "mismatch": c["fridge_mismatch"], "off_script": c["outside_fridge"],
        "images": c["image_made"], "photo_errors": c["photo_error"],
    }


def owners_from_env(env=os.environ) -> set[str]:
    """FRIDGEBUD_OWNER=tg:123,+1519... : the founder's own chats, left out of the numbers and allowed to ask for STATS."""
    return {p.strip() for p in env.get("FRIDGEBUD_OWNER", "").split(",") if p.strip()}


def _key(e: dict) -> str:
    if e["type"] == "recipe_saved":
        return "recipe_saved:ai" if e["payload"].get("source") == "ai" else "recipe_saved:own"
    return e["type"]


def _ratio(part: int, whole: int) -> dict:
    return {"value": _pct(part, whole), "n": part, "of": whole}


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 1)


def _own_recipes(store: Store, household_id: int) -> int:
    """Recipes they brought (links, screenshots, videos, pastes). Counted from the book, since one recipe sent as
    several screenshots logs a save per screenshot."""
    try:
        row = store.db.execute("SELECT COUNT(*) AS n FROM saved_recipes WHERE household_id = ? AND source != 'ai'",
                               (household_id,)).fetchone()
    except Exception:  # no recipe book in this database
        return 0
    return row["n"]


def report(store: Store, now: datetime, exclude: set[str] | frozenset = frozenset(), tz: ZoneInfo = LOCAL) -> dict:
    profiles = {p.household_id: p for p in Accounts(store).all()}
    every = store.all_households()
    homes = [h for h in every if h.phone not in exclude
             and (h.opted_in or (h.id in profiles and profiles[h.id].consented_at))]
    by_phone = {h.phone: h.id for h in every}
    events: dict[int, list[dict]] = {h.id: [] for h in homes}
    for e in store.events():
        hid = e["household_id"] if e["household_id"] is not None else by_phone.get(e["payload"].get("phone"))
        if hid in events:
            events[hid].append(e)

    today = now.astimezone(tz).date()
    week = {today - timedelta(days=i) for i in range(7)}
    month = {today - timedelta(days=i) for i in range(28)}
    since7 = min(week).isoformat()

    def day(e: dict) -> date:
        return _at(e).astimezone(tz).date()

    rows = []
    for h in homes:
        evs = events[h.id]
        prof = profiles.get(h.id)
        inbound = [e for e in evs if e["type"] == "inbound"]
        joined = prof.consented_at if prof and prof.consented_at else (_at(evs[0]) if evs else now)
        join_day = joined.astimezone(tz).date()
        active = {day(e) for e in inbound}
        items = store.db.execute("SELECT status, closed_on, price FROM items WHERE household_id = ?", (h.id,)).fetchall()
        used = [r for r in items if r["status"] == "used"]
        tossed = [r for r in items if r["status"] == "tossed"]
        kinds = Counter(_key(e) for e in evs)
        in_times = [_at(e) for e in inbound]
        made_times = [_at(e) for e in evs if e["type"] == "made"]
        nudges = [_at(e) for e in evs if e["type"] == "nudge" and _at(e) + timedelta(hours=24) <= now]
        outcome_times = [_at(e) for e in evs if e["type"] in ("made", "used", "tossed", "still_good")]
        capture_times = [_at(e) for e in evs if e["type"] == "capture"]

        def followed(kind: str, by: list[datetime], hours: int) -> tuple[int, int]:
            """(times Bud sent `kind`, times the thing it asked for happened within `hours`)"""
            sent = [_at(e) for e in evs if e["type"] == kind]
            return len(sent), sum(1 for s in sent if any(s < t <= s + timedelta(hours=hours) for t in by))
        rows.append({
            "id": h.id,
            "channel": "telegram" if h.phone.startswith("tg:") else "sms",
            "joined": join_day.isoformat(),
            "age_days": (today - join_day).days,
            "active_days": len(active),
            "active_7d": len(active & week),
            "last_active": max(active).isoformat() if active else None,
            "messages": len(inbound),
            "messages_7d": sum(1 for e in inbound if day(e) in week),
            "food_logged": len(items),
            "used": len(used),
            "tossed": len(tossed),
            "used_7d": sum(1 for r in used if (r["closed_on"] or "") >= since7),
            "dollars_saved": round(sum(r["price"] or 0 for r in used), 2),
            "cooked": len(made_times),
            "cooked_7d": sum(1 for t in made_times if t.astimezone(tz).date() in week),
            "recipes_shown": kinds["recipe_shown"],
            "ai_written": kinds["recipe_saved:ai"],
            "own_saved": _own_recipes(store, h.id),
            "liked": kinds["recipe_liked"] + kinds["liked"],
            "disliked": kinds["recipe_declined"] + kinds["disliked"],
            "misunderstood": kinds["misunderstood"],
            "cookalongs": kinds["audio"],
            "audio_voiced": kinds["audio_voiced"],
            "audio_reused": kinds["audio_reused"],
            "errors": {k: n for k, n in kinds.items() if k.endswith("_error")},
            "nudges": len(nudges),
            "nudges_replied": sum(1 for n in nudges if any(n < t <= n + timedelta(hours=24) for t in in_times)),
            "nudges_cooked": sum(1 for n in nudges if any(n < t <= n + timedelta(hours=24) for t in made_times)),
            "opted_out": bool(kinds["opt_out"]),
            "checkins": followed("checkin", outcome_times, 14),
            "chases": followed("activation_chase", capture_times, 72),
            "refresh_asks": followed("refresh_ask", capture_times, 72),
            "timezone_set": bool(kinds["timezone_set"]),
            "_active": active,
            "_join": join_day,
            "_kinds": kinds,
            "_reply_secs": [e["payload"]["secs"] for e in evs if e["type"] == "handled" and "secs" in e["payload"]],
            "_missed": [(e["at"], e["payload"].get("text", "")) for e in evs if e["type"] == "misunderstood"],
        })

    n = len(rows)
    active_7d = [r for r in rows if r["active_7d"]]
    retention = {}
    for d in (1, 7, 14, 30):
        pool = [r for r in rows if r["age_days"] >= d]
        kept = [r for r in pool if any((a - r["_join"]).days >= d for a in r["_active"])]
        retention[f"day_{d}"] = _ratio(len(kept), len(pool))
    daily_avg = sum(sum(1 for r in rows if d in r["_active"]) for d in week) / 7

    used, tossed = sum(r["used"] for r in rows), sum(r["tossed"] for r in rows)
    logged = sum(r["food_logged"] for r in rows)
    liked, disliked = sum(r["liked"] for r in rows), sum(r["disliked"] for r in rows)
    nudges = sum(r["nudges"] for r in rows)
    inbound = sum(r["messages"] for r in rows)
    missed = sum(r["misunderstood"] for r in rows)
    secs = [s for r in rows for s in r["_reply_secs"]]
    errors = Counter()
    for r in rows:
        errors.update(r["errors"])
    examples = sorted((m for r in rows for m in r["_missed"]), reverse=True)[:3]

    return {
        "as_of": now.isoformat(timespec="minutes"),
        "excluded": sorted(exclude),
        "usage": {
            "homes": n,
            "active_today": sum(1 for r in rows if today in r["_active"]),
            "active_7d": len(active_7d),
            "active_28d": sum(1 for r in rows if r["_active"] & month),
            "retention": retention,
            "stickiness_pct": _pct(round(daily_avg * 10), len(active_7d) * 10),
            "msgs_per_active_home_7d": round(sum(r["messages_7d"] for r in active_7d) / len(active_7d), 1) if active_7d else None,
            "features": {label: _ratio(sum(1 for r in rows if keys & set(r["_kinds"])), n) for label, keys in FEATURES},
        },
        "value": {
            "helped_7d": sum(1 for r in rows if r["used_7d"] or r["cooked_7d"]),
            "food": {"logged": logged, "used": used, "tossed": tossed, "rescue_rate": _ratio(used, used + tossed),
                     "outcome_known": _ratio(used + tossed, logged),
                     "dollars_saved": round(sum(r["dollars_saved"] for r in rows), 2)},
            "dinner": {"nudges": nudges, "replied": _ratio(sum(r["nudges_replied"] for r in rows), nudges),
                       "cooked_after_nudge": sum(r["nudges_cooked"] for r in rows),
                       "cooked_total": sum(r["cooked"] for r in rows)},
            "recipes": {"shown": sum(r["recipes_shown"] for r in rows), "ai_written": sum(r["ai_written"] for r in rows),
                        "own_saved": sum(r["own_saved"] for r in rows), "liked": liked, "disliked": disliked,
                        "like_rate": _ratio(liked, liked + disliked),
                        "cookalongs": sum(r["cookalongs"] for r in rows),
                        "audio_voiced": sum(r["audio_voiced"] for r in rows),
                        "audio_reused": sum(r["audio_reused"] for r in rows)},
        },
        "friction": {
            "inbound": inbound,
            "misunderstood": _ratio(missed, inbound),
            "reply_secs": {"median": _percentile(secs, 0.5), "p90": _percentile(secs, 0.9), "measured": len(secs),
                           "over_10s": _ratio(sum(1 for s in secs if s > 10), len(secs))},
            "errors": dict(errors.most_common()),
            "opted_out": sum(1 for r in rows if r["opted_out"]),
            "misunderstood_examples": [text for _, text in examples],
        },
        "proactive": {
            "evening_checkin": _ratio(sum(r["checkins"][1] for r in rows), sum(r["checkins"][0] for r in rows)),
            "activation_chase": _ratio(sum(r["chases"][1] for r in rows), sum(r["chases"][0] for r in rows)),
            "refresh_ask": _ratio(sum(r["refresh_asks"][1] for r in rows), sum(r["refresh_asks"][0] for r in rows)),
            "telegram_city_set": _ratio(sum(1 for r in rows if r["channel"] == "telegram" and r["timezone_set"]),
                                        sum(1 for r in rows if r["channel"] == "telegram")),
        },
        "loop": _loop(homes, events, now),
        "homes": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows],
        "gates": scorecard(store, now),
    }


def _p(r: dict) -> str:
    return "–" if r["value"] is None else f"{r['value']:g}% ({r['n']}/{r['of']})"


def _ago(iso: str | None, today: date) -> str:
    if not iso:
        return "never"
    gap = (today - date.fromisoformat(iso)).days
    return "today" if gap <= 0 else "yesterday" if gap == 1 else f"{gap}d ago"


def render(rep: dict, daily: bool = False) -> str:
    u, v, f = rep["usage"], rep["value"], rep["friction"]
    when = datetime.fromisoformat(rep["as_of"]).astimezone(LOCAL)
    food, dinner, rec, secs = v["food"], v["dinner"], v["recipes"], f["reply_secs"]
    lines = [f"{'☀️ Morning check-in' if daily else '📊 FridgeBud stats'} · {when:%a %b} {when.day}, {when:%H:%M}",
             "Your own chats are left out." if rep["excluded"] else "Includes every chat (set FRIDGEBUD_OWNER to leave yours out)."]
    if u["homes"] < 10:
        lines.append(f"Only {u['homes']} home{'s' if u['homes'] != 1 else ''} so far: trust the per-home lines, "
                     "not the percentages, until there are about 10.")

    lines += ["", "IS IT USED?",
              f"Homes {u['homes']} · active today {u['active_today']} · this week {u['active_7d']} · 28 days {u['active_28d']}",
              "Came back after: " + " · ".join(f"{k.split('_')[1]}d {_p(r)}" for k, r in u["retention"].items()),
              f"Stickiness {'–' if u['stickiness_pct'] is None else str(u['stickiness_pct']) + '%'} "
              "(average day's users ÷ week's users; 20%+ means a habit)",
              f"Messages per active home this week: {u['msgs_per_active_home_7d'] or '–'}"]

    low = food["outcome_known"]["value"] is None or food["outcome_known"]["value"] < 30
    lines += ["", "IS IT SOLVING THE PROBLEM?",
              f"⭐ Homes Bud helped this week: {v['helped_7d']} of {u['active_7d']} active (used up food or cooked a pick)",
              f"Food: {food['logged']} logged · {food['used']} used up · {food['tossed']} tossed · "
              f"rescue rate {_p(food['rescue_rate'])}" + (f" · ${food['dollars_saved']:.0f} saved" if food["dollars_saved"] else ""),
              f"Outcome known for {_p(food['outcome_known'])} of food" + (" ⚠ too thin to trust the rescue rate yet" if low else ""),
              f"4:30 texts: {dinner['nudges']} sent · replied within a day {_p(dinner['replied'])} · "
              f"{dinner['cooked_after_nudge']} led to a cooked meal ({dinner['cooked_total']} cooked in all)",
              f"Recipes: {rec['shown']} shown · {rec['ai_written']} AI-written · {rec['own_saved']} saved by them · "
              f"👍 {rec['liked']} 👎 {rec['disliked']} (like rate {_p(rec['like_rate'])})",
              f"Cook-alongs: {rec['cookalongs']} asked for · {rec['audio_reused']} resent free · "
              f"{rec['audio_voiced']} newly voiced (the only ones Gemini charges for)"]

    errors = ", ".join(f"{k.removesuffix('_error')} {n}" for k, n in f["errors"].items()) or "none"
    lines += ["", "WHERE IT BREAKS",
              f"Didn't understand: {_p(f['misunderstood'])} of messages",
              (f"Reply time: median {secs['median']}s · slowest 10% {secs['p90']}s+ · over 10s {_p(secs['over_10s'])}"
               if secs["measured"] else "Reply time: not measured yet (starts with this version)"),
              f"Errors: {errors}",
              f"Stopped texts: {f['opted_out']}"]
    if f["misunderstood_examples"]:
        lines.append("Bud didn't get: " + " | ".join(f'"{t[:60]}"' for t in f["misunderstood_examples"]))

    pro = rep["proactive"]

    def sent(r: dict, what: str) -> str:
        return f"{r['of']} sent · {what} {_p(r)}" if r["of"] else "none sent yet"
    lines += ["", "BUD REACHING OUT (does each nudge work?)",
              f"8 PM check-ins: {sent(pro['evening_checkin'], 'got an answer')}",
              f"Photo chases (never logged food): {sent(pro['activation_chase'], 'led to a photo')}",
              f"Fresh-list asks: {sent(pro['refresh_ask'], 'led to a photo')}",
              f"Telegram homes on their own time zone: {_p(pro['telegram_city_set'])}"]

    lp = rep["loop"]

    def counts(d: dict) -> str:
        return " · ".join(f"{k} {v}" for k, v in d.items()) or "none"
    lines += ["", "THE DAILY LOOP (last 7 days)",
              f"Daily ideas: {lp['picks']} sent · held back: {counts(lp['skipped'])}",
              f"👍 cook it {_p(lp['cook'])} · 👎 swap {_p(lp['swap'])} · option 2 picked {lp['option2']}×",
              f"Cooked within a day of an idea: {_p(lp['made_after'])}",
              f"\"Still on?\" follow-ups: {_p(lp['followup'])} got a reply within 3h",
              f"After cooking: rated {_p(lp['rate_rate'])} of meals ({counts(lp['rated'])}) · "
              f"{lp['milestones']} milestones · ≈${lp['est_saved']} of food saved (estimates)",
              f"Cheeky P.S. when ignored: {_p(lp['ignored_ps'])} got a reply · gone quiet now {lp['quiet_now']} · "
              f"stepped back {lp['snoozed_now']} · came back {lp['woke']}",
              f"Pace: MORE {lp['more']} · LESS {lp['less']} · every other day {lp['every_other_day']} · "
              f"extra meals {counts(lp['extra_homes'])}",
              f"Breakfast/lunch offers: {_p(lp['offers'])} said yes · {lp['offers_declined']} no · {lp['meal_texts']} meal texts",
              f"Tips: {_p(lp['tips'])} led to trying that feature",
              f"Taste learned for {lp['noticed_homes']} homes: {counts(lp['cuisines'])} · "
              f"adventurousness: {counts(lp['dial'])}",
              f"Orders: {_p(lp['orders'])} of reviews sent ({lp['instacart_links']} to Instacart) · "
              f"{lp['order_cancelled']} cancelled · per-dish lists {lp['shop_for']}",
              f"Didn't have the ingredients {lp['mismatch']}× · wanted something off-script {lp['off_script']}×",
              f"Pictures: {lp['images']} AI photos drawn · {lp['photo_errors']} failed"]

    ranked = sorted(u["features"].items(), key=lambda kv: -(kv[1]["n"]))
    lines += ["", "WHAT THEY USE (share of homes, ever)"]
    lines += [f"{label}: {_p(r)}" for label, r in ranked if r["n"]] or ["Nothing yet"]
    unused = [label for label, r in ranked if not r["n"]]
    if unused and u["homes"]:
        lines.append("Never touched: " + ", ".join(unused))

    today = when.date()
    lines += ["", "PER HOME"]
    for h in sorted(rep["homes"], key=lambda h: h["last_active"] or "", reverse=True):
        lines.append(f"#{h['id']} {h['channel']} · day {h['age_days']} · active {h['active_days']}d, last {_ago(h['last_active'], today)} · "
                     f"{h['messages']} msgs · food {h['food_logged']} ({h['used']} used/{h['tossed']} tossed) · cooked {h['cooked']} · "
                     f"👍{h['liked']} 👎{h['disliked']} · ❓{h['misunderstood']}" + (" · STOPPED" if h["opted_out"] else ""))
    if not rep["homes"]:
        lines.append("No testers yet.")

    g = rep["gates"]
    lines += ["", "Launch gates: activation " + _gate(g["activation_pct"], "%") + " · week-2 replies "
              + _gate(g["week2_replies_pct"], "%") + " · rescued/home/wk " + _gate(g["rescued_median_per_week"], "")
              + " · paying " + _gate(g["paying"], "")]
    return "\n".join(lines)


def _gate(g: dict, unit: str) -> str:
    value = "–" if g["value"] is None else f"{g['value']:g}{unit}"
    return f"{value} (target {g['target']}{unit})"


if __name__ == "__main__":
    store = Store(os.environ.get("FRIDGEBUD_DB", "fridgebud.db"))
    rep = report(store, datetime.now(timezone.utc), owners_from_env())
    print(json.dumps(rep, indent=2, default=str) if "--json" in sys.argv else render(rep))
