"""Free-form replies: turn "we ended up eating the chicken last night" into USED chicken.

The rule-based parser handles one- and two-word replies. Anything it can't
read used to get the help message. This asks a small Claude model to rewrite
the text as ONE of the commands the parser already understands, so the engine
and its safety rules stay exactly the same. If the model isn't confident, the
household gets the help message as before.

It can never opt someone in (only a literal YES does that), and it never
answers food-safety questions.
"""
from __future__ import annotations

import json
import os
import re
from typing import Protocol

from .capture import API_URL, Transport, _http_post
from .commands import service_command
from .parser import parse

DEFAULT_MODEL = "claude-haiku-5-5"
MIN_CONFIDENCE = 0.7
ALLOWED = {"made", "used", "tossed", "good", "leftover", "add", "list", "help", "stop"}

PROMPT = """You route text messages for FridgeBud, a service that texts a household about food that is about to go to waste.
Rewrite the household's message as exactly ONE command, or NONE.

Commands:
  MADE                      they cooked the dinner that was suggested
  USED <items>              they ate or used up specific items
  TOSSED <items>            they threw specific items out
  GOOD <items>              they checked and the items are still fine
  BOUGHT <items>            they bought or have new food
  LEFTOVERS <dish> x<N>     they have N portions of a cooked dish left (use x2 if no number is given)
  LIST                      they want to see what is at risk
  DINNER                    they want an idea for what to cook or eat now
  HAVE <item>               they ask whether they have a food, or how much is left
  ALL                       they want to see everything on their list
  OLD <items>               they say items have already been in the fridge for a few days
  STOP                      they want the texts to stop
  HELP                      they are asking how this works
  NONE                      anything else

Rules:
- Separate several items with commas. Prefer the names from "On their list" when the message clearly means one of them.
- If they are asking whether food is safe or OK to eat, answer NONE. Never judge food safety.
- If the message mixes actions, pick the one about food they used or tossed.
- If you are not sure, answer NONE with low confidence.

On their list: {items}
Last dinner suggested: {recipe}
Message: {text}

Reply with JSON only: {{"command": "...", "confidence": 0.0 to 1.0}}"""


class ReplyInterpreter(Protocol):
    def interpret(self, text: str, item_names: list[str], last_recipe: str | None) -> str | None:
        ...


def validate(command: str, confidence: float) -> str | None:
    """Accept a command only if the parser reads it as an allowed intent."""
    command = (command or "").strip()
    if not command or command.upper() == "NONE" or confidence < MIN_CONFIDENCE:
        return None
    if service_command(command):
        return command
    return command if parse(command).kind in ALLOWED else None


class ClaudeInterpreter:
    def __init__(self, api_key: str | None = None, model: str | None = None, transport: Transport | None = None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or os.environ.get("FRIDGEBUD_REPLY_MODEL", DEFAULT_MODEL)
        self.transport = transport or (lambda url, headers, body: _http_post(url, headers, body, timeout=20))
        if not self.api_key and transport is None:
            raise RuntimeError("Set ANTHROPIC_API_KEY to use the free-form reply fallback.")

    def interpret(self, text: str, item_names: list[str], last_recipe: str | None) -> str | None:
        prompt = PROMPT.format(items=", ".join(item_names[:30]) or "(empty)", recipe=last_recipe or "(none)",
                               text=text.strip()[:500])
        body = {"model": self.model, "max_tokens": 100, "messages": [{"role": "user", "content": prompt}]}
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        reply = self.transport(API_URL, headers, json.dumps(body).encode())
        out = "".join(b.get("text", "") for b in reply.get("content", []) if b.get("type") == "text")
        match = re.search(r"\{.*\}", out, re.S)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
            return validate(str(data.get("command", "")), float(data.get("confidence", 0)))
        except (ValueError, TypeError):
            return None
