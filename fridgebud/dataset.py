"""Turn the events log into a training table: one row per daily pick, with what Bud knew, what it chose, and what
happened next. Run on the server (or a copy of the DB):

    python -m fridgebud.dataset /data/fridgebud.db picks.csv

Labels (all relative to the pick being sent): locked (said yes within 6h), swapped (asked for another within 6h),
made (cooked within 24h), rating (love/fine/nope within 48h), replied (any message from them within 3h).
This is the table a contextual bandit or a small model would train on once there are enough homes.
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timedelta

from .store import Store


def _at(e: dict) -> datetime:
    return datetime.fromisoformat(e["at"])


def rows(store: Store) -> list[dict]:
    out = []
    by_home: dict[int, list[dict]] = {}
    for e in store.events():
        by_home.setdefault(e["household_id"], []).append(e)
    for hid, evs in by_home.items():
        for d in (e for e in evs if e["type"] == "decision"):
            t, pl = _at(d), d["payload"]

            def within(kinds: set[str], hours: int, recipe_match: bool = False):
                for e in evs:
                    if e["type"] in kinds and t < _at(e) <= t + timedelta(hours=hours):
                        if not recipe_match or e["payload"].get("recipe") == pl.get("recipe"):
                            return e
                return None

            rated = within({"dish_rated"}, 48, True)
            row = {"household_id": hid, "at": d["at"], "policy": pl.get("policy"), "recipe": pl.get("recipe"),
                   "cuisine": pl.get("cuisine"), "novelty": pl.get("novelty"), "rescues": pl.get("rescues"),
                   "hour": pl.get("hour"), "weekday": pl.get("weekday"), "variety": pl.get("variety"),
                   "bar": pl.get("bar"), "on_hand": pl.get("on_hand"), "at_risk": pl.get("at_risk"),
                   "cadence": pl.get("cadence"), "main_meal": pl.get("main_meal"),
                   "taste_for_cuisine": (pl.get("taste") or {}).get(pl.get("cuisine"), 0.0),
                   "taste_json": json.dumps(pl.get("taste") or {}),
                   "locked": int(bool(within({"locked"}, 6))), "swapped": int(bool(within({"pick_swapped"}, 6))),
                   "made": int(bool(within({"made"}, 24))),
                   "rating": rated["payload"].get("rating") if rated else "",
                   "replied": int(bool(within({"inbound"}, 3)))}
            out.append(row)
    return out


def main(argv: list[str]) -> None:
    if len(argv) != 2:
        raise SystemExit("usage: python -m fridgebud.dataset <db> <out.csv>")
    data = rows(Store(argv[0]))
    with open(argv[1], "w", newline="") as f:
        if data:
            w = csv.DictWriter(f, fieldnames=list(data[0]))
            w.writeheader()
            w.writerows(data)
    print(f"{len(data)} picks written to {argv[1]}")


if __name__ == "__main__":
    main(sys.argv[1:])
