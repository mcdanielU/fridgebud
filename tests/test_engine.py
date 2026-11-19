from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fridgebud import recipes, watchlist
from fridgebud.capture import FixedExtractor
from fridgebud.engine import Engine
from fridgebud.parser import parse
from fridgebud.shelf_life import ShelfLifeTable, normalize
from fridgebud.store import Store

TZ = ZoneInfo("America/Toronto")
PHONE = "+15195550100"
MON = datetime(2026, 11, 16, 9, 0, tzinfo=TZ)


@pytest.fixture
def table():
    return ShelfLifeTable()


@pytest.fixture
def engine():
    return Engine(Store())


def signed_up(engine, items="baby spinach, chicken thighs, milk"):
    engine.handle_inbound(PHONE, "hi", MON)
    engine.handle_inbound(PHONE, "yes", MON)
    engine.handle_inbound(PHONE, f"bought {items}", MON)
    return engine.store.get_household_by_phone(PHONE)


def at(day_offset, hour=16, minute=30):
    return (MON + timedelta(days=day_offset)).replace(hour=hour, minute=minute)


# shelf-life lookup ------------------------------------------------------
@pytest.mark.parametrize("raw,key", [
    ("Organic Baby Spinach 312g", "spinach"),
    ("2 avocados", "avocado"),
    ("a bag of strawberries", "berries"),
    ("ground beef", "ground_meat"),      # longer alias beats "beef"
    ("Chicken Thighs x2", "chicken"),
    ("salmon", "fish"),
    ("tomatoes", "tomatoes"),
])
def test_lookup(table, raw, key):
    assert table.lookup(raw).key == key


def test_unknown_items_are_untracked(table):
    assert table.lookup("rice") is None
    assert table.lookup("") is None


def test_health_canada_times_used_for_meat(table):
    rule = table.lookup("ground beef")
    assert (rule.min_days, rule.source) == (1, "hc")


def test_normalize_strips_quantities():
    assert normalize("2 x Large Organic Eggs") == "eggs"


# parser -----------------------------------------------------------------
@pytest.mark.parametrize("text,kind,items,portions", [
    ("STOP", "stop", [], None),
    ("Yes", "start", [], None),
    ("made it", "made", [], None),
    ("tossed the spinach and milk", "tossed", ["spinach", "milk"], None),
    ("still good milk", "good", ["milk"], None),
    ("good", "good", [], None),
    ("ate the berries", "used", ["berries"], None),
    ("bought kale, eggs & 2 avocados", "add", ["kale", "eggs", "2 avocados"], None),
    ("leftovers chili x3", "leftover", ["chili"], 3),
    ("made chili, 3 portions", "leftover", ["chili"], 3),
    ("3 portions of curry left", "leftover", ["curry"], 3),
    ("left lasagna 4", "leftover", ["lasagna"], 4),
    ("what is this", "unknown", [], None),
])
def test_parse(text, kind, items, portions):
    intent = parse(text)
    assert (intent.kind, intent.items, intent.portions) == (kind, items, portions)


# consent ----------------------------------------------------------------
def test_no_texts_without_consent(engine):
    replies = engine.handle_inbound(PHONE, "hi", MON)
    assert "Reply YES" in replies[0]
    engine.handle_inbound(PHONE, "bought chicken", MON)  # ignored until YES
    assert engine.due_nudges(at(1)) == []


def test_stop_ends_nudges(engine):
    signed_up(engine)
    engine.handle_inbound(PHONE, "STOP", MON)
    assert engine.due_nudges(at(1)) == []
    assert engine.store.events(type_="opt_out")


# watchlist --------------------------------------------------------------
def test_watchlist_orders_by_urgency(engine):
    h = signed_up(engine, "ground beef, baby spinach, carrots")
    wl = engine.watchlist(h, MON.date())
    assert [e.item.key for e in wl] == ["ground_meat"]          # 1-2 days
    wl = engine.watchlist(h, MON.date() + timedelta(days=2))
    assert [e.item.key for e in wl][:2] == ["ground_meat", "spinach"]
    assert "carrots" not in [e.item.key for e in wl]             # 14 days


def test_past_window_items_are_never_cooked_with(engine):
    h = signed_up(engine, "chicken thighs, tortillas, cheddar")
    day4 = MON.date() + timedelta(days=4)                       # chicken window is 2 days
    wl = engine.watchlist(h, day4)
    chicken = next(e for e in wl if e.item.key == "chicken")
    assert chicken.past_window and chicken.label == "worth checking"
    pick = recipes.suggest(wl, engine.store.active_items(h.id), [])
    assert pick is None or all(i.key != "chicken" for i in pick.rescues)
    text = engine.compose_nudge(h, day4)
    assert "Still good? chicken thighs" in text
    assert "safe" not in text.lower()


def test_items_drop_off_after_three_days_past(engine):
    h = signed_up(engine, "chicken thighs")
    assert engine.watchlist(h, MON.date() + timedelta(days=6)) == []


# daily loop -------------------------------------------------------------
def test_one_nudge_per_day_at_430(engine):
    signed_up(engine)
    assert engine.due_nudges(at(0, 16, 29)) == []
    sent = engine.due_nudges(at(0, 16, 30))
    assert len(sent) == 1 and sent[0][0] == PHONE
    assert engine.due_nudges(at(0, 18, 0)) == []                 # not twice
    assert len(engine.due_nudges(at(1))) == 1


def test_quiet_day_still_gets_a_pick_if_the_fridge_can_make_one(engine):
    signed_up(engine, "ground beef, tortillas")
    engine.store.db.execute("UPDATE items SET added_on = ?", (at(0).date().isoformat(),))
    text = engine.due_nudges(at(0))[0][1]
    assert "Tacos" in text and "👍 cook it\n👎 another idea" in text


def test_no_text_when_nothing_at_risk_and_nothing_to_make(engine):
    signed_up(engine, "ketchup")
    assert engine.due_nudges(at(0)) == []
    assert engine.store.events(type_="nudge_skipped")


def test_made_closes_only_dinner_items(engine):
    h = signed_up(engine, "chicken thighs, tortillas, cheddar, strawberries, yogurt")
    engine.due_nudges(at(1))
    h = engine.store.get_household(h.id)
    dinner_ids = set(h.last_suggestion["item_ids"])
    assert dinner_ids
    engine.handle_inbound(PHONE, "made it", at(1, 19))
    open_ids = {i.id for i in engine.store.active_items(h.id)}
    assert not dinner_ids & open_ids
    assert set(h.last_suggestion["breakfast_ids"]) <= open_ids


def test_leftovers_take_priority(engine):
    h = signed_up(engine, "chicken thighs, tortillas, cheddar")
    engine.handle_inbound(PHONE, "leftovers chili x3", MON)
    text = engine.compose_nudge(h, MON.date() + timedelta(days=2))
    assert "Leftover night: finish the chili" in text


def test_photo_capture_uses_extractor():
    engine = Engine(Store(), extractor=FixedExtractor(["mushrooms", "bread"]))
    signed_up(engine, "milk")
    reply = engine.handle_inbound(PHONE, "", MON, media=[b"jpeg"])
    assert "mushrooms" in reply[0] and "bread" in reply[0]


def test_tossed_matches_food_family(engine):
    h = signed_up(engine, "organic baby spinach")
    reply = engine.handle_inbound(PHONE, "tossed spinach", at(3))
    assert "tossed" in reply[0]
    assert engine.store.active_items(h.id) == [i for i in engine.store.active_items(h.id) if i.key != "spinach"]


def test_bare_good_refers_to_last_check(engine):
    h = signed_up(engine, "milk")
    engine.compose_nudge(h, MON.date() + timedelta(days=4))       # milk past window
    reply = engine.handle_inbound(PHONE, "good", at(4, 17))
    assert "milk" in reply[0]


# learning (the moat) ----------------------------------------------------
def test_tossing_early_shortens_the_window(engine, table):
    h = signed_up(engine, "baby spinach")                          # window 4 days
    engine.handle_inbound(PHONE, "tossed spinach", at(2))
    mult, n = engine.store.get_rate(h.id, "greens")
    assert n == 1 and mult < 1.0
    engine.handle_inbound(PHONE, "bought baby spinach", at(3, 9))
    item = engine.store.active_items(h.id)[0]
    left, _ = watchlist.days_left(item, table.rules["spinach"], mult, item.added_on)
    assert left < 4                                                # flagged sooner next time


def test_still_good_past_window_lengthens_it(engine):
    h = signed_up(engine, "milk")                                  # window 3 days
    engine.handle_inbound(PHONE, "good milk", at(4))
    mult, _ = engine.store.get_rate(h.id, "dairy")
    assert mult > 1.0


def test_learning_is_bounded(engine):
    h = signed_up(engine, "baby spinach")
    for day in range(1, 30):
        engine.handle_inbound(PHONE, "bought baby spinach", at(day, 9))
        engine.handle_inbound(PHONE, "tossed spinach", at(day, 20))
    mult, _ = engine.store.get_rate(h.id, "greens")
    assert mult >= watchlist.MIN_MULT


def test_full_week_simulation_runs(engine):
    signed_up(engine, "baby spinach, chicken thighs, avocados, strawberries, milk, cheddar, tortillas")
    for day in range(7):
        for _, text in engine.due_nudges(at(day)):
            assert len(text) < 480                                 # about three SMS segments at most
        engine.handle_inbound(PHONE, "made it", at(day, 19))
    h = engine.store.get_household_by_phone(PHONE)
    assert "Rescue rate" in engine.weekly_receipt(h, at(6).date())
