"""Text FridgeBud from your terminal, with a fake clock.

    python -m fridgebud.cli          # interactive
    python -m fridgebud.cli --demo   # scripted week for one household

Interactive commands:
    /next          jump to the next 4:30 PM nudge (runs the scheduler)
    /day N         move the clock forward N days (default 1)
    /photo a, b    pretend you sent a photo that contains a and b
    /realphoto P   send a real photo file P through Claude vision (needs ANTHROPIC_API_KEY)
    /receipt       show this week's rescue receipt
    /rates         show learned spoil rates for this household
    /quit
Anything else is sent as a text message.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .capture import ClaudeVisionExtractor, FixedExtractor, media_type_for
from .engine import Engine
from .store import Store

TZ = ZoneInfo("America/Toronto")
PHONE = "+15195550100"


class Sim:
    def __init__(self, db: str = ":memory:"):
        self.engine = Engine(Store(db))
        self.now = datetime(2026, 11, 16, 9, 0, tzinfo=TZ)

    def say(self, text: str, photo: list[str] | None = None) -> None:
        print(f"\n[{self.now:%a %b %d %H:%M}] you: {text if not photo else '(photo) ' + ', '.join(photo)}")
        if photo:
            self.engine.extractor = FixedExtractor(photo)
        for reply in self.engine.handle_inbound(PHONE, text, self.now, media=[b"img"] if photo else None):
            print(f"FridgeBud: {reply}")

    def real_photo(self, path: str) -> None:
        try:
            self.engine.extractor = ClaudeVisionExtractor()
        except RuntimeError as err:
            print(f"(can't read photos: {err})")
            return
        if self.engine.store.get_household_by_phone(PHONE) is None:  # sign up silently first
            self.engine.handle_inbound(PHONE, "hi", self.now)
            self.engine.handle_inbound(PHONE, "yes", self.now)
        with open(path, "rb") as fh:
            blob = fh.read()
        print(f"\n[{self.now:%a %b %d %H:%M}] you: (photo) {path}")
        for reply in self.engine.handle_inbound(PHONE, "", self.now, media=[blob], media_types=[media_type_for(path)]):
            print(f"FridgeBud: {reply}")
        h = self.engine.store.get_household_by_phone(PHONE)
        for item in self.engine.store.active_items(h.id):
            price = f"  ${item.price:.2f}" if item.price else ""
            print(f"  - {item.name} [{item.key or 'untracked'}]{price}")

    def next_nudge(self) -> None:
        target = self.now.replace(hour=16, minute=30, second=0)
        if target <= self.now:
            target += timedelta(days=1)
        self.now = target
        sent = self.engine.due_nudges(self.now)
        print(f"\n[{self.now:%a %b %d %H:%M}] scheduler ran")
        for _, text in sent:
            print(f"FridgeBud:\n{text}")
        if not sent:
            print("(no text today: nothing at risk)")

    def receipt(self) -> None:
        h = self.engine.store.get_household_by_phone(PHONE)
        print("FridgeBud: " + self.engine.weekly_receipt(h, self.now.date()))

    def rates(self) -> None:
        h = self.engine.store.get_household_by_phone(PHONE)
        rows = self.engine.store.db.execute(
            "SELECT category, multiplier, observations FROM spoil_rates WHERE household_id = ?", (h.id,))
        for r in rows:
            print(f"  {r['category']}: x{r['multiplier']} after {r['observations']} replies")


def demo() -> None:
    s = Sim()
    s.say("hi")
    s.say("yes")
    s.say("bought baby spinach, chicken thighs, 2 avocados, strawberries, milk, cheddar, tortillas and rice")
    s.next_nudge()                       # Mon: nothing at risk yet for most items
    s.say("/photo surprise bag", photo=["mushrooms", "bell peppers", "bread"])
    s.next_nudge()                       # Tue
    s.say("made it")
    s.say("leftovers chicken stir-fry x3")
    s.next_nudge()                       # Wed
    s.say("tossed strawberries")
    s.next_nudge()                       # Thu
    s.say("made")
    s.next_nudge()                       # Fri
    s.say("good milk")
    s.next_nudge()                       # Sat
    s.next_nudge()                       # Sun
    s.receipt()
    print("\nLearned spoil rates:")
    s.rates()


def interactive() -> None:
    s = Sim()
    print(__doc__)
    while True:
        try:
            line = input(f"[{s.now:%a %H:%M}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line == "/quit":
            break
        if line == "/next":
            s.next_nudge()
        elif line.startswith("/day"):
            parts = line.split()
            s.now += timedelta(days=int(parts[1]) if len(parts) > 1 else 1)
            print(f"(clock: {s.now:%a %b %d %H:%M})")
        elif line.startswith("/photo"):
            names = [n.strip() for n in line[len("/photo"):].split(",") if n.strip()]
            s.say("", photo=names)
        elif line.startswith("/realphoto"):
            s.real_photo(line[len("/realphoto"):].strip())
        elif line == "/receipt":
            s.receipt()
        elif line == "/rates":
            s.rates()
        else:
            s.say(line)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    demo() if args.demo else interactive()
