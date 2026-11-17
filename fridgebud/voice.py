"""The Bud voice: a little extra character on a few plain acknowledgements.

Bud's personality now lives in messages.py itself (short, warm, at most one emoji). This layer only swaps a
handful of flat acknowledgements for livelier ones of about the same length. It never adds a line: people
read these in five seconds. Bud is dramatic about the food, never about the person, and never says a food
is safe. Consent, STOP and HELP wording is never changed.
"""
from __future__ import annotations

import re


def budify(text: str, seed: int = 0) -> str:
    """Give one plain FridgeBud text a bit of Bud. Anything not listed passes through unchanged."""
    m = re.match(r"^Nice, that rescued (.+?)\. (Any leftovers\?.*)$", text, re.S)
    if m:
        return f"🎉 Rescued: {m.group(1)}! {m.group(2)}"
    m = re.match(r"^Logged (.+) as tossed\. (.*)$", text, re.S)
    if m:
        return f"RIP {m.group(1)} 🪦 I'll flag that kind of thing sooner next time."
    m = re.match(r"^Logged (.+) as used\. Nice\.$", text)
    if m:
        return f"✅ {m.group(1)}: rescued. Nice work."
    m = re.match(r"^Good to know: (.+?)\. (.*)$", text, re.S)
    if m:
        return f"Phew, the {m.group(1)} lives on. I'll check back in a couple of days."
    if text.startswith(("Saved to your recipes.", "Added your next screenshot.")):
        return "📌 " + text
    return text
