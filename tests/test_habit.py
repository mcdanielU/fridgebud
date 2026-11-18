"""The daily loop: trigger (a pick every day), action (one tap), variable reward (MADE), investment (rating)."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fridgebud import habit, messages
from fridgebud.engine import Engine
from fridgebud.service import Config, Service
from fridgebud.store import Store

A = "+15195550101"
TZ = ZoneInfo("America/Toronto")
MON = datetime(2026, 11, 16, 9, 0, tzinfo=TZ)


def at(day, hour=16, minute=31):
    return (MON + timedelta(days=day)).replace(hour=hour, minute=minute)


@pytest.fixture
def svc():
    s = Service(Engine(Store()), Config(number="+15195550000"))
    for text in ("hi", "yes", "2", "bought ground beef, tortillas, baby spinach, eggs, cheddar"):
        s.handle_message(A, text, MON)
    return s


def pick(svc, day=0):
    sent = [t for p, t in svc.tick(at(day)) if p == A]
    assert sent, "expected a daily pick"
    return sent[0]


@pytest.mark.parametrize("tap", ["👍", "👍🏾", "yes", "Lock it in!", "let's do it", "ok"])
def test_one_tap_locks_in_todays_pick(svc, tap):
    text = pick(svc)
    recipe = svc.store.get_household(1).last_suggestion["recipe"]
    assert recipe in text
    reply = svc.handle_message(A, tap, at(0, 16, 40))[0]
    assert reply.startswith(f"Nice, {recipe} it is 👍") and "I'll check in after dinner" in reply
    assert svc.store.events(1, "locked")
    assert "it is 👍" not in svc.handle_message(A, tap, at(0, 16, 41))[0]     # once is enough


@pytest.mark.parametrize("tap", ["👎", "nah", "not feeling it"])
def test_one_tap_swaps_todays_pick(svc, tap):
    pick(svc)
    first = svc.store.get_household(1).last_suggestion["recipe"]
    reply = svc.handle_message(A, tap, at(0, 16, 40))[0]
    assert reply.startswith("Tonight: ") and first not in reply.split("\n")[0]
    assert svc.store.events(1, "pick_swapped")


def test_taps_mean_nothing_special_without_a_pick_today(svc):
    assert "it is 👍" not in svc.handle_message(A, "👍", at(0, 10))[0]


def test_made_pays_out_a_reward_then_asks_for_a_rating(svc):
    pick(svc)
    reward = svc.handle_message(A, "made it", at(0, 19))[0]
    assert "done ✅" in reward and "🥬 Rescued:" in reward and "≈$" in reward and "saved this month" in reward
    assert "🎉 First meal cooked with Bud" in reward                    # a milestone now and then
    assert reward.rstrip().endswith("How was it? Reply 🔥 if you loved it, 👌 if it was fine, or 👎 if never again")
    assert svc.accounts.get(1).awaiting == "rate"


def test_loving_a_dish_makes_it_a_favourite_that_comes_back(svc):
    pick(svc)
    recipe = svc.store.get_household(1).last_suggestion["recipe"]
    svc.handle_message(A, "made it", at(0, 19))
    assert svc.handle_message(A, "🔥", at(0, 19, 5))[0] == messages.rated("love", recipe)
    assert recipe in svc.store.get_household(1).persona["loved"]
    assert svc.accounts.get(1).awaiting is None
    assert "⭐ Favourites: " + recipe in svc.handle_message(A, "what do you know about me?", at(0, 20))[0]


def test_a_flop_never_comes_back(svc):
    pick(svc)
    recipe = svc.store.get_household(1).last_suggestion["recipe"]
    svc.handle_message(A, "made it", at(0, 19))
    assert "off the rotation" in svc.handle_message(A, "not again", at(0, 19, 5))[0]
    assert recipe.lower() in svc.store.get_household(1).dislikes
    svc.handle_message(A, "bought ground beef, tortillas, baby spinach", at(1, 9))
    assert all(recipe not in t for p, t in svc.tick(at(1)) if p == A)


def test_rating_window_closes_and_other_messages_pass_through(svc):
    pick(svc)
    svc.handle_message(A, "made it", at(0, 19))
    assert "it is 👍" not in svc.handle_message(A, "bought milk", at(0, 19, 2))[0]   # normal messages still work
    assert svc.accounts.get(1).awaiting == "rate"
    assert not svc.handle_message(A, "🔥", at(1, 15))[0].startswith("Noted 🔥")      # next day: stale, not a rating


def test_reward_varies_and_counts_the_week(svc):
    cheers = set()
    for day in range(3):
        svc.handle_message(A, "bought ground beef, tortillas, baby spinach, eggs", at(day, 9))
        pick(svc, day)
        reward = svc.handle_message(A, "made it", at(day, 19))[0]
        cheers.add(reward.split(" done")[0])
        svc.handle_message(A, "👌", at(day, 19, 5))
    assert "3rd home-cooked meal this week" in reward
    assert len(cheers) >= 2                                               # not the same line every night


def test_dollar_milestones_cross_once():
    assert habit.crossed(8, 12, habit.DOLLAR_MARKS) == 10
    assert habit.crossed(12, 20, habit.DOLLAR_MARKS) is None
    assert habit.crossed(20, 60, habit.DOLLAR_MARKS) == 50
    assert habit.value("chicken thighs") == 11.0 and habit.value("milk", price=3.49) == 3.49
    assert habit.value("chili", kind="leftover", portions=3) == 12.0


def test_notebook_shows_what_bud_has_learned(svc):
    svc.set_persona(svc.store.get_household(1), {"likes": ["jollof"], "cuisines": ["Nigerian"],
                                                 "meals": {"dinner": "19:00"}, "main_meal": "dinner"}, MON)
    book = svc.handle_message(A, "notebook", at(0, 12))[0]
    assert "❤️ Loves: jollof" in book and "🌍 Cuisines: Nigerian" in book and "dinner 7pm" in book.replace(" PM", "pm")


def test_cook_after_locking_in_gives_that_dishes_steps():
    from tests.test_kitchen import FakeChef
    s = Service(Engine(Store()), Config(number="+15195550000"), chef=FakeChef())
    for text in ("hi", "yes", "2", "bought chicken thighs, bell peppers, tortillas, baby spinach, eggs, cheddar"):
        s.handle_message(A, text, MON)
    s.tick(at(0))
    s.handle_message(A, "👍", at(0, 16, 40))
    steps = s.handle_message(A, "COOK", at(0, 17))[0]
    assert "Steps:" in steps and not steps.startswith("Tonight: ")          # the steps, not a different idea
    assert s.book.current(1) is not None


def test_bud_learns_cuisine_from_what_they_ask_for_and_says_so_once():
    from tests.test_kitchen import FakeChef
    s = Service(Engine(Store()), Config(number="+15195550000"), chef=FakeChef())
    for text in ("hi", "yes", "2", "bought chicken thighs, tomatoes, bell peppers, baby spinach, leaf lettuce, cucumber"):
        s.handle_message(A, text, MON)
    before = [t for p, t in s.tick(at(0)) if p == A][0]
    assert "Jollof" not in before                                       # no idea of their taste yet
    texts = ["recipe egusi soup", "how do I make jollof rice", "recipe suya"]
    for i, t in enumerate(texts):                                       # a few lookups is not "love"
        out = s.handle_message(A, t, at(0, 18 + i))
        assert messages.noticed("West African") not in out
    same_day = s.handle_message(A, "recipe efo riro", at(0, 22))
    assert messages.noticed("West African") not in same_day            # four in one sitting is still not a taste
    fourth = s.handle_message(A, "recipe ofada stew", at(1, 8))
    assert fourth[-1] == messages.noticed("West African")              # said once, when it tips over
    fifth = s.handle_message(A, "recipe akara", at(1, 9))
    assert messages.noticed("West African") not in fifth               # never repeated
    s.handle_message(A, "bought chicken thighs, tomatoes", at(1, 10))
    after = [t for p, t in s.tick(at(1)) if p == A][0]
    from fridgebud import taste
    assert taste.cuisine_of(after.split("Tonight: ")[1].split(",")[0]) == "West African" or "Efo" in after or "pepper soup" in after
    assert "🧠 Picked up that you love: West African" in s.handle_message(A, "what do you know about me?", at(1, 20))[0]


def test_a_cuisine_they_keep_rejecting_sinks():
    from fridgebud import taste
    p, _ = taste.learn({}, "Thai", 2.5)
    p, _ = taste.learn(p, "Thai", -2.0)
    p, _ = taste.learn(p, "Thai", -2.0)
    assert taste.weights(p)["Thai"] < 0 and taste.learned(p) == []


def test_cuisine_names_map_sensibly():
    from fridgebud.taste import cuisine_of, normalize
    assert cuisine_of("Egusi Soup (Nigerian Melon Seed Soup)") == "West African"
    assert cuisine_of("Thai green curry") == "Thai" and cuisine_of("chicken curry") == "Indian"
    assert cuisine_of("curry goat") == "Caribbean" and cuisine_of("Chicken shawarma bowl") == "Middle Eastern"
    assert normalize("Nigerian food") == "West African" and cuisine_of("Big chopped salad") is None


def test_tips_teach_one_feature_at_a_time_then_stop(svc):
    seen = {}
    for day in range(0, 34):
        svc.handle_message(A, "bought ground beef, tortillas, baby spinach", at(day, 9))
        for p, t in svc.tick(at(day)):
            if p == A and "💡 Tip:" in t:
                seen[day] = t.split("💡 Tip:")[1].split("\n")[0]
    days = sorted(seen)
    assert days and days[0] >= 1                                       # nothing on signup day
    assert all(b - a >= 2 for a, b in zip(days, days[1:]))             # never two days running
    assert len(set(seen.values())) == len(seen)                        # each tip once
    assert max(days) <= 29                                             # stops after four weeks


def test_tips_skip_what_they_already_use(svc):
    svc.handle_message(A, "do I have eggs?", at(0, 12))                # they know HAVE already
    texts = []
    for day in range(1, 29):
        svc.handle_message(A, "bought milk", at(day, 9))                   # an engaged user (an ignored one gets fewer)
        texts += [t for p, t in svc.tick(at(day)) if p == A]
    assert not any("do I have eggs" in t for t in texts)
    assert any("JOIN " in t for t in texts) or len([t for t in texts if "💡" in t]) >= 5


# how often Bud texts ----------------------------------------------------------
def texts(svc, day, hour, minute=0):
    return [t for p, t in svc.tick(at(day, hour, minute)) if p == A]


def test_more_asks_which_meals_then_sends_those_ideas(svc):
    assert svc.handle_message(A, "MORE", at(0, 12))[0] == messages.ASK_MORE
    on = svc.handle_message(A, "both please", at(0, 12, 1))[0]
    assert on.startswith("Done ✅ You'll get a breakfast idea around 7:30 AM and a lunch idea around 11 AM")
    svc.handle_message(A, "bought eggs, bread, baby spinach, chicken thighs", at(1, 6))
    morning = texts(svc, 1, 7, 31)
    assert len(morning) == 1 and morning[0].startswith("☀️ Breakfast: ") and "Too many texts? Reply LESS" in morning[0]
    assert texts(svc, 1, 8) == []                                           # once a morning
    midday = texts(svc, 1, 11, 1)
    assert len(midday) == 1 and midday[0].startswith("🥪 For lunch: ")


def test_less_turns_extras_off_then_goes_every_other_day_and_more_undoes_it(svc):
    svc.handle_message(A, "more", at(0, 12)); svc.handle_message(A, "breakfast", at(0, 12, 1))
    assert svc.handle_message(A, "LESS", at(0, 13))[0] == messages.EXTRAS_OFF
    assert svc.handle_message(A, "text me less", at(0, 13, 1))[0] == messages.EVERY_OTHER_DAY
    sent = [bool(texts(svc, d, 16, 31)) for d in range(1, 5)]
    assert sent == [True, False, True, False]                                # every other day
    assert svc.handle_message(A, "less", at(5, 9))[0] == messages.ALREADY_LEAST
    assert svc.handle_message(A, "more", at(5, 9, 1))[0] == messages.BACK_TO_DAILY


def test_breakfast_offer_comes_once_on_day_3_at_breakfast_time(svc):
    for day in range(0, 3):
        svc.handle_message(A, "bought eggs, bread, baby spinach", at(day, 6))
        assert not any("Want a breakfast idea" in t for t in texts(svc, day, 7, 31))
    offer = texts(svc, 3, 7, 31)
    assert len(offer) == 1 and offer[0].startswith("☀️ Morning! Breakfast: ") and "Reply 👍 for yes or 👎 for no thanks" in offer[0]
    assert svc.handle_message(A, "👍", at(3, 7, 40))[0].startswith("Done ✅ You'll get a breakfast idea around 7:30 AM")
    assert svc.store.get_household(1).persona["extra"] == ["breakfast"]
    assert not any("Want a breakfast idea" in t for d in range(4, 9) for t in texts(svc, d, 7, 31))   # never asked again


def test_saying_no_to_the_offer_is_respected(svc):
    svc.handle_message(A, "bought eggs, bread", at(3, 6))
    texts(svc, 3, 7, 31)
    assert svc.handle_message(A, "no thanks", at(3, 7, 40))[0] == messages.meal_offer_declined("breakfast")
    assert not (svc.store.get_household(1).persona or {}).get("extra")


def test_offer_skipped_for_someone_who_says_they_skip_breakfast(svc):
    svc.set_persona(svc.store.get_household(1), {"meals": {"lunch": "12:30", "dinner": "19:00"}, "main_meal": "dinner"}, MON)
    svc.handle_message(A, "bought eggs, bread", at(3, 6))
    assert not any("breakfast" in t.lower() for t in texts(svc, 3, 7, 31))


def test_an_ignored_offer_does_not_hijack_the_evening(svc):
    svc.handle_message(A, "bought eggs, bread, ground beef, tortillas", at(3, 6))
    texts(svc, 3, 7, 31)                                                     # breakfast offer, never answered
    texts(svc, 3, 16, 31)                                                    # the dinner pick later that day
    assert "it is 👍" in svc.handle_message(A, "👍", at(3, 16, 40))[0]


def test_bud_backs_off_when_nobody_answers_and_comes_back_when_they_do(svc):
    sent = []
    for day in range(0, 8):
        svc.handle_message(A, "bought ground beef, tortillas, baby spinach", at(day, 9)) if day == 0 else None
        sent.append(bool(texts(svc, day, 16, 31)))
    assert sent[:3] == [True, True, True] and sent[3] is False and sent[4] is True   # three unanswered: every other day
    svc.handle_message(A, "bought eggs, ground beef", at(8, 12))
    assert texts(svc, 8, 16, 31) and texts(svc, 9, 16, 31)                   # they're back: daily again


# variety: a fuzzy dial learned from what they do ---------------------------------------
def test_variety_dial_moves_gently_and_reads_fuzzily():
    from fridgebud import taste
    p = {}
    assert taste.variety(p) == taste.VARIETY_START
    for _ in range(6):
        p = taste.nudge_variety(p, 0.1, 0.12)
    assert 0.15 < taste.variety(p) < 0.3 and taste.describe(taste.variety(p)).startswith("comfort-first")
    assert "balance" in taste.describe(0.5) and taste.describe(0.92) == "adventurous"
    assert taste.describe(0.35) == "a balance, leaning familiar"


def test_comfort_cooks_get_familiar_dishes_and_adventurous_ones_get_new():
    from fridgebud import recipes
    from fridgebud.models import Item
    from datetime import date
    fridge = [Item(i, 1, n, k, None, "grocery", date(2026, 11, 16)) for i, (n, k) in
              enumerate([("chicken thighs", "chicken"), ("tomatoes", "tomatoes"), ("bell peppers", "peppers"),
                         ("tortillas", "tortillas")], 1)]
    comfort = recipes.suggest([], fridge, [], any_day=True, taste={}, variety=0.1, history={"chicken fajitas": 4})
    bold = recipes.suggest([], fridge, [], any_day=True, taste={}, variety=0.95, history={"chicken fajitas": 4})
    assert comfort.title == "Chicken fajitas" and comfort.novelty == 0   # the dish they already make
    assert bold.novelty >= 0.75                                      # a cuisine they haven't tried yet
    again = recipes.suggest([], fridge, [], any_day=True, taste={}, variety=0.1, history={"chicken fajitas": 1})
    assert again.title != "Chicken fajitas"                         # but not the same dish two nights running


def test_swapping_new_dishes_teaches_bud_to_play_it_safer(svc):
    from fridgebud import taste
    for day in range(3):
        svc.handle_message(A, "bought chicken thighs, tomatoes, bell peppers, tortillas, baby spinach", at(day, 9))
        texts(svc, day, 16, 31)
        h = svc.store.get_household(1)
        h.last_suggestion = dict(h.last_suggestion or {}, novelty=0.75)   # pretend tonight's pick was brand new to them
        svc.store.save_household(h)
        svc.handle_message(A, "👎", at(day, 16, 40))
    assert taste.variety(svc.store.get_household(1).persona) < taste.VARIETY_START
    assert "🎲 How adventurous: comfort-first" in svc.handle_message(A, "what do you know about me?", at(3, 9))[0]


# "I don't have that" / "something not from my fridge" / "I'm craving suya" -----------------
@pytest.mark.parametrize("said", ["I don't have most of the ingredients", "i dont have those", "I'm missing most of it"])
def test_not_having_the_ingredients_gets_three_ways_forward(svc, said):
    pick(svc)
    recipe = svc.store.get_household(1).last_suggestion["recipe"]
    reply = svc.handle_message(A, said, at(0, 16, 45))[0]
    assert reply.startswith("Ah, my picture of your fridge must be out of date")
    assert "👎 for an idea from what you do have" in reply and f"RECIPE {recipe}" in reply and "📸" in reply
    assert svc.store.events(1, "fridge_mismatch")


def test_not_having_what_a_recipe_card_needs_offers_shop():
    from tests.test_kitchen import FakeChef
    s = Service(Engine(Store()), Config(number="+1"), chef=FakeChef())
    for text in ("hi", "yes", "2", "bought eggs"):
        s.handle_message(A, text, MON)
    s.handle_message(A, "recipe jollof rice", at(0, 12))
    assert "• SHOP to add what Jollof rice needs to your list" in s.handle_message(A, "i don't have most of that", at(0, 12, 5))[0]


def test_wanting_something_outside_the_fridge_asks_then_delivers_the_recipe():
    from tests.test_kitchen import FakeChef
    s = Service(Engine(Store()), Config(number="+1"), chef=FakeChef())
    for text in ("hi", "yes", "2", "bought eggs", "recipe egusi soup", "recipe jollof rice"):
        s.handle_message(A, text, MON)
    ask = s.handle_message(A, "I want something not from my fridge", at(0, 12))[0]
    assert ask.startswith("Let's go off-script 😄") and "jollof rice" in ask          # examples in their taste
    card = s.handle_message(A, "pad thai", at(0, 12, 1))[0]
    assert "📖" in card and "SHOP" in card                                               # the recipe, with what to buy


@pytest.mark.parametrize("said,dish", [("I'm craving suya", "suya"), ("i feel like pasta tonight", "pasta"),
                                       ("in the mood for shawarma", "shawarma"), ("i want pad thai", "pad thai")])
def test_cravings_go_straight_to_a_recipe(said, dish):
    from fridgebud.commands import service_command
    assert service_command(said) == ("recipe", [dish])



@pytest.mark.parametrize("words,locked", [("cook it", True), ("yes", True), ("I'll make it", True),
                                          ("another one", False), ("something different", False), ("no", False)])
def test_plain_words_work_as_well_as_emoji(svc, words, locked):
    pick(svc)
    reply = svc.handle_message(A, words, at(0, 16, 40))[0]
    assert ("it is 👍" in reply) == locked and (reply.startswith("Tonight: ") or locked)


# the gentle follow-up when the pick got no reply --------------------------------------------
def test_silence_after_the_pick_gets_one_follow_up_an_hour_later(svc):
    pick(svc)
    recipe = svc.store.get_household(1).last_suggestion["recipe"]
    assert texts(svc, 0, 17, 0) == []                                       # not yet: give them an hour
    follow = texts(svc, 0, 17, 32)
    assert len(follow) == 1 and follow[0].startswith("Still on for tonight? 🍳") and f"make the {recipe}?" in follow[0]
    assert "Reply 👍 if yes" in follow[0] and "Reply 👎 for a different idea" in follow[0]
    assert texts(svc, 0, 17, 50) == []                                      # once
    assert "it is 👍" in svc.handle_message(A, "👍", at(0, 17, 55))[0]      # 👍 to the follow-up locks it in
    assert any("How was dinner" in t for t in texts(svc, 0, 20, 1))        # and the evening check-in still comes


def test_no_follow_up_if_they_already_replied(svc):
    pick(svc)
    svc.handle_message(A, "bought milk", at(0, 16, 50))                    # any message counts as "still here"
    assert not any("Still on" in t for t in texts(svc, 0, 17, 32))


def test_an_ignored_follow_up_means_no_evening_text(svc):
    pick(svc)
    texts(svc, 0, 17, 32)                                                   # follow-up, never answered
    assert texts(svc, 0, 20, 1) == []                                       # busy evening: two texts is enough


def test_no_follow_up_too_close_to_when_they_eat(svc):
    svc.set_persona(svc.store.get_household(1), {"meals": {"dinner": "17:45"}, "main_meal": "dinner"}, MON)
    h = svc.store.get_household(1)
    h.nudge_hour, h.nudge_minute = 16, 50                                  # a late pick, close to a 5:45 dinner
    svc.store.save_household(h)
    assert [t for p, t in svc.tick(at(0, 16, 51)) if p == A]
    assert not any("Still on" in t for t in texts(svc, 0, 17, 52))        # an hour later is too close to dinner


def test_follow_up_offers_the_saved_recipe_too():
    from fridgebud import messages as m
    text = m.still_on("Thai basil stir-fry", "dinner", "Efo Riro (Yoruba Spinach Stew)")
    assert "Reply 2 for the Efo Riro instead" in text



# rating a recipe with a tap, not a word ------------------------------------------------------
def test_ai_recipe_asks_for_a_thumb_and_a_thumb_keeps_it():
    from tests.test_kitchen import FakeChef
    s = Service(Engine(Store()), Config(number="+1"), chef=FakeChef())
    for text in ("hi", "yes", "2", "bought eggs"):
        s.handle_message(A, text, MON)
    out = s.handle_message(A, "recipe jollof rice", at(0, 12))
    assert out[-1] == messages.rate_ai_recipe("Jollof rice") and "LIKED" not in out[-1] and "Reply 👍" in out[-1]
    s.handle_message(A, "👍", at(1, 20))
    assert [r.title for r in s.book.kept(1)] == ["Jollof rice"]


def test_a_thumbs_down_bins_the_untested_recipe():
    from tests.test_kitchen import FakeChef
    s = Service(Engine(Store()), Config(number="+1"), chef=FakeChef())
    for text in ("hi", "yes", "2", "bought eggs"):
        s.handle_message(A, text, MON)
    s.handle_message(A, "recipe jollof rice", at(0, 12))
    s.handle_message(A, "👎", at(0, 20))
    assert s.book.kept(1) == [] and s.store.events(1, "recipe_declined")


# ignoring Bud: playful nudges, then Bud steps back, then a warm welcome back -------------------
def test_ignored_picks_get_cheeky_then_bud_steps_back_and_wakes_on_any_reply(svc):
    sent = {}
    for day in range(0, 14):
        for p, t in svc.tick(at(day)):
            if p == A:
                sent[day] = t
    ladder = [(d, t) for d, t in sorted(sent.items())]
    assert any(messages.IGNORED_PS[4] in t for _, t in ladder)
    assert any(messages.IGNORED_PS[5] in t for _, t in ladder)
    assert any(messages.IGNORED_PS[6] in t for _, t in ladder)
    last_day, last = ladder[-1]
    assert last == messages.STEPPING_BACK                                  # the final word, instead of a pick
    assert all(not [t for p, t in svc.tick(at(d)) if p == A] for d in range(last_day + 1, last_day + 6))   # quiet after
    back = svc.handle_message(A, "what's for dinner?", at(last_day + 6, 12))
    assert back[-1] == messages.WELCOME_BACK
    assert [t for p, t in svc.tick(at(last_day + 7)) if p == A]           # daily ideas are back
    for t in sent.values():
        assert "waste" not in t.lower() and "money" not in t.lower()     # never guilt about food or money


def test_stats_track_the_daily_loop(svc):
    from fridgebud import metrics
    pick(svc)
    svc.handle_message(A, "👍", at(0, 16, 40))
    svc.handle_message(A, "made it", at(0, 19))
    svc.handle_message(A, "🔥", at(0, 19, 5))
    rep = metrics.report(svc.store, at(1, 9))
    lp = rep["loop"]
    assert lp["picks"] == 1 and lp["cook"]["n"] == 1 and lp["made_after"]["n"] == 1 and lp["rated"] == {"love": 1}
    text = metrics.render(rep)
    assert "THE DAILY LOOP (last 7 days)" in text and "👍 cook it 100% (1/1)" in text and "Rated a dish" in text


def test_taste_bar_adapts_and_old_signals_fade():
    from datetime import date
    from fridgebud import taste
    d0 = date(2026, 11, 1)
    p = {}
    for i in range(3):                                                  # asked on 3 days: Bud may say it noticed
        p, new = taste.learn(p, "Thai", 2.5, d0.replace(day=1 + i))
    assert new and taste.learned(p) == ["Thai"]
    assert taste.bar(p) == taste.LEARNED
    p, _ = taste.learn(p, "Thai", -2.0, d0.replace(day=4))              # they shot one down: Bud was too eager
    assert taste.bar(p) > taste.LEARNED
    q, _ = taste.learn({}, "Thai", 5.0, d0)
    q, _ = taste.learn(q, "Italian", 1.0, date(2026, 12, 20))           # 7 weeks later, the old Thai has faded
    assert q["learned"]["Thai"] < 1.5


def test_every_daily_pick_is_logged_as_training_data_with_its_outcome():
    from tests.test_kitchen import FakeChef
    from fridgebud import dataset
    s = Service(Engine(Store()), Config(number="+15195550000"), chef=FakeChef())
    for text in ("hi", "yes", "2", "bought chicken thighs, bell peppers, tortillas, baby spinach, eggs, cheddar"):
        s.handle_message(A, text, MON)
    s.tick(at(0))
    decisions = s.store.events(None, "decision")
    assert len(decisions) == 1
    pl = decisions[0]["payload"]
    assert pl["policy"] == "rules_v2" and pl["recipe"] and "variety" in pl and "taste" in pl and "hour" in pl
    s.handle_message(A, "👍", at(0, 16, 40))
    table = dataset.rows(s.store)
    assert len(table) == 1 and table[0]["locked"] == 1 and table[0]["swapped"] == 0


def test_a_dish_the_fridge_covers_with_several_things_beats_one_built_on_a_single_ingredient():
    from fridgebud import recipes
    from fridgebud.models import Item
    from datetime import date
    fridge = [Item(i, 1, n, k, None, "grocery", date(2026, 11, 16)) for i, (n, k) in
              enumerate([("rotisserie chicken", "chicken"), ("baby spinach", "spinach"), ("tomatoes", "tomatoes"),
                         ("cucumber", "cucumber")], 1)]
    for ask in range(6):                                              # whatever the order or taste, never single-item
        pick = recipes.suggest([], fridge, [], any_day=True, taste={"West African": 4, "Thai": 4}, variety=0.5,
                               avoid=["Chicken shawarma bowl"] if ask else None)
        names = {r.name: r for r in recipes.BANK}
        r = names[pick.title]
        assert len(r.needs) + len(r.uses & {i.key for i in fridge}) >= 2, pick.title


def test_when_the_dish_list_fits_the_fridge_poorly_the_ai_writes_a_proper_meal_within_a_daily_cap():
    from tests.test_kitchen import FakeChef
    chef = FakeChef()
    s = Service(Engine(Store()), Config(number="+15195550000", ai_idea_daily_cap=2), chef=chef)
    for text in ("hi", "yes", "2", "bought rotisserie chicken"):          # one ingredient: only single-ingredient dishes fit
        s.handle_message(A, text, MON)
    idea = s.handle_message(A, "DINNER", at(0, 12))[0]
    assert "🧪 I wrote this one" in idea and "Jollof" in idea and "Reply 👍 to cook this" in idea
    assert [c for c in chef.calls if isinstance(c, tuple)][0][0] == "generate"
    state = s.store.get_household(1).last_suggestion
    assert state["recipe"] and state.get("ai") is True                     # so 👍 and COOK work on it
    s.handle_message(A, "DINNER", at(0, 12, 2))
    third = s.handle_message(A, "DINNER", at(0, 12, 4))[0]                 # cap of 2 reached: back to the dish list
    assert "🧪 I wrote this one" not in third
    assert len(s.store.events(1, "ai_idea")) == 2


def test_the_daily_pick_can_be_ai_written_when_nothing_in_the_list_fits():
    from tests.test_kitchen import FakeChef
    s = Service(Engine(Store()), Config(number="+15195550000"), chef=FakeChef())
    for text in ("hi", "yes", "2", "bought rotisserie chicken"):
        s.handle_message(A, text, MON)
    pick = [t for p, t in s.tick(at(0)) if p == A]
    assert pick and "🧪 I wrote this one" in pick[0]
