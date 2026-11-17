"""The Sunday rescue receipt: built to be screenshotted and shared.

It leads with dollars kept, lists what was rescued, and ends with a way for a
friend to try it. Dollars only count items the household said they used, at
the prices on their own receipts. Nothing is estimated.
"""
from __future__ import annotations

from datetime import date, timedelta

from .models import Household
from .store import Store

MAX_LINES = 8


def _n(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _week(start: date, end: date) -> str:
    if start.month == end.month:
        return f"{start:%b} {start.day} to {end.day}"
    return f"{start:%b} {start.day} to {end:%b} {end.day}"


def build(store: Store, h: Household, today: date, voice: str = "plain", number: str = "",
          code: str = "") -> tuple[str, bool] | None:
    """Return (text, asks_for_dollar_estimate), or None when there is nothing to report."""
    start = today - timedelta(days=6)
    closed = store.items_closed_between(h.id, start, today)
    used = [i for i in closed if i.status == "used"]
    tossed = [i for i in closed if i.status == "tossed"]
    if not used and not tossed:
        return None

    kept = round(sum(i.price for i in used if i.price), 2)
    bud = voice == "bud"
    lines = ["🧾 BUD'S RESCUE RECEIPT" if bud else "🧾 FRIDGEBUD RESCUE RECEIPT", f"Week of {_week(start, today)}", ""]

    # Lead with money when we have real prices; otherwise lead with the count.
    if kept:
        lines.append(f"${kept:.2f} KEPT OUT OF THE BIN")
    elif used:
        lines.append(f"{_n(len(used), 'item').upper()} KEPT OUT OF THE BIN")
    else:
        lines.append("NOTHING RESCUED THIS WEEK" if not bud else "A QUIET WEEK. WE GO AGAIN.")
    lines.append("")

    rows = []
    for i in used:
        if i.kind == "leftover":
            rows.append(f"✅ {i.name} (leftovers finished)")
        else:
            rows.append(f"✅ {i.name}" + (f"  ${i.price:.2f}" if i.price else ""))
    rows += [f"🪦 {i.name}" if bud else f"✗ {i.name} (tossed)" for i in tossed]
    lines += rows[:MAX_LINES]
    if len(rows) > MAX_LINES:
        lines.append(f"+ {len(rows) - MAX_LINES} more")

    total = len(used) + len(tossed)
    lines += ["", f"Rescue rate: {round(100 * len(used) / total)}% ({len(used)} of {total})"]
    if kept:
        lines.append("Dollars are your own receipt prices, for items you told me you used.")

    ask = bool(used) and any(not i.price for i in used if i.kind != "leftover")
    if ask:
        lines.append("Roughly what did the unpriced items save you? Reply with a dollar amount.")

    where = f" to {number}" if number else ""
    invite = f"HI {code}" if code else "HI"
    lines += ["", ("I'm proud of us. " if bud and used else "") +
              f"Know someone who bins food? Screenshot this. They can text {invite}{where}."]
    return "\n".join(lines), ask
