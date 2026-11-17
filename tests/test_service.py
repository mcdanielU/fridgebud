from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fridgebud import messages, metrics
from fridgebud.capture import CapturedItem
from fridgebud.engine import Engine
from fridgebud.fallback import ClaudeInterpreter, validate
from fridgebud.service import Config, Service
from fridgebud.sms import normalize_phone, signature, timezone_for, valid_signature
from fridgebud.store import Store
from fridgebud.voice import budify

TZ = ZoneInfo("America/Toronto")
A, B, C = "+15195550101", "+15195550102", "+16045550103"   # households 1 (plain), 2 (bud), 3 (plain)
MON = datetime(2026, 11, 16, 9, 0, tzinfo=TZ)
NUMBER = "+12265550000"


def at(day, hour=16, minute=30):
    return (MON + timedelta(days=day)).replace(hour=hour, minute=minute)


class FakeInterpreter:
    def __init__(self, command):
        self.command, self.calls = command, 0

    def interpret(self, text, item_names, last_recipe):
        self.calls += 1
        return self.command


@pytest.fixture
def svc():
    return Service(Engine(Store()), Config(number=NUMBER))


def onboard(svc, phone, items="baby spinach, chicken thighs, milk", when=MON):
    svc.handle_message(phone, "hi", when)
    svc.handle_message(phone, "yes", when)
    svc.handle_message(phone, "2", when)
    return svc.handle_message(phone, f"bought {items}", when)


# onboarding ---------------------------------------------------------------
def test_onboarding_reaches_first_watchlist_in_four_texts(svc):
    assert "Reply YES" in svc.handle_message(A, "hi", MON)[0]
    assert svc.handle_message(A, "YES", MON) == [messages.ASK_SIZE]
    ack, photo_ask = svc.handle_message(A, "3", MON)
    assert "3 of you" in ack and f"JOIN" in ack and NUMBER in ack
    assert "photo" in photo_ask
    assert "Got it: baby spinach" in svc.handle_message(A, "bought baby spinach", MON)[0]
    prof = svc.accounts.get(1)
    assert (prof.stage, prof.size, prof.source) == ("active", 3, "sms") and prof.consented_at == MON


def test_no_texts_before_yes(svc):
    svc.handle_message(A, "hi", MON)
    svc.handle_message(A, "bought chicken", MON)
    assert svc.tick(at(1)) == []


def test_size_question_can_be_skipped(svc):
    svc.handle_message(A, "hi", MON)
    svc.handle_message(A, "yes", MON)
    replies = svc.handle_message(A, "bought milk, kale", MON)
    assert "Got it: milk, kale" in replies[0]
    assert "Who else eats from this fridge" in replies[1]       # invite prompt still shown once
    assert svc.accounts.get(1).stage == "active"
    assert len(svc.handle_message(A, "bought eggs", MON)) == 1


def test_just_me_gets_no_invite(svc):
    svc.handle_message(A, "hi", MON)
    svc.handle_message(A, "yes", MON)
    assert svc.handle_message(A, "just me", MON)[0] == "Got it, just you."


def test_voice_is_split_evenly_and_logged(svc):
    for phone in (A, B, C):
        svc.handle_message(phone, "hi", MON)
    assert [svc.accounts.get(i).voice for i in (1, 2, 3)] == ["plain", "bud", "plain"]
    assert len(svc.store.events(type_="voice_assigned")) == 3


def test_timezone_from_area_code(svc):
    svc.handle_message(C, "hi", MON)
    assert svc.store.get_household_by_phone(C).timezone == "America/Vancouver"
    onboard(svc, C, "ground beef")
    assert svc.tick(at(0, 16, 30)) == []                         # 1:30 PM in Vancouver
    assert len(svc.tick(at(0, 19, 30))) == 1                     # 4:30 PM in Vancouver


# stop / consent -----------------------------------------------------------
def test_stop_always_works(svc):
    onboard(svc, A)
    assert svc.handle_message(A, "STOP", at(0, 10)) == [messages.STOPPED]
    assert svc.tick(at(1)) == []


def test_compliance_texts_are_never_restyled(svc):
    svc.handle_message(A, "hi", MON)
    onboard(svc, B)                                              # bud voice
    assert svc.handle_message(B, "help", MON) == [messages.HELP]
    assert svc.handle_message(B, "stop", MON) == [messages.STOPPED]


# voice --------------------------------------------------------------------
def test_bud_and_plain_get_the_same_facts(svc):
    onboard(svc, A, "ground beef, tortillas")
    onboard(svc, B, "ground beef, tortillas")
    sent = dict(svc.tick(at(0)))
    assert sent[A].startswith("Use soon: ground beef")
    assert sent[B] == sent[A]                                    # same facts; Bud never adds a line
    assert "Rescued: ground beef (≈$11)" in svc.handle_message(B, "made it", at(0, 19))[0]
    assert "Rescued: ground beef (≈$11)" in svc.handle_message(A, "made it", at(0, 19))[0]


@pytest.mark.parametrize("plain", [
    "Use soon: milk (use today).\nReply e.g. GOOD milk or TOSSED milk. LIST shows all.",
    "Worth checking: chicken thighs. Still good, or tossed?",
    "Logged milk as tossed. I'll flag that kind of thing a little earlier next time.",
    "Good to know: milk. I'll check back in a couple of days.",
    "Nice, that rescued milk. Any leftovers? Text e.g. \"leftovers chili x3\".",
])
def test_bud_never_says_safe_and_never_blames(plain):
    for seed in range(6):
        out = budify(plain, seed).lower()
        assert "safe" not in out and "you wasted" not in out and "your fault" not in out


# free-form replies --------------------------------------------------------
def test_fallback_turns_free_text_into_a_command(svc):
    svc.interpreter = FakeInterpreter("TOSSED spinach")
    onboard(svc, A)
    reply = svc.handle_message(A, "ugh the spinach went slimy so it's in the compost", at(2, 10))
    assert "baby spinach as tossed" in reply[0]
    assert all(i.key != "spinach" for i in svc.store.active_items(1))


def test_fallback_not_used_for_replies_the_parser_understands(svc):
    svc.interpreter = FakeInterpreter("TOSSED spinach")
    onboard(svc, A)
    svc.handle_message(A, "good milk", at(2, 10))
    assert svc.interpreter.calls == 0


def test_fallback_unsure_gets_help_and_is_capped(svc):
    svc.interpreter = FakeInterpreter(None)
    onboard(svc, A)
    for _ in range(8):
        assert svc.handle_message(A, "is my chicken ok to eat?", at(1, 10)) == [messages.UNKNOWN]
    assert svc.interpreter.calls == svc.config.fallback_daily_cap


def test_fallback_can_never_opt_someone_in():
    assert validate("YES", 0.99) is None and validate("START", 0.99) is None
    assert validate("USED chicken", 0.4) is None and validate("NONE", 0.99) is None
    assert validate("LEFTOVERS chili x2", 0.9) == "LEFTOVERS chili x2"


def test_claude_interpreter_parses_model_json():
    def transport(url, headers, body):
        return {"content": [{"type": "text", "text": '{"command": "USED chicken thighs", "confidence": 0.92}'}]}
    assert ClaudeInterpreter("k", transport=transport).interpret("ate the chicken", ["chicken thighs"], None) == "USED chicken thighs"

    def junk(url, headers, body):
        return {"content": [{"type": "text", "text": "not json"}]}
    assert ClaudeInterpreter("k", transport=junk).interpret("x", [], None) is None


def test_fallback_outage_does_not_break_the_chat(svc):
    class Broken:
        def interpret(self, *a):
            raise RuntimeError("503")
    svc.interpreter = Broken()
    onboard(svc, A)
    assert svc.handle_message(A, "blah blah", at(1, 10)) == [messages.UNKNOWN]
    assert svc.store.events(type_="fallback_error")


# photos -------------------------------------------------------------------
def test_photo_items_and_prices_are_saved(svc):
    onboard(svc, A, "milk")
    found = [CapturedItem("chicken thighs", price=11.47), CapturedItem("PC MLTGRN", confidence=0.3)]
    reply = svc.handle_message(A, "", MON, captured=found, has_media=True)[0]
    assert "chicken thighs" in reply and "Saved your prices" in reply and "PC MLTGRN" in reply
    assert [i.price for i in svc.store.active_items(1) if i.key == "chicken"] == [11.47]


def test_unreadable_photo_gets_a_kind_reply(svc):
    onboard(svc, A, "milk")
    assert svc.handle_message(A, "", MON, captured=[], has_media=True) == [messages.PHOTO_UNREADABLE]


# households ---------------------------------------------------------------
def test_fridge_mate_joins_and_shares_the_loop(svc):
    onboard(svc, A, "ground beef, tortillas")
    code = svc.accounts.get(1).code
    assert svc.handle_message(B, f"JOIN {code}", MON) == [messages.JOIN_PROMPT]
    assert dict(svc.tick(at(0))).keys() == {A}                   # not until they say YES
    assert svc.handle_message(B, "yes", at(0, 17)) == [messages.MEMBER_IN]
    assert {p for p, _ in svc.tick(at(1))} == {A, B}             # both get the 4:30 text
    assert "Rescued: ground beef" in svc.handle_message(B, "made it", at(1, 19))[0]
    assert svc.store.get_household_by_phone(B) is None           # one fridge, not two
    assert svc.handle_message(B, "stop", at(1, 20)) == [messages.STOPPED]
    svc.handle_message(A, "bought ground beef", at(2, 9))
    assert {p for p, _ in svc.tick(at(2))} == {A}                # owner carries on


def test_referral_is_recorded(svc):
    onboard(svc, A)
    code = svc.accounts.get(1).code
    svc.handle_message(B, f"hi {code}", MON)
    assert svc.accounts.get(2).referred_by == 1
    assert svc.store.events(1, "referral")


# sunday receipt -----------------------------------------------------------
def test_sunday_receipt_leads_with_dollars(svc):
    onboard(svc, A, "milk")
    svc.handle_message(A, "", MON, has_media=True, captured=[
        CapturedItem("chicken thighs", price=11.47), CapturedItem("baby spinach", price=3.99),
        CapturedItem("strawberries", price=4.99)])
    svc.handle_message(A, "used chicken, spinach", at(1, 19))
    svc.handle_message(A, "tossed strawberries", at(3, 19))
    sunday = at(6, 11, 0)
    sent = [t for p, t in svc.tick(sunday) if "RECEIPT" in t]
    assert len(sent) == 1
    lines = sent[0].splitlines()
    assert lines[0].startswith("🧾") and "$15.46 KEPT OUT OF THE BIN" in lines[3]
    assert "✅ chicken thighs  $11.47" in sent[0] and "strawberries" in sent[0]
    assert "Rescue rate: 67% (2 of 3)" in sent[0]
    assert f"text HI {svc.accounts.get(1).code} to {NUMBER}" in sent[0]
    assert not [t for _, t in svc.tick(at(6, 15, 0)) if "RECEIPT" in t]   # once per Sunday


def test_no_receipt_when_nothing_happened(svc):
    onboard(svc, A, "carrots")
    assert not [t for _, t in svc.tick(at(6, 11, 0)) if "RECEIPT" in t]


def test_receipt_asks_for_estimate_when_no_prices(svc):
    onboard(svc, A, "baby spinach")
    svc.handle_message(A, "used spinach", at(1, 19))
    text = [t for _, t in svc.tick(at(6, 11, 0))][0]
    assert "1 ITEM KEPT OUT OF THE BIN" in text and "Reply with a dollar amount" in text
    assert svc.handle_message(A, "$4", at(6, 12)) == ["Thanks. Logged about $4 kept this week (your estimate)."]
    assert svc.store.events(1, "self_reported_savings")[0]["payload"]["amount"] == 4.0


# paywall ------------------------------------------------------------------
def test_day_14_paywall_then_pause_unless_paid():
    svc = Service(Engine(Store()), Config(number=NUMBER, price="$6.99 a month", payment_link="https://pay.example/x"))
    onboard(svc, A, "milk")
    assert not [t for _, t in svc.tick(at(13, 13))if "free days" in t]
    sent = svc.tick(at(14, 13))
    assert len(sent) == 1 and "$6.99 a month" in sent[0][1] and "https://pay.example/x" in sent[0][1]
    assert svc.tick(at(14, 14)) == []                            # only once
    svc.handle_message(A, "bought ground beef", at(15, 9))
    assert len(svc.tick(at(15))) == 1                            # grace period: still nudged
    svc.handle_message(A, "bought ground beef", at(18, 9))
    assert svc.tick(at(18)) == []                                # paused
    assert svc.mark_paid(A, at(18, 17)) == A
    assert len(svc.tick(at(19))) == 1                            # back on


def test_paywall_is_off_without_a_payment_link(svc):
    onboard(svc, A, "milk")
    svc.handle_message(A, "bought ground beef", at(20, 9))
    assert len(svc.tick(at(20))) == 1


# web signup ---------------------------------------------------------------
def test_web_signup_records_consent_and_waits_for_yes(svc):
    ok, phone = svc.signup_web("(519) 555-0101", MON, ip="203.0.113.9", consent_wording="I agree...")
    assert (ok, phone) == (True, A)
    event = svc.store.events(1, "consent_web")[0]["payload"]
    assert event == {"ip": "203.0.113.9", "wording": "I agree..."}
    assert not svc.store.get_household_by_phone(A).opted_in
    assert svc.handle_message(A, "yes", MON) == [messages.ASK_SIZE]
    assert svc.signup_web("519-555-0101", MON)[0] is False        # no second welcome text
    assert svc.signup_web("12345", MON)[0] is False


# channel helpers ----------------------------------------------------------
def test_phone_helpers():
    assert normalize_phone("1 (604) 555-0199") == "+16045550199"
    assert normalize_phone("555-0199") is None and normalize_phone("+44 20 7946 0958") is None
    assert timezone_for("+17095550100") == "America/St_Johns"


def test_twilio_signature_round_trip():
    params = {"From": A, "Body": "made it", "NumMedia": "0"}
    sig = signature("token", "https://x.example/sms", params)
    assert valid_signature("token", "https://x.example/sms", params, sig)
    assert not valid_signature("token", "https://x.example/sms", params | {"Body": "stop"}, sig)
    assert not valid_signature("other", "https://x.example/sms", params, sig)


# scorecard ----------------------------------------------------------------
def test_scorecard_counts_the_gates(svc):
    onboard(svc, A, "ground beef, tortillas")
    onboard(svc, B, "ground beef, tortillas")
    svc.handle_message(C, "hi", MON)                             # signed up, never said yes
    svc.tick(at(0))
    svc.handle_message(A, "made it", at(0, 19))
    card = metrics.scorecard(svc.store, at(2))
    assert card["signups"]["value"] == 3 and card["consented"] == 2
    assert card["activation_pct"]["value"] == 100.0
    assert card["voice_test"]["plain"]["reply_rate_pct"] == 100.0
    assert card["voice_test"]["bud"]["reply_rate_pct"] == 0.0
    assert card["rescued_median_per_week"]["value"] == 0.5        # A rescued 1 item, B none


# instant value: rescue plan, DINNER, OLD ----------------------------------
def fridge(*names):
    return [CapturedItem(n, source_kind="fridge") for n in names]


def test_first_photo_gets_a_rescue_plan_in_seconds(svc):
    svc.handle_message(A, "hi", MON)
    svc.handle_message(A, "yes", MON)
    svc.handle_message(A, "1", MON)
    got, plan_text, age = svc.handle_message(A, "", MON, has_media=True,
                                             captured=fridge("chicken thighs", "baby spinach", "carrots", "cheddar", "eggs", "milk"))
    assert got.startswith("Got it: chicken thighs")
    lines = plan_text.splitlines()
    assert lines[0] == "Use first:" and lines[1] == "• chicken thighs: by Wed"     # Mon + 2 days, soonest first
    assert "(+2 more that keep longer)" in plan_text
    assert "Tonight: " in plan_text and "chicken thighs" in plan_text.split("Tonight: ")[1]
    assert "safe" not in plan_text.lower()
    assert age == messages.ASK_TASTE                              # first photo asks about taste, not age


def test_receipt_photo_gets_plan_but_no_age_question(svc):
    onboard(svc, A, "milk")
    replies = svc.handle_message(A, "", MON, has_media=True, captured=[CapturedItem("ground beef", source_kind="receipt")])
    assert len(replies) == 2 and replies[1].startswith("Use first:\n• ground beef: by tomorrow")


def test_dinner_on_demand_then_made_rescues(svc):
    onboard(svc, A, "chicken thighs, baby spinach, tortillas, cheddar")
    idea = svc.handle_message(A, "what's for dinner?", at(0, 12))[0]
    assert idea.startswith("Tonight: ") and "Reply 👍 to cook this" in idea
    another = svc.handle_message(A, "DINNER", at(0, 12, 5))[0]
    assert another.startswith("Tonight: ") and another != idea                 # a different idea second time
    done = svc.handle_message(A, "made it", at(0, 19))[0]
    assert "done ✅" in done and "Rescued:" in done and done.rstrip().endswith("👎 if never again")
    assert len(svc.store.active_items(1)) < 4


def test_dinner_with_nothing_to_cook(svc):
    onboard(svc, A, "carrots")
    svc.store.close_item(1, "used", MON.date())
    assert svc.handle_message(A, "dinner", at(0, 12)) == [messages.NO_IDEA]


def test_old_items_move_up_and_trigger_todays_nudge(svc):
    onboard(svc, A, "baby spinach, cheddar, eggs")
    assert all("Use soon" not in t for _, t in svc.tick(at(0)))                # nothing at risk on day 0, just a pick
    svc2 = Service(Engine(Store()), Config(number=NUMBER))
    onboard(svc2, A, "baby spinach, cheddar, eggs")
    moved, plan_text = svc2.handle_message(A, "OLD spinach", at(0, 10))
    assert moved == "Moved up: baby spinach." and "• baby spinach: by tomorrow" in plan_text
    sent = svc2.tick(at(0))
    assert len(sent) == 1 and "baby spinach (use by tomorrow)" in sent[0][1]   # the 4:30 text fires today
    assert "couldn't find" in svc2.handle_message(A, "old lobster", at(0, 17))[0]


def test_fallback_can_route_to_dinner(svc):
    svc.interpreter = FakeInterpreter("DINNER")
    onboard(svc, A, "chicken thighs, baby spinach")
    assert svc.handle_message(A, "no clue what to eat honestly", at(0, 12))[0].startswith("Tonight: ")
    assert validate("DINNER", 0.9) == "DINNER" and validate("OLD milk", 0.9) == "OLD milk"


def test_bud_voice_on_the_plan(svc):
    svc.handle_message(A, "hi", MON)
    onboard(svc, B, "chicken thighs, baby spinach")
    out = svc.handle_message(B, "", MON, has_media=True, captured=fridge("ground beef"))
    assert out[1].startswith("Use first:")                       # no opener line on top of the plan


# questions about what's in the fridge ------------------------------------
@pytest.mark.parametrize("question", [
    "Do I have oranges left?", "do we still have any oranges", "got any oranges?", "any oranges left?",
    "are there oranges in the fridge?", "HAVE oranges",
])
def test_do_i_have_question_is_answered_from_the_list(svc, question):
    onboard(svc, A, "oranges, chicken thighs, ketchup")
    reply = svc.handle_message(A, question, at(1, 10))[0]
    assert reply.startswith("On your list: oranges (added Mon Nov 16") and "check the fridge" in reply


def test_have_matches_food_family_and_gives_the_date(svc):
    onboard(svc, A, "chicken thighs")
    reply = svc.handle_message(A, "do I have chicken?", at(0, 10))[0]
    assert "chicken thighs (added Mon Nov 16, use by Wed)" in reply


def test_have_says_no_honestly_and_remembers_recent_use(svc):
    onboard(svc, A, "milk, baby spinach")
    assert svc.handle_message(A, "do I have eggs?", at(0, 10))[0].startswith("I don't have eggs on your list.")
    svc.handle_message(A, "used spinach", at(1, 19))
    reply = svc.handle_message(A, "any spinach left?", at(2, 10))[0]
    assert reply.startswith("Not on your list now: the baby spinach was used on Nov 17.")


def test_whats_in_my_fridge_lists_everything(svc):
    onboard(svc, A, "chicken thighs, carrots, ketchup, rice")
    reply = svc.handle_message(A, "what's in my fridge?", at(0, 10))[0]
    assert reply.splitlines()[1] == "- chicken thighs: by Wed"
    assert "carrots" in reply and "Also have (keeps well or not tracked): ketchup, rice." in reply


def test_question_mark_alone_is_still_help(svc):
    onboard(svc, A, "milk")
    assert svc.handle_message(A, "?", at(0, 10)) == [messages.HELP]


def test_asking_for_another_dinner_never_repeats_the_same_dish_unless_it_is_the_only_one(svc):
    onboard(svc, A, "chicken thighs, baby spinach, tortillas, cheddar, rotisserie chicken, cooked shrimp")
    first = svc.handle_message(A, "what's for dinner?", at(0, 12))[0]
    seen = {first.split("\n")[0]}
    for i in range(1, 4):
        nxt = svc.handle_message(A, "DINNER", at(0, 12, i))[0]
        if nxt.startswith("That's the only thing"):
            break                                                        # honest about it, not a silent repeat
        assert nxt.split("\n")[0] not in seen
        seen.add(nxt.split("\n")[0])
