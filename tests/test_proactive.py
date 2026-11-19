"""Bud starting conversations: its own time zone per home, the 8 PM check-in, photo chases, fresh-list asks."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fridgebud import messages, metrics, places
from fridgebud.engine import Engine
from fridgebud.service import Config, Service
from fridgebud.store import Store

TZ = ZoneInfo("America/Toronto")
A, B, C, T = "+15195550101", "+15195550102", "+15195550103", "tg:501"
MON = datetime(2026, 11, 16, 9, 0, tzinfo=TZ)
FOOD = "bought baby spinach, chicken thighs, milk"


def at(day, hour=16, minute=30):
    return (MON + timedelta(days=day)).replace(hour=hour, minute=minute)


@pytest.fixture
def svc():
    return Service(Engine(Store()), Config(number="+12265550000"))


def to(out, phone):
    return [text for p, text in out if p == phone]


def onboard(svc, phone, food=FOOD):
    for text in ("hi", "yes", "2"):
        last = svc.handle_message(phone, text, MON)
    if food:
        svc.handle_message(phone, food, MON)
    return last


def hh(svc, phone):
    return svc.store.get_household_by_phone(phone)


# where they are -----------------------------------------------------------
def test_places_reads_what_people_actually_type():
    assert places.parse("Vancouver", bare=True)[0] == "America/Vancouver"
    assert places.parse("I'm in Calgary")[0] == "America/Edmonton"
    assert places.parse("timezone London Ontario")[0] == "America/Toronto"
    assert places.parse("Kitchener, ON", bare=True)[0] == "America/Toronto"
    assert places.parse("I live in Lagos")[0] == "Africa/Lagos"
    assert places.parse("Vancouver") is None                    # a bare city only counts right after Bud asked
    assert places.parse("lima", bare=True) is None              # food words aren't cities unless said outright
    assert places.parse("chicken", bare=True) is None and places.parse("bought eggs", bare=True) is None


def test_telegram_signup_asks_the_city_and_the_daily_text_moves_to_their_430(svc):
    assert onboard(svc, T, food=None)[-1] == messages.ASK_PLACE
    got = svc.handle_message(T, "Vancouver", MON)
    assert got[0].startswith("Got it, Vancouver time. Your daily text comes at 4:30 PM there.")
    assert got[1] == messages.PHOTO_ASK                          # the step onboarding held back
    assert hh(svc, T).timezone == "America/Vancouver"
    svc.handle_message(T, FOOD, MON)
    assert not to(svc.tick(at(0)), T)                            # 4:30 Toronto is 1:30 in Vancouver
    assert to(svc.tick(at(0, 19, 30)), T)                        # 4:30 Vancouver


def test_sms_signup_is_unchanged_and_anyone_can_move_with_timezone(svc):
    assert onboard(svc, A, food=None)[-1] == messages.PHOTO_ASK  # area code already gives the zone
    assert svc.handle_message(A, "Vancouver", MON) != [] and hh(svc, A).timezone == "America/Toronto"  # not asked
    svc.handle_message(A, "timezone Calgary", MON)
    assert hh(svc, A).timezone == "America/Edmonton"


def test_telegram_home_that_skipped_the_question_is_asked_once_on_its_430_text(svc):
    for text in ("hi", "yes", FOOD):                             # skipped the size question, so never asked
        svc.handle_message(T, text, MON)
    first = to(svc.tick(at(0)), T)
    assert first and messages.PLACE_PS in first[0]
    assert not any(messages.PLACE_PS in t for t in to(svc.tick(at(1)), T))   # once only
    svc.handle_message(T, "Halifax", at(1, 18))
    assert hh(svc, T).timezone == "America/Halifax"


# the 8 PM check-in ----------------------------------------------------------
def test_evening_checkin_asks_once_after_an_unanswered_430_text(svc):
    onboard(svc, A)
    onboard(svc, B)
    assert to(svc.tick(at(0)), A) and to(svc.tick(at(0)), B) == []  # both got the 4:30 text in the first tick
    svc.handle_message(B, "made", at(0, 18))                      # B already said what happened
    eight = svc.tick(at(0, 20, 0))
    assert len(to(eight, A)) == 1 and to(eight, A)[0].startswith("How was dinner?")
    assert to(eight, B) == []
    assert to(svc.tick(at(0, 20, 30)), A) == []                   # once a day
    assert to(svc.tick(at(1, 21, 5)), A) == []                    # server back at 9:05 PM: no late 4:30 text...
    assert to(svc.tick(at(2)), A)                                 # ...and the next day carries on as normal


def test_off_duty_tonight_means_no_how_was_dinner(svc):
    onboard(svc, A)
    assert to(svc.tick(at(0)), A)                                 # the 4:30 pick went out
    reply = svc.handle_message(A, "Skip recipe for tonight", at(0, 18, 42))
    assert reply[0].startswith("Night off")
    assert to(svc.tick(at(0, 20, 0)), A) == []                    # no "How was dinner?" at 8 PM
    assert to(svc.tick(at(1)), A)                                 # tomorrow carries on as normal
    svc.handle_message(A, "I'm off duty tonight", at(1, 18, 43))
    assert to(svc.tick(at(1, 20, 0)), A) == []


def test_a_late_430_text_does_not_get_a_checkin_on_its_heels(svc):
    onboard(svc, A)
    late = to(svc.tick(at(0, 20, 0)), A)                          # server was down at 4:30; catches up at 8 PM
    assert len(late) == 1 and late[0].startswith("🍽️ Tonight")
    assert to(svc.tick(at(0, 20, 45)), A) == []                   # no check-in 45 minutes later


# chasing the first photo ----------------------------------------------------
def test_homes_that_never_logged_food_get_two_gentle_chases_then_silence(svc):
    onboard(svc, C, food=None)
    assert to(svc.tick(at(0)), C) == []                           # same day as signing up: leave them be
    day1 = to(svc.tick(at(1)), C)
    assert day1 == [messages.activation_chase(1)]
    assert to(svc.tick(at(1, 18)), C) == [] and to(svc.tick(at(2)), C) == []
    assert to(svc.tick(at(3)), C) == [messages.activation_chase(2)]
    assert to(svc.tick(at(5)), C) == [] and to(svc.tick(at(9)), C) == []


def test_no_chase_once_they_have_sent_food(svc):
    onboard(svc, C, food=None)
    svc.handle_message(C, FOOD, at(0, 12))
    assert not any("Want tonight's dinner sorted" in t for t in to(svc.tick(at(1)), C))


# keeping the list fresh -----------------------------------------------------
def test_old_list_gets_a_ps_on_the_430_text_at_most_weekly(svc):
    onboard(svc, A)
    first = to(svc.tick(at(0)), A)
    assert first and "days old" not in first[0]                   # fresh list: no P.S.
    svc.handle_message(A, "bought eggs", at(1, 9))
    week = to(svc.tick(at(8)), A)
    assert len(week) == 1 and messages.stale_list(7) in week[0]   # a week since "bought eggs": the daily pick asks for a photo
    assert not any("days" in t and "list" in t for t in to(svc.tick(at(9)), A) + to(svc.tick(at(12)), A))  # weekly at most


def test_old_list_rides_on_the_430_text_when_there_is_one(svc):
    onboard(svc, A)
    svc.store.log(1, at(-10), "capture", source="photo")          # pretend the last photo was 10 days ago
    svc.store.db.execute("DELETE FROM events WHERE type = 'capture' AND at > ?", (at(-5).isoformat(),))
    text = to(svc.tick(at(0)), A)
    assert len(text) == 1 and text[0].startswith("🍽️ Tonight") and "P.S. Your fridge list is 10 days old" in text[0]


def test_empty_list_gets_a_standalone_ask_after_three_days(svc):
    onboard(svc, A)
    svc.handle_message(A, "used baby spinach, chicken thighs, milk", at(0, 12))
    assert to(svc.tick(at(2)), A) == []
    assert to(svc.tick(at(3)), A) == [messages.stale_list(3, standalone=True, empty=True)]
    assert to(svc.tick(at(4)), A) == []


# measuring it ---------------------------------------------------------------
def test_stats_show_whether_each_proactive_message_works(svc):
    onboard(svc, A)
    onboard(svc, C, food=None)
    svc.tick(at(0))
    svc.tick(at(0, 20, 0))                                        # A gets the check-in
    svc.handle_message(A, "made", at(0, 20, 15))                  # ...and answers it
    svc.tick(at(1))                                               # C gets chase 1
    svc.handle_message(C, FOOD, at(1, 18))                        # ...and sends food
    rep = metrics.report(svc.store, at(2))
    pro = rep["proactive"]
    assert pro["evening_checkin"] == {"value": 100.0, "n": 1, "of": 1}
    assert pro["activation_chase"] == {"value": 100.0, "n": 1, "of": 1}
    text = metrics.render(rep)
    assert "BUD REACHING OUT" in text and "8 PM check-ins: 1 sent · got an answer 100% (1/1)" in text
