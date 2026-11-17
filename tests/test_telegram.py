from datetime import datetime
from zoneinfo import ZoneInfo

from fridgebud import messages
from fridgebud.capture import CapturedItem, FixedExtractor
from fridgebud.engine import Engine
from fridgebud.server import App
from fridgebud.service import Config, Service
from fridgebud.store import Store
from fridgebud.telegram import TelegramSender, handle_update, load_dotenv, parse_update

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=ZoneInfo("America/Toronto"))
CHAT = 4242


class FakeTelegram:
    def __init__(self):
        self.calls, self.photos = [], {"big": b"jpeg-bytes"}

    def call(self, method, params, timeout=20):
        self.calls.append((method, params))
        if method == "getFile":
            return {"file_path": f"photos/{params['file_id']}.jpg"}
        return {}

    def download(self, file_path):
        return self.photos[file_path.split("/")[-1].removesuffix(".jpg")]

    def texts(self):
        return [p["text"] for m, p in self.calls if m == "sendMessage"]


def make_app(extractor=None):
    tg = FakeTelegram()
    service = Service(Engine(Store()), Config(number="@FridgeBudBot on Telegram"))
    return App(service, TelegramSender(call=tg.call, download=tg.download), extractor, clock=lambda: NOW), tg


def msg(text=None, **extra):
    body = {"chat": {"id": CHAT, "type": "private"}, **extra}
    if text is not None:
        body["text"] = text
    return {"update_id": 1, "message": body}


def test_start_button_runs_the_same_onboarding():
    app, tg = make_app()
    handle_update(app, msg("/start"))
    assert tg.texts() == [messages.WELCOME]
    handle_update(app, msg("YES"))
    handle_update(app, msg("2"))
    handle_update(app, msg("bought ground beef, tortillas"))
    assert tg.texts()[1] == messages.ASK_SIZE
    assert "JOIN" in tg.texts()[2] and "@FridgeBudBot on Telegram" in tg.texts()[2]
    assert "Got it: ground beef, tortillas" in tg.texts()[-3] and tg.texts()[-1] == messages.ASK_TASTE
    assert all(p["chat_id"] == str(CHAT) for m, p in tg.calls if m == "sendMessage")
    assert app.service.store.get_household_by_phone(f"tg:{CHAT}").timezone == "America/Toronto"


def test_fridge_photo_is_downloaded_and_read():
    found = [CapturedItem("baby spinach"), CapturedItem("chicken thighs"), CapturedItem("oat milk", confidence=0.2)]
    app, tg = make_app(FixedExtractor(found))
    for text in ("/start", "yes", "1"):
        handle_update(app, msg(text))
    handle_update(app, msg(photo=[{"file_id": "small"}, {"file_id": "big"}], caption=""))
    methods = [m for m, _ in tg.calls]
    assert "sendChatAction" in methods and ("getFile", {"file_id": "big"}) in tg.calls   # largest size
    reply = tg.texts()[-3]
    assert tg.texts()[-2].startswith("Use first:") and tg.texts()[-1] == messages.ASK_TASTE
    assert "baby spinach" in reply and "chicken thighs" in reply and '"oat milk"' in reply
    assert {i.key for i in app.service.store.active_items(1)} == {"spinach", "chicken"}


def test_photo_without_api_key_gets_a_kind_reply():
    app, tg = make_app(extractor=None)
    for text in ("/start", "yes", "1"):
        handle_update(app, msg(text))
    handle_update(app, msg(photo=[{"file_id": "big"}]))
    assert tg.texts()[-1] == messages.PHOTO_UNREADABLE


def test_daily_nudge_goes_to_the_chat():
    app, tg = make_app()
    for text in ("/start", "yes", "1", "bought ground beef, tortillas"):
        handle_update(app, msg(text))
    app.clock = lambda: NOW.replace(hour=16, minute=30)
    assert app.tick() == 1
    assert tg.texts()[-1].startswith("Use soon: ground beef")


def test_groups_stickers_and_edits_are_ignored():
    assert parse_update({"update_id": 1, "edited_message": {"chat": {"id": 1, "type": "private"}, "text": "x"}}) is None
    assert parse_update({"update_id": 1, "message": {"chat": {"id": 1, "type": "group"}, "text": "x"}}) is None
    assert parse_update({"update_id": 1, "message": {"chat": {"id": 1, "type": "private"}, "sticker": {}}}) is None
    assert parse_update(msg(document={"file_id": "d", "mime_type": "image/png", "file_size": 1000})) == (
        f"tg:{CHAT}", "", [("d", "image/png")])


def test_dotenv_loader(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nFB_TEST_TOKEN="abc:123"\nFB_TEST_KEEP=new\n')
    monkeypatch.setenv("FB_TEST_KEEP", "old")
    monkeypatch.delenv("FB_TEST_TOKEN", raising=False)
    load_dotenv(str(env))
    import os
    assert os.environ["FB_TEST_TOKEN"] == "abc:123" and os.environ["FB_TEST_KEEP"] == "old"
    monkeypatch.delenv("FB_TEST_TOKEN")
