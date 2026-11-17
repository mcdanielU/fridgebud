"""Bud plans around how each household eats: a voice note (or TASTE ...) builds a food persona."""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fridgebud import messages
from fridgebud.capture import CapturedItem
from fridgebud.engine import Engine
from fridgebud.server import App
from fridgebud.service import Config, Service
from fridgebud.store import Store
from fridgebud.telegram import parse_update
from fridgebud.video import GeminiWatcher

TZ = ZoneInfo("America/Toronto")
A = "+15195550101"
MON = datetime(2026, 11, 16, 9, 0, tzinfo=TZ)
FOOD = "bought baby spinach, chicken thighs, milk"
LUNCH_LOVER = {"transcript": "I love jollof and suya, spicy food, no mushrooms. Big lunch around 1, dinner is light.",
               "about_me": True, "likes": ["jollof", "suya", "spicy food"], "cuisines": ["Nigerian"],
               "avoid": ["mushrooms"], "diet": "", "meals": {"lunch": "13:00", "dinner": "19:30"}, "main_meal": "lunch"}


def at(day, hour=16, minute=30):
    return (MON + timedelta(days=day)).replace(hour=hour, minute=minute)


def gemini(reply: dict):
    seen = []

    def transport(url, headers, body):
        seen.append(json.loads(body))
        return {"candidates": [{"content": {"parts": [{"text": json.dumps(reply)}]}}]}
    return GeminiWatcher(api_key="k", transport=transport), seen


def make(watcher=None):
    svc = Service(Engine(Store()), Config(number="+12265550000"), watcher=watcher)
    for text in ("hi", "yes", "2", FOOD):
        svc.handle_message(A, text, MON)
    return svc


def test_persona_is_merged_summarised_and_moves_the_daily_text():
    svc = make()
    h = svc.store.get_household_by_phone(A)
    out = svc.set_persona(h, LUNCH_LOVER, at(0, 10))[0]
    assert out.startswith("Got it 😋 Your food persona:")
    assert "• Loves: jollof, suya, spicy food" in out and "• Cuisines: Nigerian" in out and "• Skips: mushrooms" in out
    assert "• Eats: lunch 1pm, dinner 7:30pm" in out and "I'll send meal ideas around 11:00 AM" in out
    h = svc.store.get_household_by_phone(A)
    assert (h.nudge_hour, h.nudge_minute) == (11, 0) and "mushrooms" in h.dislikes
    assert h.persona["main_meal"] == "lunch" and h.persona["meals"]["lunch"] == "13:00"
    svc.set_persona(h, {"likes": ["Jollof", "plantain"], "about_me": True}, at(0, 11))   # adds, no duplicates
    assert svc.store.get_household_by_phone(A).persona["likes"] == ["jollof", "suya", "spicy food", "plantain"]


def test_lunch_people_get_lunch_ideas_before_lunch():
    svc = make()
    svc.set_persona(svc.store.get_household_by_phone(A), LUNCH_LOVER, at(0, 10))
    sent = [t for p, t in svc.tick(at(0, 11, 0)) if p == A]
    assert sent and "For lunch: " in sent[0] and "Tonight: " not in sent[0]
    assert not [t for p, t in svc.tick(at(0, 16, 30)) if p == A and t.startswith("Use soon")]   # not again at 4:30


def test_breakfast_and_unknown_times_keep_the_usual_time():
    svc = make()
    svc.set_persona(svc.store.get_household_by_phone(A),
                    {"about_me": True, "likes": ["pancakes"], "meals": {"breakfast": "07:30"}, "main_meal": "breakfast"}, at(0, 9))
    h = svc.store.get_household_by_phone(A)
    assert (h.nudge_hour, h.nudge_minute) == (16, 30)                  # a 5:30 AM text would help nobody
    sent = [t for p, t in svc.tick(at(0)) if p == A]
    assert "Tonight: " in sent[0]                                        # never "breakfast: stir-fry"


def test_empty_voice_note_says_so():
    svc = make()
    assert svc.set_persona(svc.store.get_household_by_phone(A), {"about_me": True, "transcript": "um"}, at(0, 9)) == [messages.TASTE_EMPTY]


class VoiceSender:
    def __init__(self):
        self.sent = []

    def send(self, to, body):
        self.sent.append(body)

    def fetch_media(self, ref, max_bytes=None):
        return b"OggS-voice"


def test_a_voice_note_about_taste_builds_the_persona():
    watcher, seen = gemini(LUNCH_LOVER)
    svc = make(watcher)
    sender = VoiceSender()
    App(svc, sender, clock=lambda: at(0, 10)).inbound(A, "", [("v1", "audio/ogg")])
    assert sender.sent[-1].startswith("Got it 😋 Your food persona:")
    part = seen[0]["contents"][0]["parts"][0]["inline_data"]
    assert part["mime_type"] == "audio/ogg"                             # the audio itself went to Gemini


def test_a_spoken_request_is_answered_like_a_typed_one():
    watcher, _ = gemini({"transcript": "what's for dinner", "about_me": False})
    svc = make(watcher)
    sender = VoiceSender()
    App(svc, sender, clock=lambda: at(0, 17)).inbound(A, "", [("v1", "audio/ogg")])
    assert sender.sent[-1].startswith("Tonight: ")


def test_voice_notes_without_a_listener_say_how_to_type_it():
    svc = make()
    sender = VoiceSender()
    App(svc, sender, clock=lambda: at(0, 17)).inbound(A, "", [("v1", "audio/ogg")])
    assert sender.sent == [messages.VOICE_NEEDS_KEY]


def test_telegram_passes_voice_notes_through():
    update = {"message": {"chat": {"id": 5, "type": "private"}, "voice": {"file_id": "VN", "mime_type": "audio/ogg"}}}
    assert parse_update(update) == ("tg:5", "", [("VN", "audio/ogg")])


def test_typed_taste_works_with_or_without_a_listener():
    watcher, seen = gemini(LUNCH_LOVER)
    svc = make(watcher)
    out = svc.handle_message(A, "TASTE I love jollof, big lunch at 1", at(0, 10))
    assert out[0].startswith("Got it 😋") and "typed: I love jollof" in seen[0]["contents"][0]["parts"][0]["text"]
    assert svc.handle_message(A, "taste", at(0, 10))[0].startswith("📓 What I know about your kitchen")   # Bud knows them now
    plain = make()
    assert plain.handle_message(A, "about me: vegetarian, love Thai", at(0, 10))[0].startswith("Noted 📝")
    assert "vegetarian, love Thai" in plain.store.get_household_by_phone(A).diet_notes


def test_first_photo_asks_about_taste_and_later_photos_ask_about_age():
    svc = Service(Engine(Store()), Config(number="+12265550000"))
    for text in ("hi", "yes", "2"):
        svc.handle_message(A, text, MON)
    fridge = [CapturedItem(n, source_kind="fridge") for n in ("chicken thighs", "baby spinach", "milk")]
    first = svc.handle_message(A, "", MON, has_media=True, captured=fridge)
    assert first[-1] == messages.ASK_TASTE
    second = svc.handle_message(A, "", at(1, 12), has_media=True, captured=[CapturedItem("eggs", source_kind="fridge")])
    assert second[-1] == messages.FRIDGE_AGE and messages.ASK_TASTE not in second


def test_homes_from_before_get_one_taste_ps_on_their_daily_text():
    svc = make()
    svc.store.db.execute("DELETE FROM events WHERE type = 'persona_asked'")   # joined before Bud asked
    first = [t for p, t in svc.tick(at(0)) if p == A]
    assert first and messages.TASTE_PS in first[0]
    assert not any(messages.TASTE_PS in t for p, t in svc.tick(at(1)) if p == A)


def test_recipes_and_the_assistant_know_their_taste():
    svc = make()
    svc.set_persona(svc.store.get_household_by_phone(A), LUNCH_LOVER, at(0, 10))
    taste = svc._taste(svc.store.get_household_by_phone(A))
    assert "loves jollof, suya, spicy food" in taste and "favourite cuisines Nigerian" in taste and "main meal is lunch" in taste


def test_the_check_in_follows_their_main_meal_and_names_it():
    svc = make()
    svc.set_persona(svc.store.get_household_by_phone(A), LUNCH_LOVER, at(0, 9))      # lunch at 1, idea at 11
    assert [t for p, t in svc.tick(at(0, 11, 0)) if p == A]
    assert not [t for p, t in svc.tick(at(0, 14, 0)) if p == A]                    # still eating
    after = [t for p, t in svc.tick(at(0, 14, 30)) if p == A]                      # lunch + 90 min
    assert len(after) == 1 and after[0].startswith("How was lunch?")


def test_late_dinners_still_get_asked_before_quiet_hours():
    svc = make()
    svc.set_persona(svc.store.get_household_by_phone(A), {"about_me": True, "meals": {"dinner": "19:30"}, "main_meal": "dinner"}, at(0, 9))
    assert [t for p, t in svc.tick(at(0, 17, 30)) if p == A]                        # idea two hours before
    assert not [t for p, t in svc.tick(at(0, 20, 0)) if p == A]                     # they're eating
    late = [t for p, t in svc.tick(at(0, 20, 30)) if p == A]                         # capped at 8:30, not 9
    assert len(late) == 1 and late[0].startswith("How was dinner?")


# every idea fits the meal it's for --------------------------------------------
from fridgebud import recipes
from test_kitchen import FakeChef

BY_NAME = {r.name: r for r in recipes.BANK}


def _idea_name(text, label):
    line = next(l for l in text.splitlines() if l.startswith(label + ": "))
    return line[len(label) + 2:].split(",")[0]


def fridge_with(*food):
    svc = Service(Engine(Store()), Config(number="+12265550000"), chef=FakeChef())
    for text in ("hi", "yes", "2", "bought " + ", ".join(food)):
        svc.handle_message(A, text, MON)
    return svc


def test_every_dish_says_which_meals_it_suits():
    assert all(r.meals and r.meals <= set(recipes.MEALS) for r in recipes.BANK)
    for meal in recipes.MEALS:
        assert sum(meal in r.meals for r in recipes.BANK) >= 5, meal


@pytest.mark.parametrize("ask, label, meal", [("breakfast", "Breakfast", "breakfast"), ("brunch ideas", "Brunch", "brunch"),
                                              ("what's for lunch?", "For lunch", "lunch")])
def test_asking_for_a_meal_gets_a_dish_for_that_meal(ask, label, meal):
    svc = fridge_with("eggs", "bread", "baby spinach", "chicken thighs", "tomatoes", "leaf lettuce")
    reply = svc.handle_message(A, ask, at(0, 9))[0]
    assert meal in BY_NAME[_idea_name(reply, label)].meals
    assert "Reply 👍 to cook this" in reply and "Reply 👎 for a different idea" in reply


def test_snacks_are_snacks():
    svc = fridge_with("hummus", "carrots", "chicken thighs")
    reply = svc.handle_message(A, "something to snack on", at(0, 15))[0]
    assert reply.startswith("Snack: Hummus and veg sticks") and "Reply 👍 to cook this" in reply


def test_dinner_only_dishes_never_turn_up_for_breakfast():
    svc = fridge_with("chicken thighs", "broccoli", "peppers")         # sheet-pan dinner territory, nothing breakfasty
    out = svc.handle_message(A, "breakfast", at(0, 8))
    assert out[0].startswith("Nothing in your fridge fits breakfast yet")
    assert ("generate", "a breakfast idea") in svc.chef.calls          # written for breakfast instead


def test_a_lunch_persons_daily_idea_is_a_real_lunch_dish():
    svc = fridge_with("baby spinach", "chicken thighs", "leaf lettuce", "tomatoes", "bread", "deli meat")
    svc.set_persona(svc.store.get_household_by_phone(A), LUNCH_LOVER, at(0, 9))
    text = next(t for p, t in svc.tick(at(0, 11, 0)) if p == A)
    assert "lunch" in BY_NAME[_idea_name(text, "For lunch")].meals and "Tonight: " not in text


def test_brunch_is_part_of_the_persona():
    svc = make()
    out = svc.set_persona(svc.store.get_household_by_phone(A),
                          {"about_me": True, "meals": {"brunch": "11:00"}, "main_meal": "brunch"}, at(0, 9))[0]
    assert "• Eats: brunch 11am" in out and svc.store.get_household_by_phone(A).persona["main_meal"] == "brunch"
