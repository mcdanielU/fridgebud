"""The messaging channel: SMS and MMS through Twilio, standard library only.

Inbound: Twilio POSTs each text to /sms (see server.py). Outbound: one REST
call per text. `ConsoleSender` prints instead of sending, so the whole app
runs locally with no account.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
MAX_MEDIA_BYTES = 8 * 1024 * 1024

# Canadian area codes -> time zone, so the 4:30 text lands at 4:30 local time.
# Anything not listed (including US numbers) defaults to Eastern.
_ZONES = {
    "America/Vancouver": "236 250 257 604 672 778",
    "America/Edmonton": "368 403 587 780 825",
    "America/Regina": "306 474 639",
    "America/Winnipeg": "204 431 584",
    "America/Halifax": "428 506 782 902",
    "America/St_Johns": "709 879",
}
_AREA = {code: zone for zone, codes in _ZONES.items() for code in codes.split()}
DEFAULT_ZONE = "America/Toronto"


def normalize_phone(raw: str) -> str | None:
    """'(519) 555-0100' -> '+15195550100'. Canada/US numbers only; None if it isn't one."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01" or digits[3] in "01":
        return None
    return "+1" + digits


def timezone_for(phone: str) -> str:
    return _AREA.get(phone[2:5], DEFAULT_ZONE) if phone.startswith("+1") else DEFAULT_ZONE


class Sender(Protocol):
    def send(self, to: str, body: str) -> None:
        ...


class ConsoleSender:
    def send(self, to: str, body: str) -> None:
        print(f"\n--- text to {to} ---\n{body}\n---", flush=True)


class MemorySender:
    """For tests: keeps what would have been sent."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, to: str, body: str) -> None:
        self.sent.append((to, body))


class TwilioSender:
    def __init__(self, account_sid: str, auth_token: str, from_number: str, timeout: float = 15):
        self.sid, self.token, self.from_number, self.timeout = account_sid, auth_token, from_number, timeout

    def _auth(self) -> str:
        return "Basic " + base64.b64encode(f"{self.sid}:{self.token}".encode()).decode()

    def send(self, to: str, body: str) -> None:
        data = urllib.parse.urlencode({"To": to, "From": self.from_number, "Body": body}).encode()
        req = urllib.request.Request(API.format(sid=self.sid), data=data, method="POST")
        req.add_header("Authorization", self._auth())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                json.loads(resp.read())
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")[:300]
            raise RuntimeError(f"Twilio send failed ({err.code}): {detail}") from err

    def fetch_media(self, url: str) -> bytes:
        """Download a photo someone texted. Twilio media URLs need the account's credentials."""
        if not url.startswith("https://api.twilio.com/"):
            raise RuntimeError("Refusing to fetch media from a non-Twilio URL.")
        req = urllib.request.Request(url)
        # "Unredirected": the URL redirects to storage, which must not receive our credentials.
        req.add_unredirected_header("Authorization", self._auth())
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            blob = resp.read(MAX_MEDIA_BYTES + 1)
        if len(blob) > MAX_MEDIA_BYTES:
            raise RuntimeError("Photo is too large.")
        return blob


def signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Twilio's request signature: HMAC-SHA1 over the URL plus sorted form fields."""
    payload = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()).decode()


def valid_signature(auth_token: str, url: str, params: dict[str, str], given: str) -> bool:
    return hmac.compare_digest(signature(auth_token, url, params), given or "")
