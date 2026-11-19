"""Runs the real HTTP server on a local port and talks to it like a browser and like Twilio."""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from zoneinfo import ZoneInfo

import pytest

from fridgebud import messages
from fridgebud.engine import Engine
from fridgebud.server import App, make_handler
from fridgebud.service import Config, Service
from fridgebud.sms import MemorySender, signature
from fridgebud.store import Store

PHONE = "+15195550101"
NOW = datetime(2026, 11, 16, 9, 0, tzinfo=ZoneInfo("America/Toronto"))


@pytest.fixture
def site():
    sender = MemorySender()
    service = Service(Engine(Store()), Config(number="+12265550000", price="$6.99 a month"))
    app = App(service, sender, public_url="https://bud.example", auth_token="secret", admin_key="k3y",
              clock=lambda: NOW)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", sender, app
    server.shutdown()
    server.server_close()


def call(url, form=None, headers=None):
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()


def text_in(base, sender, body, phone=PHONE, signed=True):
    form = {"From": phone, "Body": body, "NumMedia": "0"}
    sig = signature("secret", "https://bud.example/sms", form) if signed else "nope"
    before = len(sender.sent)
    status, _ = call(base + "/sms", form, {"X-Twilio-Signature": sig})
    for _ in range(100):  # replies are sent from a worker thread
        if len(sender.sent) > before or status != 200:
            break
        time.sleep(0.02)
    return status


def test_landing_shows_price_before_the_number_field(site):
    base, _, _ = site
    status, html = call(base + "/")
    assert status == 200
    assert html.index("$6.99 a month") < html.index('name="phone"')
    assert 'type="checkbox" name="consent"' in html and "checked" not in html
    assert "Reply STOP" in html or "reply STOP" in html


def test_signup_needs_the_consent_box(site):
    base, sender, _ = site
    status, html = call(base + "/signup", {"phone": "519 555 0101"})
    assert status == 400 and "tick the box" in html and sender.sent == []


def test_signup_sends_welcome_then_full_flow_over_sms(site):
    base, sender, app = site
    status, html = call(base + "/signup", {"phone": "519 555 0101", "consent": "yes"})
    assert status == 200 and "Reply YES" in html
    assert sender.sent == [(PHONE, messages.WELCOME)]

    assert text_in(base, sender, "YES") == 200
    assert sender.sent[-1] == (PHONE, messages.ASK_SIZE)
    text_in(base, sender, "2")
    text_in(base, sender, "bought ground beef, tortillas")
    assert "Got it: ground beef, tortillas" in sender.sent[-3][1]
    assert sender.sent[-2][1].startswith("Use first:\n• ground beef: by tomorrow")
    assert sender.sent[-1][1] == messages.ASK_TASTE                 # first food in: ask how they eat

    app.clock = lambda: NOW.replace(hour=16, minute=30)
    assert app.tick() == 1
    assert sender.sent[-1][1].startswith("🍽️ Tonight: ")
    assert app.tick() == 0


def test_forged_webhook_is_rejected(site):
    base, sender, _ = site
    assert text_in(base, sender, "YES", signed=False) == 403
    assert sender.sent == []


def test_admin_routes_need_the_key(site):
    base, _, _ = site
    assert call(base + "/admin/scorecard")[0] == 404
    assert call(base + "/admin/scorecard?key=wrong")[0] == 404
    status, body = call(base + "/admin/scorecard?key=k3y")
    assert status == 200 and json.loads(body)["signups"]["target"] == 150
    assert call(base + "/healthz") == (200, "ok")


def test_signup_rate_limit(site):
    base, sender, _ = site
    codes = [call(base + "/signup", {"phone": f"519 555 01{n:02d}", "consent": "yes"})[0] for n in range(10, 17)]
    assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]
    assert len(sender.sent) == 5
