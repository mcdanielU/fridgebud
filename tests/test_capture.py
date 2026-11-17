import base64
import json
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fridgebud.capture import ClaudeVisionExtractor, CapturedItem, FixedExtractor, parse_reply
from fridgebud.engine import Engine
from fridgebud.store import Store

TZ = ZoneInfo("America/Toronto")
PHONE = "+15195550100"
MON = datetime(2026, 11, 16, 9, 0, tzinfo=TZ)

RECEIPT_REPLY = {
    "kind": "receipt", "store": "No Frills",
    "items": [
        {"name": "baby spinach", "quantity": "1", "price": 3.99, "confidence": 0.95},
        {"name": "chicken thighs", "quantity": "1 kg", "price": 11.47, "confidence": 0.9},
        {"name": "strawberries", "quantity": None, "price": 4.99, "confidence": 0.85},
        {"name": "PC MLTGRN SNK", "quantity": None, "price": 3.49, "confidence": 0.3},
    ],
}


def fake_transport(reply_obj, seen):
    def transport(url, headers, body):
        seen.update(url=url, headers=headers, body=json.loads(body))
        return {"content": [{"type": "text", "text": "Here you go:\n" + json.dumps(reply_obj)}]}
    return transport


def signed_up(engine):
    engine.handle_inbound(PHONE, "hi", MON)
    engine.handle_inbound(PHONE, "yes", MON)
    return engine.store.get_household_by_phone(PHONE)


def test_request_is_well_formed():
    seen = {}
    ex = ClaudeVisionExtractor(api_key="test-key", model="test-model", transport=fake_transport(RECEIPT_REPLY, seen))
    items = ex.extract(b"\xff\xd8jpeg-bytes", "image/jpeg")
    assert seen["url"].endswith("/v1/messages")
    assert seen["headers"]["x-api-key"] == "test-key"
    assert seen["headers"]["anthropic-version"]
    content = seen["body"]["messages"][0]["content"]
    assert content[0]["source"]["data"] == base64.b64encode(b"\xff\xd8jpeg-bytes").decode()
    assert content[0]["source"]["media_type"] == "image/jpeg"
    assert seen["body"]["model"] == "test-model"
    assert [i.name for i in items][:2] == ["baby spinach", "chicken thighs"]


def test_parse_reply_is_defensive():
    assert parse_reply("no json here") == []
    assert parse_reply("{broken json") == []
    items = parse_reply(json.dumps({"items": [
        {"name": "milk", "price": "4.29", "confidence": 2},     # string price, out-of-range confidence
        {"name": "", "price": 1},                                # empty name dropped
        {"name": "eggs", "price": 9999, "confidence": 0.9},      # absurd price rejected
        {"name": "bread", "price": "n/a"},                       # unparseable price
    ]}))
    assert [(i.name, i.price, i.confidence) for i in items] == [
        ("milk", 4.29, 1.0), ("eggs", None, 0.9), ("bread", None, 0.5)]


def test_missing_key_fails_clearly(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    try:
        ClaudeVisionExtractor()
    except RuntimeError as err:
        assert "ANTHROPIC_API_KEY" in str(err)
    else:
        raise AssertionError("expected RuntimeError")


def test_receipt_photo_stores_prices_and_skips_unsure_items():
    seen = {}
    engine = Engine(Store(), extractor=ClaudeVisionExtractor(api_key="k", transport=fake_transport(RECEIPT_REPLY, seen)))
    h = signed_up(engine)
    reply = engine.handle_inbound(PHONE, "", MON, media=[b"img"], media_types=["image/jpeg"])[0]
    assert "baby spinach" in reply and "Saved your prices" in reply
    assert "PC MLTGRN SNK" in reply  # asked about, not silently added
    items = {i.name: i for i in engine.store.active_items(h.id)}
    assert items["baby spinach"].price == 3.99
    assert "PC MLTGRN SNK" not in items and "pc mltgrn snk" not in items
    assert engine.store.events(type_="capture_unsure")


def test_weekly_receipt_counts_real_dollars_kept():
    engine = Engine(Store(), extractor=FixedExtractor([
        CapturedItem("baby spinach", price=3.99), CapturedItem("strawberries", price=4.99),
        CapturedItem("chicken thighs", price=11.47)]))
    h = signed_up(engine)
    engine.handle_inbound(PHONE, "", MON, media=[b"img"])
    engine.handle_inbound(PHONE, "used spinach", MON + timedelta(days=2))
    engine.handle_inbound(PHONE, "used chicken", MON + timedelta(days=2))
    engine.handle_inbound(PHONE, "tossed strawberries", MON + timedelta(days=4))
    text = engine.weekly_receipt(h, (MON + timedelta(days=6)).date())
    assert "$15.46 of food kept" in text           # 3.99 + 11.47, tossed berries not counted
    assert "1 tossed" in text


def test_extractor_failure_never_breaks_the_chat():
    class Boom:
        def extract(self, image, media_type="image/jpeg"):
            raise RuntimeError("network down")
    engine = Engine(Store(), extractor=Boom())
    h = signed_up(engine)
    reply = engine.handle_inbound(PHONE, "", MON, media=[b"img"])[0]
    assert "couldn't make out" in reply
    assert engine.store.events(h.id, "capture_error")


def test_old_database_gets_price_column(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript("""CREATE TABLE items (id INTEGER PRIMARY KEY, household_id INTEGER NOT NULL, name TEXT NOT NULL,
        key TEXT, category TEXT, kind TEXT NOT NULL, added_on TEXT NOT NULL, portions INTEGER,
        status TEXT NOT NULL DEFAULT 'active', closed_on TEXT, confirmed_good_on TEXT);""")
    db.close()
    store = Store(str(path))
    cols = {r["name"] for r in store.db.execute("PRAGMA table_info(items)")}
    assert "price" in cols
