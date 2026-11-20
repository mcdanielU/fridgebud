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
    assert tg.texts()[-1].startswith("🍽️ Tonight: ")


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


def test_tapping_a_thumbs_reaction_counts_as_replying_with_that_emoji():
    def react(emoji, chat_type="private"):
        return {"message_reaction": {"chat": {"id": 7, "type": chat_type}, "message_id": 3, "user": {"id": 7},
                                     "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": emoji}]}}
    assert parse_update(react("👍")) == ("tg:7", "👍", [])
    assert parse_update(react("👎")) == ("tg:7", "👎", [])
    assert parse_update(react("🔥")) == ("tg:7", "🔥", [])
    assert parse_update(react("🤡")) is None                      # reactions Bud doesn't use are ignored
    assert parse_update(react("👍", chat_type="supergroup")) is None
    removed = {"message_reaction": {"chat": {"id": 7, "type": "private"}, "message_id": 3,
                                    "old_reaction": [{"type": "emoji", "emoji": "👍"}], "new_reaction": []}}
    assert parse_update(removed) is None                           # taking a reaction back does nothing


class _Svc:
    def __init__(self, current):
        self.current, self.focused = current, []

    def is_current_pick(self, who, text):
        return self.current in text

    def focus_recipe(self, who, title):
        self.focused.append(title)
        return True


class _App:
    def __init__(self, current="Tacos"):
        self.sender = TelegramSender(call=lambda *a, **k: {})
        self.service = _Svc(current)
        self.sent = []

    def send(self, who, text):
        self.sent.append(text)


def test_a_reaction_means_the_message_it_is_tapped_on():
    from fridgebud.telegram import OLDER_IDEA, reaction_applies
    app = _App(current="Tacos")
    pick_now = "Tonight: Tacos, 20 min (uses chicken)\n\n👍 cook it\n👎 another idea"
    pick_old = "Tonight: Thai basil stir-fry, 15 min (uses shrimp)\n\n👍 cook it\n👎 another idea"
    card = "📖 Jollof rice\nSteps: ..."
    chat = "I couldn't find that, try LIST."
    for mid, text in ((1, pick_now), (2, pick_old), (3, card), (4, chat)):
        app.sender._remember("tg:7", {"message_id": mid}, text)
    assert reaction_applies(app, "tg:7", 1) is None                # the idea on the table: go ahead
    assert reaction_applies(app, "tg:7", 2) == OLDER_IDEA          # an older idea: don't change today's pick
    assert reaction_applies(app, "tg:7", 3) is None and app.service.focused == ["Jollof rice"]   # rates that dish
    assert reaction_applies(app, "tg:7", 4) == ""                  # not an idea at all: ignore quietly
    assert reaction_applies(app, "tg:7", 99) is None               # sent before a restart: treat as current


def test_old_idea_reaction_gets_a_note_and_no_taste_change():
    app, tg = make_app()
    for text in ("/start", "yes", "1", "bought ground beef, tortillas"):
        handle_update(app, msg(text))
    app.sender._remember(f"tg:{CHAT}", {"message_id": 50}, "Tonight: Old dish, 10 min (uses beef)\n\n👍 cook it\n👎 another idea")
    before = len(tg.texts())
    handle_update(app, {"message_reaction": {"chat": {"id": CHAT, "type": "private"}, "message_id": 50,
                                             "new_reaction": [{"type": "emoji", "emoji": "👎"}]}})
    assert tg.texts()[before:] == ["That one's from earlier 🙂 Tap 👍 or 👎 on my newest idea and I'll use it."]


class _NoAgent:
    def run(self, *a, **k):
        raise AssertionError("a reaction must never reach the chat agent")


def test_thumbs_up_on_an_on_demand_idea_locks_it_and_counts_once():
    app, tg = make_app()
    for text in ("/start", "yes", "1", "bought ground beef, tortillas, cooked shrimp"):
        handle_update(app, msg(text))
    handle_update(app, msg("lunch"))
    idea = tg.texts()[-1]
    title = idea.split("\n")[0].split(": ", 1)[1]
    app.sender._remember(f"tg:{CHAT}", {"message_id": 60}, idea)
    app.service.agent = _NoAgent()
    tap = {"message_reaction": {"chat": {"id": CHAT, "type": "private"}, "message_id": 60,
                                "new_reaction": [{"type": "emoji", "emoji": "👍"}]}}
    before = len(tg.texts())
    handle_update(app, tap)
    assert len(tg.texts()) == before + 1 and tg.texts()[-1].startswith(f"Nice, {title} it is 👍")
    handle_update(app, tap)                                        # took it off, tapped again: no repeat
    assert len(tg.texts()) == before + 1
    unknown = {"message_reaction": {"chat": {"id": CHAT, "type": "private"}, "message_id": 999,
                                    "new_reaction": [{"type": "emoji", "emoji": "👍"}]}}
    handle_update(app, unknown)                                    # a message Bud can't place: silence, not a guess
    assert len(tg.texts()) == before + 1


def test_a_stalled_telegram_read_is_a_telegram_error_not_a_crash(monkeypatch):
    """A read timeout is a raw TimeoutError, not a URLError. It used to escape the poll loop and stop Bud for good."""
    import urllib.request

    import pytest

    from fridgebud.telegram import TelegramError, _api_call

    def stalled(*args, **kwargs):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(urllib.request, "urlopen", stalled)
    with pytest.raises(TelegramError):
        _api_call("123:fake")("getUpdates", {}, 1)
