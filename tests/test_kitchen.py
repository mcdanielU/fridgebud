"""Recipe book, links, shopping list, audio cook-along, the agent and Bud's voice."""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fridgebud import links, messages
from fridgebud.agent import Agent
from fridgebud.audio import Audio
from fridgebud.capture import CapturedItem
from fridgebud.chef import Chef
from fridgebud.engine import Engine
from fridgebud.recipebook import Ingredient, Recipe
from fridgebud.links import LinkError, Page
from fridgebud.server import App
from fridgebud.service import Config, Service
from fridgebud.sms import MemorySender
from fridgebud.store import Store
from fridgebud.telegram import TelegramError, TelegramSender, split_message

TZ = ZoneInfo("America/Toronto")
A, B = "+15195550101", "+15195550102"
MON = datetime(2026, 11, 16, 9, 0, tzinfo=TZ)

JOLLOF = {
    "is_recipe": True, "title": "Jollof rice", "servings": "4", "minutes": 60, "complete": True,
    "ingredients": [
        {"name": "long-grain rice", "quantity": "3 cups"}, {"name": "tomatoes", "quantity": "6"},
        {"name": "tomato paste", "quantity": "2 tbsp"}, {"name": "bell peppers", "quantity": "2"},
        {"name": "chicken thighs", "quantity": "600 g"}, {"name": "salt", "quantity": "to taste", "staple": True},
        {"name": "vegetable oil", "quantity": "1/3 cup", "staple": True}],
    "steps": ["Blend the tomatoes and peppers.", "Fry the paste, add the blend and cook down.", "Add rice and stock; steam 30 min."],
}


def at(day, hour=16, minute=30):
    return (MON + timedelta(days=day)).replace(hour=hour, minute=minute)


class FakeChef:
    def __init__(self, source=JOLLOF, generated=JOLLOF):
        self.source, self.generated, self.calls = source, generated, []

    def from_source(self, text, site):
        self.calls.append("from_source")
        return self.source

    def generate(self, dish, on_hand, dislikes=None, diet=None):
        self.calls.append(("generate", dish))
        self.last_dislikes = dislikes
        self.last_diet = diet
        return self.generated

    def match(self, ingredients, on_hand):
        return None  # fall back to the code matcher

    def narrate(self, *a):
        self.calls.append("narrate")
        return "Hello hello, Bud here in your crisper. " + "Tonight we cook jollof, and it is going to smell amazing. " * 6

    def roast(self, items):
        return f"A fridge with {len(items)} things and a dream."


def make(chef=None, agent=None, fetch=None, force_voice=""):
    svc = Service(Engine(Store()), Config(number="@bud on Telegram", force_voice=force_voice), chef=chef, agent=agent, fetch=fetch)
    for text in ("hi", "yes", "1", "bought tomatoes, bell peppers, chicken thighs, milk"):
        svc.handle_message(A, text, MON)
    return svc


def site_page(url):
    return Page(url, "example.com", "Party Jollof", "Party Jollof", recipe={
        "title": "Party Jollof Rice", "servings": "6 servings", "minutes": 75,
        "ingredients": [{"name": "3 cups long-grain rice"}, {"name": "6 plum tomatoes"}, {"name": "2 tbsp tomato paste"},
                        {"name": "salt"}],
        "steps": ["Blend the tomatoes.", "Fry the paste.", "Steam the rice."]})


def video_page(url):
    return Page(url, "youtube.com", "Smoky Party Jollof in 1 hour", "Smoky Party Jollof in 1 hour\nMy grandma's way!")


# links --------------------------------------------------------------------
def test_recipe_site_link_is_copied_without_ai_and_checked_against_the_fridge():
    svc = make(fetch=site_page)                                   # no chef at all
    card = svc.handle_message(A, "https://example.com/jollof", at(0, 12))[0]
    assert card.startswith("Saved to your recipes.\n📖 Party Jollof Rice\n🔗 https://example.com/jollof\n6 servings")
    assert "6 servings · 75 min" in card
    assert "🧊 Checked against your last update (Mon Nov 16, 9:00 AM)" in card
    assert "🛒 Almost there. Just missing: 3 cups long-grain rice, 2 tbsp tomato paste" in card
    assert "✅ You have: tomatoes" in card
    assert "Assumes you have: salt." in card
    assert "AI" not in card                                        # nothing here was written by a model
    again = svc.handle_message(A, "look https://example.com/jollof", at(0, 13))[0]
    assert again.startswith("You've saved this one already.") and len(svc.book.all(1)) == 1


def test_video_link_says_ai_filled_the_gaps():
    chef = FakeChef(source=dict(JOLLOF, complete=False))
    svc = make(chef=chef, fetch=video_page)
    card = svc.handle_message(A, "https://youtu.be/abc123", at(0, 12))[0]
    assert "🔗 https://youtu.be/abc123\n" + messages.LINK_PARTIAL in card
    assert "✅ You have: tomatoes, bell peppers, chicken thighs" in card
    assert chef.calls == ["from_source"]


def test_link_that_cannot_be_read_asks_for_the_text():
    def blocked(url):
        raise LinkError("the site answered 403")
    svc = make(chef=FakeChef(), fetch=blocked)
    reply = svc.handle_message(A, "https://www.instagram.com/reel/xyz/", at(0, 12))[0]
    assert reply.startswith("instagram.com blocks automated readers like me (it answered 403)")
    assert "screenshot" in reply and "paste the text" in reply and "RECIPE" not in reply   # no dish name in a reel link
    assert svc.book.all(1) == []


def test_link_that_times_out_asks_for_the_text():
    def dead(url):
        raise LinkError("the site didn't answer")
    svc = make(chef=FakeChef(), fetch=dead)
    reply = svc.handle_message(A, "https://example.org/some-recipe", at(0, 12))[0]
    assert "couldn't read that link (example.org: the site didn't answer)" in reply and "Paste the caption" in reply


def test_link_with_no_dish_in_it():
    svc = make(chef=FakeChef(source=None), fetch=video_page)
    assert "couldn't find a dish" in svc.handle_message(A, "https://youtu.be/cats", at(0, 12))[0]


def test_only_public_web_addresses_are_fetched():
    for url in ("http://localhost:8080/x", "http://127.0.0.1/x", "http://192.168.1.1/admin", "ftp://example.com/x"):
        with pytest.raises(LinkError):
            links._check_public(url)


def test_reading_pages():
    ld = ('<script type="application/ld+json">{"@graph":[{"@type":"WebSite"},{"@type":["Recipe"],"name":"Egusi &amp; Spinach",'
          '"recipeYield":"4","totalTime":"PT1H5M","recipeIngredient":["2 cups egusi","spinach"],'
          '"recipeInstructions":[{"@type":"HowToSection","itemListElement":[{"@type":"HowToStep","text":"Toast."}]},"Simmer."]}]}</script>')
    recipe = links.read_page("https://www.site.com/egusi", ld).recipe
    assert recipe["title"] == "Egusi & Spinach" and recipe["minutes"] == 65 and recipe["steps"] == ["Toast.", "Simmer."]
    yt = '<meta property="og:title" content="Easy Jollof"><script>{"shortDescription":"3 cups rice\\n\\"smoky\\"","x":1}</script>'
    page = links.read_page("https://www.youtube.com/watch?v=1", yt)
    assert page.site == "youtube.com" and page.text == 'Easy Jollof\n3 cups rice\n"smoky"' and page.recipe is None
    assert links.first_url("try this https://vm.tiktok.com/ZM123/.") == "https://vm.tiktok.com/ZM123/"


# recipe lookup ------------------------------------------------------------
def test_saved_recipe_is_found_before_ai_writes_one():
    chef = FakeChef()
    svc = make(chef=chef, fetch=site_page)
    svc.handle_message(A, "https://example.com/jollof", at(0, 12))
    card = svc.handle_message(A, "can you fetch jollof rice recipe", at(0, 13))[0]
    assert card.startswith("Found it in your saved recipes.\n📖 Party Jollof Rice")
    assert chef.calls == []                                         # no AI involved


def test_missing_recipe_is_ai_written_and_says_so():
    chef = FakeChef()
    svc = make(chef=chef)
    card = svc.handle_message(A, "can you fetch jollof rice recipe?", at(0, 13))[0]
    assert card.startswith("You haven't saved a jollof rice recipe, so I wrote one.\n📖 Jollof rice\n" + messages.AI_LABEL)
    assert chef.calls == [("generate", "jollof rice")]
    again = svc.handle_message(A, "jollof rice recipe", at(0, 14))[0]
    assert again.startswith("I wrote this one for you before.") and messages.AI_LABEL in again   # still labelled
    assert len(chef.calls) == 1
    # untested AI recipes don't clutter the list until the household says they're a keeper
    listing = svc.handle_message(A, "recipes", at(0, 14, 1))[0]
    assert "Jollof rice" not in listing
    liked = svc.handle_message(A, "liked jollof rice", at(0, 15))[0]
    assert "More like Jollof rice coming up" in liked
    listing = svc.handle_message(A, "recipes", at(0, 15, 1))[0]
    assert "• Jollof rice (AI-generated)" in listing


def test_dislike_is_learned_and_reaches_ai_generated_recipes():
    chef = FakeChef()
    svc = make(chef=chef)
    ack = svc.handle_message(A, "dislike mushrooms", at(0, 12))[0]
    assert "no more mushrooms" in ack
    svc.handle_message(A, "can you fetch jollof rice recipe?", at(0, 13))
    assert chef.last_dislikes == ["mushrooms"]
    # saying it again doesn't duplicate the stored preference
    again = svc.handle_message(A, "dislike mushrooms", at(0, 14))[0]
    assert "Tell me what to skip" in again


def test_diet_notes_are_stored_and_reach_ai_generated_recipes():
    chef = FakeChef()
    svc = make(chef=chef)
    ack = svc.handle_message(A, "diet vegetarian, no pork, loves Nigerian and Thai food", at(0, 12))[0]
    assert "Got it, I'll cook with that in mind" in ack
    svc.handle_message(A, "can you fetch jollof rice recipe?", at(0, 13))
    assert "vegetarian" in chef.last_diet and "no pork" in chef.last_diet


def test_liked_undoes_a_dislike_and_bare_commands_ask_for_a_name():
    svc = make()
    assert "no more mushrooms" in svc.handle_message(A, "dislike mushrooms", at(0, 12))[0]
    assert "Tell me what you liked" in svc.handle_message(A, "loved it", at(0, 12, 1))[0]
    ack = svc.handle_message(A, "loved mushrooms", at(0, 13))[0]
    assert "Back on the menu: mushrooms" in ack


def test_search_matches_a_cuisine_phrase_against_a_real_saved_recipe():
    svc = make()
    svc.book.save(Recipe(household_id=1, title="Nigerian Fried Rice", source="video",
                         ingredients=[Ingredient(name="rice", quantity="")], steps=["Fry it."]), at(0, 10))
    hit = svc.book.search(1, "a nigerian delicacy")
    assert hit is not None and hit.title == "Nigerian Fried Rice"


def test_dislike_on_an_unconfirmed_ai_recipe_bins_it_but_a_kept_one_survives():
    chef = FakeChef()
    svc = make(chef=chef)
    svc.handle_message(A, "recipe jollof rice", at(0, 12))                    # written, unconfirmed
    out = svc.handle_message(A, "dislike jollof rice", at(0, 12, 1))[0]
    assert "Binned the Jollof rice" in out
    assert svc.book.search(1, "jollof rice") is None                         # really gone
    svc.handle_message(A, "recipe jollof rice", at(0, 13))                   # written again, unconfirmed
    svc.handle_message(A, "liked jollof rice", at(0, 13, 1))                 # now kept
    out2 = svc.handle_message(A, "dislike jollof rice", at(0, 14))[0]
    assert "no more jollof rice" in out2                         # falls through to the plain list, not binned
    assert svc.book.search(1, "jollof rice") is not None


def test_recipe_without_ai_key_is_honest():
    svc = make()
    assert svc.handle_message(A, "recipe egusi soup", at(0, 13)) == [messages.NEEDS_AI]


def test_fridge_date_is_flagged_when_stale():
    svc = make(chef=FakeChef())
    card = svc.handle_message(A, "recipe jollof rice", at(5, 13))[0]
    assert "against your last update (Mon Nov 16, 9:00 AM)\n(That was 5 days ago. Send a new photo if things have changed.)" in card


def test_as_of_names_the_photo():
    svc = make(chef=FakeChef())
    svc.handle_message(A, "", at(1, 23, 52), has_media=True, captured=[CapturedItem("eggs")])
    card = svc.handle_message(A, "recipe jollof rice", at(2, 9))[0]
    assert "against your last photo (Tue Nov 17, 11:52 PM)" in card


# cook, shop, made ---------------------------------------------------------
def test_cook_shop_and_made_flow():
    svc = make(chef=FakeChef())
    svc.handle_message(A, "recipe jollof rice", at(0, 13))
    steps = svc.handle_message(A, "COOK", at(0, 13))[0]
    assert "• 3 cups long-grain rice" in steps and "3. Add rice and stock; steam 30 min." in steps and messages.AI_LABEL in steps

    added = svc.handle_message(A, "add them to my shopping list", at(0, 13))[0]     # parser would read this as a purchase
    assert added.startswith("🛒 Added to your shopping list:\n\n• long-grain rice\n• tomato paste")
    assert all("shopping list" not in i.name for i in svc.store.active_items(1))
    assert "• tomato paste" in svc.handle_message(A, "what do I need to buy?", at(0, 13))[0]
    svc.handle_message(A, "add scotch bonnet to my list", at(0, 13))
    assert svc.book.shopping(1) == ["long-grain rice", "tomato paste", "scotch bonnet"]

    replies = svc.handle_message(A, "bought rice, tomato paste", at(1, 10))
    assert "✔️ Ticked off: long-grain rice, tomato paste" in replies
    assert svc.book.shopping(1) == ["scotch bonnet"]

    svc.handle_message(A, "recipe jollof rice", at(1, 17))
    done = svc.handle_message(A, "made it", at(1, 19))[0]
    assert "tomatoes" in done and "chicken thighs" in done
    left = {i.name for i in svc.store.active_items(1)}
    assert "tomatoes" not in left and "milk" in left and "rice" in left            # staples and unused food stay


def test_cook_without_a_recipe_falls_back_to_a_dinner_idea():
    svc = make()
    assert svc.handle_message(A, "cook", at(0, 13))[0].startswith("🍽️ Tonight: ")
    assert svc.handle_message(A, "audio", at(0, 13)) == [messages.NO_CURRENT]


# audio --------------------------------------------------------------------
def test_audio_cook_along_is_sent_as_a_voice_file():
    chef = FakeChef()
    svc = make(chef=chef)
    svc.handle_message(A, "recipe jollof rice", at(0, 13))
    out = svc.handle_message(A, "AUDIO", at(0, 13))
    assert isinstance(out[0], Audio) and out[0].title == "Cook-along: Jollof rice" and "Bud here" in out[0].script
    assert "Script written by AI" in out[0].caption

    uploads, texts = [], []
    sender = TelegramSender(call=lambda m, p, t=20: texts.append(p) or {}, download=lambda p: b"",
                            upload=lambda method, fields, name, data, mime: uploads.append((method, fields, name, data, mime)))
    app = App(svc, sender, synth=lambda script: b"M4A-BYTES")
    app.send("tg:7", out[0])
    assert uploads[0][0] == "sendAudio" and uploads[0][3] == b"M4A-BYTES" and uploads[0][1]["chat_id"] == "7"

    app = App(svc, sender, synth=lambda script: None)               # e.g. not on a Mac
    app.send("tg:7", out[0])
    assert "can't make audio on this device" in texts[-1]["text"] and "Bud here" in texts[-1]["text"]


def test_audio_without_ai_reads_the_recipe_straight():
    svc = make(fetch=site_page)
    svc.handle_message(A, "https://example.com/jollof", at(0, 12))
    audio = svc.handle_message(A, "read it to me", at(0, 13))[0]
    assert audio.script.startswith("Let's cook Party Jollof Rice.") and "Step 2. Fry the paste." in audio.script
    assert "AI" not in audio.caption


def test_long_messages_are_split_for_telegram():
    parts = split_message("\n".join(f"line {i} " + "x" * 90 for i in range(100)), limit=1000)
    assert len(parts) > 5 and all(len(p) <= 1000 for p in parts)
    assert split_message("short") == ["short"]


# 4:30 text uses their own recipes ----------------------------------------
def test_daily_text_points_at_a_saved_recipe_that_uses_what_is_turning():
    svc = make(fetch=site_page)
    svc.handle_message(A, "https://example.com/jollof", at(0, 12))
    svc.handle_message(A, "old tomatoes", at(0, 13))
    text = svc.tick(at(0))[0][1]
    assert "2️⃣ your saved Party Jollof Rice ⭐" in text
    assert text.rstrip().endswith("👍 cook it\n2️⃣ your saved Party Jollof Rice ⭐\n👎 another idea")
    card = svc.handle_message(A, "2", at(0, 16, 40))[0]                  # one character instead of the whole title
    assert "📖 Party Jollof Rice" in card and svc.store.events(1, "locked")


# Bud's voice --------------------------------------------------------------
def test_everyone_gets_bud_when_forced():
    svc = make(force_voice="bud")                                   # household 1 would be 'plain' in the 50/50 test
    assert svc.accounts.get(1).voice == "plain"
    assert svc.handle_message(A, "do I have eggs?", at(0, 12))[0].startswith("I don't have eggs")
    assert svc.handle_message(A, "dinner", at(0, 12))[0].startswith("🍽️ Tonight: ")      # no extra opener line
    missed = svc.handle_message(A, "blorp zzz", at(0, 12))[0]
    assert "didn't catch that" in missed and len(missed) < 140                       # short, no help dump
    assert svc.handle_message(A, "help", at(0, 12)) == [messages.HELP]                # HELP itself stays exact
    assert svc.handle_message(A, "stop", at(0, 12)) == [messages.STOPPED]


def test_roast():
    svc = make(chef=FakeChef())
    assert svc.handle_message(A, "roast my fridge", at(0, 12)) == ["🔥 A fridge with 4 things and a dream."]


# the agent ----------------------------------------------------------------
def scripted(*turns):
    """A fake Anthropic API that plays back the given assistant turns and records what it was sent."""
    seen, queue = [], list(turns)

    def transport(url, headers, body):
        seen.append(json.loads(body))
        return {"content": queue.pop(0)}
    return transport, seen


def tool(name, **args):
    return {"type": "tool_use", "id": f"t{name}{len(args)}", "name": name, "input": args}


def test_agent_chains_tools_for_a_free_form_request():
    transport, seen = scripted(
        [tool("get_fridge")],
        [tool("create_recipe", request="a quick dinner with the chicken thighs")],
        [tool("run_command", command="SHOP")],
        [{"type": "text", "text": "Sorted. The chicken's big night is tonight 🍗"}])
    svc = make(chef=FakeChef(), agent=Agent("k", transport=transport))
    out = svc.handle_message(A, "I'm wiped. Something quick with the chicken, and put whatever I'm missing on my list", at(0, 17))
    assert out[0].startswith("Here's one I wrote for that.\n📖 Jollof rice\n" + messages.AI_LABEL)   # card made by code, label intact
    assert out[1].startswith("Untested recipe")   # AI recipe invites a LIKED/DISLIKE rating
    assert out[2].startswith("🛒 Added to your shopping list:\n\n• long-grain rice\n• tomato paste")
    assert out[3] == "Sorted. The chicken's big night is tonight 🍗"
    assert svc.book.shopping(1) == ["long-grain rice", "tomato paste"]
    fridge_result = seen[1]["messages"][-1]["content"][0]["content"]
    assert "chicken thighs: by Wed" in fridge_result and "List last updated: your last update (Mon Nov 16, 9:00 AM)" in fridge_result
    assert seen[0]["system"].count("never say a food is safe") == 1 or "Never say a food is safe" in seen[0]["system"]
    assert svc.store.events(1, "agent")[0]["payload"]["tools"] == ["get_fridge", "create_recipe", "run_command"]


def test_agent_just_chats_in_character():
    transport, _ = scripted([{"type": "text", "text": "Living my best life between the mustard and the milk. What are we cooking?"}])
    svc = make(agent=Agent("k", transport=transport))
    assert svc.handle_message(A, "hey bud how are you", at(0, 12)) == ["Living my best life between the mustard and the milk. What are we cooking?"]


def test_agent_cannot_opt_in_and_can_stop():
    transport, seen = scripted([tool("run_command", command="START")], [tool("run_command", command="STOP")],
                               [{"type": "text", "text": ""}])
    svc = make(agent=Agent("k", transport=transport))
    out = svc.handle_message(A, "please stop messaging me, I'm done with this", at(0, 12))
    assert seen[1]["messages"][-1]["content"][0]["content"] == "That isn't a command I can run."
    assert out == [messages.STOPPED] and not svc.store.get_household_by_phone(A).opted_in


def test_agent_outage_falls_back_politely():
    def down(url, headers, body):
        raise RuntimeError("529 overloaded")
    svc = make(agent=Agent("k", transport=down), force_voice="bud")
    assert "didn't catch that" in svc.handle_message(A, "tell me a story", at(0, 12))[0]
    assert svc.store.events(1, "agent_error")


def test_commands_never_reach_the_agent():
    transport, seen = scripted()
    svc = make(agent=Agent("k", transport=transport))
    for text in ("list", "dinner", "do I have milk?", "made it", "bought eggs", "shopping"):
        svc.handle_message(A, text, at(0, 12))
    assert seen == []


def test_chef_parses_model_output():
    def transport(url, headers, body):
        sent = json.loads(body)["messages"][0]["content"]
        if "For each ingredient" in sent:
            return {"content": [{"type": "text", "text": '{"matches": ["Tomatoes", null, "not in list"]}'}]}
        return {"content": [{"type": "text", "text": "Sure!\n" + json.dumps(JOLLOF)}]}
    chef = Chef("k", transport=transport)
    assert chef.generate("jollof", ["tomatoes"])["title"] == "Jollof rice"
    assert chef.match(["tomatoes", "rice", "stock"], ["tomatoes", "milk"]) == ["tomatoes", None, None]


# watching videos ----------------------------------------------------------
from fridgebud.video import GeminiWatcher, youtube_id  # noqa: E402

NOTES = "DISH: Smoky party jollof\nINGREDIENTS:\n3 cups rice\ntomato paste - amount not given\nSTEPS:\n1. Blend..."


class FakeWatcher:
    def __init__(self, notes=NOTES, fail=False):
        self.notes, self.fail, self.calls = notes, fail, []

    def watch_youtube(self, url):
        self.calls.append(("youtube", url))
        if self.fail:
            raise RuntimeError("quota")
        return self.notes

    def watch_file(self, data, mime):
        self.calls.append(("file", len(data), mime))
        return self.notes


class NotesChef(FakeChef):
    def from_source(self, text, site):
        self.calls.append(("from_source", text))
        return self.source


def test_youtube_link_is_watched_not_guessed():
    chef, watcher = NotesChef(), FakeWatcher()
    svc = make(chef=chef, fetch=video_page)
    svc.watcher = watcher
    card = svc.handle_message(A, "https://youtu.be/abc12345?si=x", at(0, 12))[0]
    assert "🔗 https://youtu.be/abc12345\n" + messages.WATCHED in card   # share tracker ?si= dropped and messages.LINK_PARTIAL not in card
    assert watcher.calls == [("youtube", "https://youtu.be/abc12345?si=x")]
    assert "amount not given" in chef.calls[0][1] and chef.calls[0][1].startswith("Smoky Party Jollof in 1 hour")
    assert svc.store.events(1, "recipe_saved")[0]["payload"]["watched"] is True


def test_watched_video_with_gaps_says_ai_filled_them():
    svc = make(chef=NotesChef(source=dict(JOLLOF, complete=False)), fetch=video_page)
    svc.watcher = FakeWatcher()
    assert messages.WATCHED_GAPS in svc.handle_message(A, "https://www.youtube.com/shorts/abc12345", at(0, 12))[0]


def test_if_watching_fails_it_falls_back_to_the_caption_and_says_so():
    svc = make(chef=FakeChef(source=dict(JOLLOF, complete=False)), fetch=video_page)
    svc.watcher = FakeWatcher(fail=True)
    card = svc.handle_message(A, "https://youtu.be/abc12345", at(0, 12))[0]
    assert messages.LINK_PARTIAL in card and messages.WATCHED not in card
    assert svc.store.events(1, "watch_error")


def test_non_youtube_links_are_not_sent_to_the_watcher():
    svc = make(chef=FakeChef(), fetch=site_page)
    svc.watcher = FakeWatcher()
    svc.handle_message(A, "https://example.com/jollof", at(0, 12))
    assert svc.watcher.calls == []


def test_video_file_sent_in_chat_is_watched_and_saved():
    from fridgebud.telegram import handle_update
    svc = make(chef=NotesChef())
    svc.watcher = FakeWatcher()
    svc.handle_message("tg:9", "hi", MON)
    svc.handle_message("tg:9", "yes", MON)
    sent = []
    sender = TelegramSender(call=lambda m, p, t=20: (sent.append((m, p)) or {"file_path": "videos/v.mp4", "file_size": 2_000_000}),
                            download=lambda path, max_bytes=0: b"\x00" * 2_000_000, upload=lambda *a: None)
    app = App(svc, sender, clock=lambda: at(0, 12))
    handle_update(app, {"update_id": 5, "message": {"chat": {"id": 9, "type": "private"},
                                                    "video": {"file_id": "vid1", "mime_type": "video/mp4", "file_size": 2_000_000}}})
    assert svc.watcher.calls == [("file", 2_000_000, "video/mp4")]
    card = [p["text"] for m, p in sent if m == "sendMessage"][-1]
    assert "📖 Jollof rice\n" + messages.WATCHED in card
    assert svc.book.all(svc.store.get_household_by_phone("tg:9").id)[0].source == "video"

    big = TelegramSender(call=lambda m, p, t=20: (sent.append((m, p)) or {"file_path": "v.mp4", "file_size": 19_000_000}),
                         download=lambda path, max_bytes=0: b"", upload=lambda *a: None)
    App(svc, big, clock=lambda: at(0, 12)).inbound("tg:9", "", [("vid2", "video/mp4")])
    assert sent[-1][1]["text"] == messages.VIDEO_TOO_BIG

    svc.watcher = None
    app.inbound("tg:9", "", [("vid3", "video/mp4")])
    assert sent[-1][1]["text"] == messages.VIDEO_NEEDS_KEY


def test_gemini_request_shape_and_ids():
    seen = {}

    def transport(url, headers, body):
        seen.update(url=url, headers=headers, body=json.loads(body))
        return {"candidates": [{"content": {"parts": [{"text": "DISH: Egusi\nSTEPS: ..."}]}}]}
    watcher = GeminiWatcher("g-key", transport=transport, model="gemini-test")
    assert watcher.watch_youtube("https://youtu.be/dQw4w9WgXcQ?t=3").startswith("DISH: Egusi")
    assert seen["url"].endswith("/models/gemini-test:generateContent") and seen["headers"]["x-goog-api-key"] == "g-key"
    assert seen["body"]["contents"][0]["parts"][0] == {"file_data": {"file_uri": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}}
    watcher.watch_file(b"abc", "video/quicktime")
    assert seen["body"]["contents"][0]["parts"][0]["inline_data"] == {"mime_type": "video/quicktime", "data": "YWJj"}
    assert GeminiWatcher("k", transport=lambda *a: {"candidates": [{"content": {"parts": [{"text": "NOT A RECIPE"}]}}]}).watch_youtube("https://youtu.be/dQw4w9WgXcQ") is None
    assert youtube_id("https://www.youtube.com/shorts/Ab_c-123XYZ") == "Ab_c-123XYZ"
    assert youtube_id("https://m.youtube.com/watch?app=desktop&v=Ab_c-123XYZ&t=1") == "Ab_c-123XYZ"
    assert youtube_id("https://www.tiktok.com/@a/video/123") is None
    with pytest.raises(ValueError):
        watcher.watch_file(b"x" * (15 * 1024 * 1024))


# sites that block bots, and recipe screenshots ----------------------------
from fridgebud.capture import FixedExtractor  # noqa: E402

SHOT_1 = "My Favorite Banana Bread\nIngredients:\n4 ripe bananas\n2 cups flour\n1/2 cup butter\n3/4 cup brown sugar"
SHOT_2 = "Instructions:\n1. Mash the bananas.\n2. Mix and bake at 350F for 60 minutes."
BREAD = {"is_recipe": True, "title": "Banana bread", "servings": "1 loaf", "minutes": 75,
         "ingredients": [{"name": "ripe bananas", "quantity": "4"}, {"name": "flour", "quantity": "2 cups"},
                         {"name": "butter", "quantity": "1/2 cup"}, {"name": "brown sugar", "quantity": "3/4 cup"}],
         "steps": ["Mash the bananas.", "Mix and bake at 350F for 60 minutes."]}


class ShotChef(FakeChef):
    """Reads whatever 'screenshot' text the test hands it; a recipe is complete once it has seen the steps."""

    def read_image(self, image, mime):
        return image.decode() or None

    def from_source(self, text, site):
        self.calls.append(("from_source", text))
        return dict(BREAD, complete="Instructions" in text)


class KindExtractor:
    def __init__(self, kind, items=()):
        self.kind, self.items = kind, [CapturedItem(i) for i in items]

    def extract_with_kind(self, image, mime):
        return self.kind, list(self.items)

    def extract(self, image, mime="image/jpeg"):
        return list(self.items)


class ChatSender:
    """A chat channel that hands over the photos it is given, in order."""

    def __init__(self, blobs):
        self.sent, self.blobs = [], list(blobs)

    def send(self, to, body):
        self.sent.append(body)

    def fetch_media(self, ref, max_bytes=None):
        return self.blobs.pop(0)


def shot_app(svc, extractor, blobs):
    sender = ChatSender(blobs)
    return App(svc, sender, extractor, clock=lambda: at(0, 12)), sender.sent


def test_blocked_site_explains_and_offers_screenshots():
    def blocked(url):
        raise LinkError("the site answered 402")
    svc = make(chef=ShotChef(), fetch=blocked)
    reply = svc.handle_message(A, "Add this banana bread to my recipe list too https://www.allrecipes.com/recipe/20144/banana-banana-bread/", at(0, 12))[0]
    assert reply.startswith("allrecipes.com blocks automated readers like me (it answered 402)")
    assert "send me a screenshot" in reply and "reply RECIPE banana bread" in reply
    assert svc.wants_recipe_photo(A, "", at(0, 12, 5)) and not svc.wants_recipe_photo(A, "", at(0, 13))
    assert links.dish_from_url("https://sallysbakingaddiction.com/best-banana-bread-recipe/") == "banana bread"


def test_two_recipe_screenshots_are_merged_into_one_recipe():
    def blocked(url):
        raise LinkError("the site answered 403")
    chef = ShotChef()
    svc = make(chef=chef, fetch=blocked)
    svc.handle_message(A, "https://sallysbakingaddiction.com/best-banana-bread-recipe/", at(0, 12))
    app, sent = shot_app(svc, FixedExtractor(["should", "not", "be", "used"]), [SHOT_1.encode(), SHOT_2.encode()])

    app.inbound(A, "", [("f1", "image/jpeg")])
    assert "📖 Banana bread\nFrom your screenshot. " + messages.SHOT_NOTE in sent[0]
    assert sent[1] == messages.SHOT_MORE

    app.inbound(A, "", [("f2", "image/jpeg")])
    assert sent[2].startswith("Added your next screenshot. Here's the full recipe now.\n📖 Banana bread\nFrom your screenshot.")
    assert messages.SHOT_NOTE not in sent[2] and len(sent) == 3                 # complete now, nothing AI-filled
    assert "Ingredients" in chef.calls[-1][1] and "Instructions" in chef.calls[-1][1]
    assert [r.title for r in svc.book.all(1)] == ["Banana bread"]               # one recipe, not two
    assert all(i.name not in {"should", "not", "be", "used", "flour"} for i in svc.store.active_items(1))


def test_recipe_screenshot_sent_cold_is_not_mistaken_for_groceries():
    svc = make(chef=ShotChef())
    app, sent = shot_app(svc, KindExtractor("recipe"), [(SHOT_1 + "\n" + SHOT_2).encode()])
    app.inbound(A, "", [("f1", "image/jpeg")])
    assert "📖 Banana bread" in sent[0] and len(svc.book.all(1)) == 1
    assert len(svc.store.active_items(1)) == 4                                   # the fridge is untouched


def test_fridge_photo_still_goes_to_the_fridge():
    svc = make(chef=ShotChef())
    app, sent = shot_app(svc, KindExtractor("fridge", ["eggs", "yogurt"]), [b"jpeg"])
    app.inbound(A, "", [("f1", "image/jpeg")])
    assert sent[0].startswith("Got it: eggs, yogurt") and svc.book.all(1) == []


def test_photo_with_recipe_caption_and_a_non_recipe_picture():
    svc = make(chef=ShotChef())
    app, sent = shot_app(svc, KindExtractor("other"), [b"x"])
    svc.chef.read_image = lambda image, mime: None
    app.inbound(A, "save this recipe", [("f1", "image/jpeg")])
    assert sent == [messages.SHOT_NOT_RECIPE]


def test_vision_reply_kind_is_read():
    from fridgebud.capture import ClaudeVisionExtractor
    def transport(url, headers, body):
        return {"content": [{"type": "text", "text": '{"kind": "recipe", "store": null, "items": []}'}]}
    assert ClaudeVisionExtractor("k", transport=transport).extract_with_kind(b"x") == ("recipe", [])


def test_album_of_screenshots_gets_one_reply_in_page_order():
    import time
    chef = ShotChef()
    svc = make(chef=chef)
    sender = ChatSender([SHOT_2.encode(), b"Notes: store for 3 days", SHOT_1.encode()])   # arrive out of order
    app = App(svc, sender, KindExtractor("recipe"), clock=lambda: at(0, 12))
    app.shot_delay = 0.15
    for order in (3, 4, 2):                                   # Telegram message ids: page 2, page 3, page 1
        app.inbound(A, "", [(f"f{order}", "image/jpeg")], order=order)
    assert sender.sent == []                                  # nothing until the album has settled
    time.sleep(0.6)
    assert len(sender.sent) == 1 and sender.sent[0].startswith("Saved to your recipes.\n📖 Banana bread")
    read = chef.calls[-1][1]
    assert read.index("Ingredients") < read.index("Instructions") < read.index("Notes")
    assert len(chef.calls) == 1 and len(svc.book.all(1)) == 1  # one extraction, one recipe


def test_script_is_made_speakable_and_gemini_voice_request():
    import base64, io, wave
    from fridgebud.audio import gemini_wav, speakable
    said = speakable("Add 1 1/2 cups (190g) flour , 1/2 tsp baking soda , then bake at 350°F 🎉 — easy!")
    assert said == "Add 1 and a half cups, 190 grams, flour, half teaspoons baking soda, then bake at 350 degrees Fahrenheit easy!"
    assert " ," not in said

    seen = []
    def transport(url, headers, body):
        seen.append((url, json.loads(body)))
        if "3.1" in url:
            raise RuntimeError("404 model not found")           # falls back to the older model
        return {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "audio/L16", "data": base64.b64encode(b"\x00\x01" * 2400).decode()}}]}}]}
    wav = gemini_wav("Hello from the crisper.", "g-key", transport=transport, voice="Puck")
    with wave.open(io.BytesIO(wav)) as wf:
        assert (wf.getframerate(), wf.getnchannels(), wf.getsampwidth(), wf.getnframes()) == (24000, 1, 2, 2400)
    assert [u.split("/models/")[1] for u, _ in seen] == ["gemini-3.1-flash-tts-preview:generateContent"] * 2 + ["gemini-2.5-flash-preview-tts:generateContent"]
    config = seen[-1][1]["generationConfig"]
    assert config["responseModalities"] == ["AUDIO"] and config["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Puck"
    assert "Hello from the crisper." in seen[-1][1]["contents"][0]["parts"][0]["text"]


# menus and Instacart ------------------------------------------------------
class MenuChef(FakeChef):
    def __init__(self, place, dishes):
        super().__init__()
        self.place, self.dishes = place, dishes

    def read_menu(self, image, mime):
        return self.place, self.dishes


MENU = [{"name": "Butter Chicken", "description": "creamy tomato curry"}, {"name": "Lamb Biryani", "description": ""},
        {"name": "Paneer Tikka", "description": "grilled"}]


def test_menu_with_several_dishes_asks_which_before_writing_anything():
    chef = MenuChef("Tandoori Flame", MENU)
    svc = make(chef=chef)
    app, sent = shot_app(svc, KindExtractor("menu"), [b"menu"])
    app.inbound(A, "", [("f1", "image/jpeg")])
    assert sent == ["I can see 3 dishes at Tandoori Flame. Which one should I turn into a home recipe?\n"
                    "1. Butter Chicken\n2. Lamb Biryani\n3. Paneer Tikka\nReply with the number."]
    assert chef.calls == [] and svc.book.all(1) == [] and len(svc.store.active_items(1)) == 4   # nothing generated, fridge untouched

    card = svc.handle_message(A, "1", at(0, 12, 2))[0]
    assert card.startswith("Here's my home version of Butter Chicken from Tandoori Flame. It's my own take on the dish, "
                           "not the restaurant's recipe.\n📖 Jollof rice\n" + messages.AI_LABEL)
    assert chef.calls == [("generate", "Butter Chicken (creamy tomato curry), as a home-kitchen version of the restaurant dish")]
    assert svc.handle_message(A, "2", at(0, 12, 3)) != card and len(chef.calls) == 1            # the question is closed


def test_single_dish_still_asks_and_yes_does_not_resubscribe_or_break():
    chef = MenuChef("", [{"name": "Pad Thai", "description": "rice noodles, tamarind"}])
    svc = make(chef=chef)
    app, sent = shot_app(svc, KindExtractor("other"), [b"x"])
    app.inbound(A, "from the uber eats menu", [("f1", "image/jpeg")])                          # caption says menu
    assert sent[0].startswith("I can see Pad Thai (rice noodles, tamarind). Want me to write a home version")
    assert svc.handle_message(A, "yes", at(0, 12, 1))[0].startswith("Here's my home version of Pad Thai.")
    assert svc.handle_message(A, "yes", at(0, 12, 2)) == [messages.ALREADY_IN]                  # not the sign-up blurb again


def test_menu_pick_by_name_ignore_and_expiry():
    svc = make(chef=MenuChef("", MENU))
    svc.menu_seen(A, "", MENU, at(0, 12))
    assert svc.handle_message(A, "list", at(0, 12, 1))[0].startswith(("At risk", "Nothing at risk"))  # other messages still work
    assert svc.handle_message(A, "the biryani", at(0, 12, 2))[0].startswith("Sorry") is False
    svc.menu_seen(A, "", MENU, at(0, 13))
    assert svc.handle_message(A, "paneer tikka", at(0, 13, 1))[0].startswith("Here's my home version of Paneer Tikka.")
    svc.menu_seen(A, "", MENU, at(0, 14))
    assert not svc.handle_message(A, "2", at(0, 15))[0].startswith("Here's my home version")   # 15-minute window passed
    app, sent = shot_app(make(chef=MenuChef("", [])), KindExtractor("menu"), [b"m"])
    app.inbound(A, "", [("f", "image/jpeg")])
    assert sent == [messages.MENU_NONE]


def _instacart(seen, retailers=("Walmart", "No Frills", "T&T Supermarket")):
    from fridgebud.instacart import Instacart

    def transport(url, headers, body):
        seen.setdefault("posts", []).append((url, headers, json.loads(body)))
        return {"products_link_url": "https://www.instacart.ca/store/shopping_lists/123"}

    def getter(url, headers):
        seen.setdefault("gets", []).append(url)
        return {"retailers": [{"name": n, "retailer_key": n.lower()} for n in retailers]}
    return Instacart("ic-key", transport=transport, getter=getter)


def test_order_shows_the_list_for_a_yes_before_anything_goes_to_instacart():
    seen = {}
    svc = make()
    svc.instacart = _instacart(seen)
    svc.handle_message(A, "add rice, eggs and fish sauce to my list", at(0, 12))
    assert "Instacart" not in " ".join(svc.handle_message(A, "shopping list", at(0, 12)))  # SHOPPING never sends anything
    review = svc.handle_message(A, "order groceries", at(0, 12, 1))[0]
    assert review.startswith("🛒 Before I send this to Instacart, check your list (3):") and "✅ to send it" in review
    assert "posts" not in seen                                                     # nothing sent yet
    edited = svc.handle_message(A, "remove eggs", at(0, 12, 2))[0]
    assert edited.startswith("Removed eggs ✂️") and "• eggs" not in edited
    added = svc.handle_message(A, "add milk", at(0, 12, 3))[0]
    assert added.startswith("Added milk ✍️") and "• milk" in added
    assert svc.handle_message(A, "✅", at(0, 12, 4)) == [messages.ASK_POSTAL]      # first order: where are you?
    assert "posts" not in seen
    sent = svc.handle_message(A, "it's N2G 1A1", at(0, 12, 5))[0]
    assert sent.startswith("✅ Sent to Instacart.") and sent.buttons == [(messages.ORDER_BUTTON, "https://www.instacart.ca/store/shopping_lists/123")]
    assert "🏪 Most likely to have most of it: Walmart" in sent
    assert "🧺 fish sauce: try T&T Supermarket" in sent and "best guess" in sent
    url, headers, body = seen["posts"][0]
    assert url == "https://connect.instacart.com/idp/v1/products/products_link" and headers["Authorization"] == "Bearer ic-key"
    assert sorted(i["name"] for i in body["line_items"]) == ["fish sauce", "milk", "rice"]
    assert "postal_code=N2G+1A1&country_code=CA" in seen["gets"][0]
    again = svc.handle_message(A, "order", at(1, 12))                              # postal code remembered next time
    assert svc.handle_message(A, "yes", at(1, 12, 1))[0].startswith("✅ Sent to Instacart.")


def test_order_can_be_cancelled_and_other_messages_still_work():
    seen = {}
    svc = make()
    svc.instacart = _instacart(seen)
    svc.handle_message(A, "add rice to my list", at(0, 12))
    svc.handle_message(A, "instacart", at(0, 12, 1))
    assert svc.handle_message(A, "cancel", at(0, 12, 2)) == [messages.ORDER_CANCELLED] and "posts" not in seen
    svc.handle_message(A, "order", at(0, 12, 3))
    assert "Your shopping list" in svc.handle_message(A, "shopping", at(0, 12, 4))[0]   # moved on: review dropped
    assert svc.accounts.get(1).awaiting is None


def test_order_without_instacart_still_helps_and_errors_never_break_it():
    svc = make()
    svc.handle_message(A, "add egusi, palm oil and onions to my list", at(0, 12))
    svc.handle_message(A, "order", at(0, 12, 1))
    out = svc.handle_message(A, "yes", at(0, 12, 2))[0]
    assert out.startswith("Instacart isn't switched on") and "an African grocery is your best bet" in out
    from fridgebud.instacart import Instacart
    svc.instacart = Instacart("k", transport=lambda *a: (_ for _ in ()).throw(RuntimeError("401")),
                              getter=lambda *a: (_ for _ in ()).throw(RuntimeError("401")))
    svc.store.save_household(svc.store.get_household(1))
    h = svc.store.get_household(1); h.persona = {"postal": ["N2G 1A1", "CA"]}; svc.store.save_household(h)
    svc.handle_message(A, "order", at(0, 13))
    assert svc.handle_message(A, "yes", at(0, 13, 1))[0].startswith("Instacart isn't switched on")
    assert svc.store.events(1, "instacart_error")


def test_store_guesses():
    from fridgebud import stores
    assert stores.postal("my code is n2g1a1") == ("N2G 1A1", "CA") and stores.postal("94103") == ("94103", "US")
    plan = stores.plan(["rice", "egusi", "fish sauce", "paneer"], ["Costco", "Patel Brothers", "Loblaws"])
    assert plan["main"] == "Costco" and plan["everyday"] == ["rice"]
    assert [(s["kind"], s["store"]) for s in plan["specialty"]] == [
        ("an African grocery", None), ("an Asian grocery", None), ("a South Asian grocery", "Patel Brothers")]


def test_long_script_is_spoken_in_passages_and_never_half_sent():
    import base64, io, wave
    from fridgebud.audio import STYLE, chunks, gemini_wav
    script = " ".join(f"Sentence number {n} is here to make the script long enough to split." for n in range(40))
    parts = chunks(script)
    assert len(parts) >= 4 and all(len(p) <= 650 for p in parts) and " ".join(parts) == script
    calls = []
    def transport(url, headers, body):
        calls.append(json.loads(body)["contents"][0]["parts"][0]["text"])
        return {"candidates": [{"content": {"parts": [{"inlineData": {"data": base64.b64encode(b"\x01\x00" * 100).decode()}}]}}]}
    wav = gemini_wav(script, "k", transport=transport)
    assert [c[len(STYLE):] for c in calls] == parts                       # every passage spoken, in order, once
    with wave.open(io.BytesIO(wav)) as wf:
        assert wf.getnframes() == len(parts) * 100 + (len(parts) - 1) * 6000
    assert "calm" in STYLE and "dramatic" in STYLE
    seen = []
    def flaky(url, headers, body):
        seen.append(1)
        if len(seen) > 2:
            raise RuntimeError("429 quota")
        return {"candidates": [{"content": {"parts": [{"inlineData": {"data": "AAAA"}}]}}]}
    assert gemini_wav(script, "k", transport=flaky) is None               # a passage failed: no half recipe


# cook-alongs are made once and reused ----------------------------------------
def _telegram(file_id="FID-1", fail_resend=False):
    voiced, uploads, resent = [], [], []

    def call(method, params, timeout=20):
        if method == "sendAudio":
            if fail_resend:
                raise TelegramError("Telegram sendAudio failed (400): wrong file identifier")
            resent.append(params)
        return {}
    sender = TelegramSender(call=call, download=lambda p: b"",
                            upload=lambda method, fields, name, data, mime: uploads.append(fields) or {"audio": {"file_id": file_id}})
    return sender, voiced, uploads, resent


def test_cook_along_is_written_and_voiced_once_then_resent_free():
    chef = FakeChef()
    svc = make(chef=chef)
    svc.handle_message(A, "recipe jollof rice", at(0, 13))
    first = svc.handle_message(A, "AUDIO", at(0, 13))[0]
    again = svc.handle_message(A, "AUDIO", at(1, 13))[0]
    assert chef.calls.count("narrate") == 1 and again.script == first.script and again.key == first.key   # Claude once
    assert "You have:" in first.caption or "Still need:" in first.caption    # the personal part rides in the caption

    sender, voiced, uploads, resent = _telegram()
    app = App(svc, sender, synth=lambda script: voiced.append(script) or b"M4A")
    app.send("tg:7", first)
    app.send("tg:8", again)
    assert len(voiced) == 1 and len(uploads) == 1                            # Gemini once
    assert resent[0]["audio"] == "FID-1" and resent[0]["chat_id"] == "8"     # the replay is Telegram's own copy
    assert len(svc.store.events(None, "audio_voiced")) == 1 and len(svc.store.events(None, "audio_reused")) == 1


def test_another_home_with_the_same_recipe_reuses_the_script():
    chef = FakeChef()
    svc = make(chef=chef)
    for text in ("hi", "yes", "1", "bought rice"):
        svc.handle_message(B, text, MON)
    svc.handle_message(A, "recipe jollof rice", at(0, 13))
    svc.handle_message(A, "AUDIO", at(0, 13))
    svc.handle_message(B, "recipe jollof rice", at(0, 14))
    svc.handle_message(B, "AUDIO", at(0, 14))
    assert chef.calls.count("narrate") == 1


def test_if_telegram_lost_its_copy_bud_voices_it_again():
    svc = make(chef=FakeChef())
    svc.handle_message(A, "recipe jollof rice", at(0, 13))
    msg = svc.handle_message(A, "AUDIO", at(0, 13))[0]
    sender, voiced, uploads, _ = _telegram()
    App(svc, sender, synth=lambda s: voiced.append(s) or b"M4A").send("tg:7", msg)
    lost, voiced2, uploads2, _ = _telegram(file_id="FID-2", fail_resend=True)
    App(svc, lost, synth=lambda s: voiced2.append(s) or b"M4A").send("tg:7", msg)
    assert len(voiced2) == 1 and len(uploads2) == 1 and svc.audio_cache.file_id(msg.key) == "FID-2"


def test_cook_along_script_is_about_the_dish_not_this_fridge(monkeypatch):
    chef, seen = Chef("key"), []
    monkeypatch.setattr(chef, "_ask", lambda prompt, *a, **k: seen.append(prompt) or "script")
    chef.narrate("Jollof rice", "serves 4", ["2 cups rice"], ["Cook the rice."])
    assert "Write it for anyone cooking this dish" in seen[0] and "They already have" not in seen[0]


# keeping a recipe Bud showed, however they ask ---------------------------------
from fridgebud.telegram import replied_recipe


def test_save_this_recipe_keeps_the_one_just_shown():
    svc = make(chef=FakeChef())
    svc.handle_message(A, "recipe jollof rice", at(0, 13))                   # AI-written: not kept yet
    assert "Jollof rice" not in svc.handle_message(A, "recipes", at(0, 13, 1))[0]
    out = svc.handle_message(A, "Save this recipe", at(0, 13, 2))[0]
    assert out.startswith("Saved Jollof rice 📚")
    assert "• Jollof rice (AI-generated)" in svc.handle_message(A, "recipes", at(0, 13, 3))[0]
    assert "already in your saved recipes" in svc.handle_message(A, "No I just want it saved", at(0, 13, 4))[0]


@pytest.mark.parametrize("ask", ["save it", "Can you save this recipe", "No I just want it saved", "add it to my recipes",
                                 "keep this one please", "save jollof rice", "SAVE"])
def test_every_way_of_asking_to_save_works(ask):
    svc = make(chef=FakeChef())
    svc.handle_message(A, "recipe jollof rice", at(0, 13))
    assert svc.handle_message(A, ask, at(0, 14))[0].startswith("Saved Jollof rice")


def test_pasting_a_recipe_after_save_this_recipe_still_saves_the_paste():
    from fridgebud.commands import service_command
    assert service_command("save this recipe: 2 cups rice, 1 onion. Boil the rice.")[0] == "paste"
    assert service_command("save me some time") is None


def test_save_with_nothing_shown_asks_which():
    svc = make(chef=FakeChef())
    assert svc.handle_message(A, "save this recipe", at(0, 13))[0].startswith("Which recipe?")


def test_replying_to_a_recipe_message_points_at_that_recipe():
    bud = {"is_bot": True}
    card = {"message": {"text": "save this", "reply_to_message": {"from": bud, "text": "Here's one.\n📖 Jollof rice\nAI-generated"}}}
    audio = {"message": {"text": "save", "reply_to_message": {"from": bud, "caption": "🎧 Cook-along: Egusi Soup (Nigerian). Script written by AI."}}}
    theirs = {"message": {"text": "save", "reply_to_message": {"from": {"is_bot": False}, "text": "📖 Fake"}}}
    assert replied_recipe(card) == "Jollof rice" and replied_recipe(audio) == "Egusi Soup (Nigerian)"
    assert replied_recipe(theirs) is None and replied_recipe({"message": {"text": "hi"}}) is None


def test_reply_beats_whatever_was_shown_last():
    svc = make(chef=FakeChef(), fetch=site_page)
    svc.handle_message(A, "recipe jollof rice", at(0, 13))                    # AI "Jollof rice"
    svc.handle_message(A, "https://example.com/party-jollof", at(0, 14))     # now "Party Jollof Rice" is current
    assert svc.focus_recipe(A, "Jollof rice")                                 # they replied to the first card
    assert svc.handle_message(A, "save this", at(0, 15))[0].startswith("Saved Jollof rice ")


# "yes" answers Bud's question; Bud remembers what it asked ----------------------
def test_yes_goes_to_the_question_bud_just_asked_not_to_sign_up():
    transport, seen = scripted(
        [{"type": "text", "text": "I can't judge that, so check it yourself. Want me to mark it TOSSED?"}],
        [tool("run_command", command="TOSSED chicken thighs")],
        [{"type": "text", "text": "Done, it's off the list."}])
    svc = make(agent=Agent("k", transport=transport))
    svc.handle_message(A, "I've had the chicken thighs since April", at(0, 14))
    out = svc.handle_message(A, "Yes", at(0, 14, 1))
    assert out[-1] == "Done, it's off the list." and not any("You're in" in o for o in out)
    history = seen[1]["messages"]
    assert history[0] == {"role": "user", "content": "I've had the chicken thighs since April"}
    assert history[1]["role"] == "assistant" and "mark it TOSSED" in history[1]["content"]
    assert "chicken thighs" not in [i.name for i in svc.store.active_items(1)]   # it really was tossed


def test_memory_is_short_and_recent():
    transport, seen = scripted([{"type": "text", "text": "Want me to do that?"}], [{"type": "text", "text": "Hi!"}])
    svc = make(agent=Agent("k", transport=transport))
    svc.handle_message(A, "something old", at(0, 9))
    svc.handle_message(A, "hey bud", at(0, 14))                                      # 5 hours later
    assert len(seen[1]["messages"]) == 1                                               # the old exchange is forgotten


def test_a_telegram_reply_shows_bud_which_message_it_answers():
    transport, seen = scripted([{"type": "text", "text": "Noted."}])
    svc = make(agent=Agent("k", transport=transport))
    svc.note_reply(A, "At risk this week:\n- raw chicken pieces tray: use in 2 days")
    svc.handle_message(A, "I've had those since April", at(0, 14))
    assert 'replying to this message of yours: "At risk this week:' in seen[0]["messages"][-1]["content"]


def test_a_question_after_a_photo_with_no_food_says_so_first():
    transport, seen = scripted([{"type": "text", "text": "No food in that photo, so going from your last list."}],
                               [{"type": "text", "text": "Hi!"}])
    svc = make(agent=Agent("k", transport=transport))
    svc.handle_message(A, "chicken thighs, pasta", at(0, 9))
    assert svc.handle_message(A, "", at(0, 14), captured=[], has_media=True) == [messages.PHOTO_UNREADABLE]
    svc.handle_message(A, "Can I make any food with this recipe", at(0, 14, 1))
    assert "showed no readable food" in seen[-1]["messages"][-1]["content"]
    svc.handle_message(A, "hey bud", at(0, 15))                                        # 59 minutes later: forgotten
    assert "showed no readable food" not in seen[-1]["messages"][-1]["content"]


# shorter, and never silent; the list goes wherever their notes live -----------
import json as _json
import threading as _threading
import urllib.error
import urllib.request as _urlreq

from fridgebud import export
from fridgebud.telegram import _health_server, keep_acting


def _svc_with_url():
    svc = Service(Engine(Store()), Config(number="@bud on Telegram", public_url="https://fridgebud.fly.dev"), chef=FakeChef())
    for text in ("hi", "yes", "1", "bought tomatoes"):
        svc.handle_message(A, text, MON)
    svc.book.add_shopping(1, ["long-grain rice", "tomato paste"], at(0, 12))
    return svc


@pytest.mark.parametrize("ask", ["shopping", "show me my grocery list", "can I see my shopping list", "export my list to notion"])
def test_shopping_list_is_bullets_with_an_export_button(ask):
    svc = _svc_with_url()
    card = svc.handle_message(A, ask, at(0, 13))[0]
    assert card.startswith("🛒 Your shopping list (2)\n\n• long-grain rice\n• tomato paste")
    label, url = card.buttons[0]
    assert label == messages.EXPORT_BUTTON and url.startswith("https://fridgebud.fly.dev/list/")
    token = url.rsplit("/", 1)[1]
    assert len(token) >= 12 and svc.shopping_page(token) == ("Shopping list", ["long-grain rice", "tomato paste"])
    assert svc.shopping_page("not-a-real-token") is None


def test_no_public_address_means_no_button():
    svc = make()
    svc.book.add_shopping(1, ["rice"], at(0, 12))
    card = svc.handle_message(A, "shopping", at(0, 13))[0]
    assert not getattr(card, "buttons", None) and "• rice" in card


def test_export_page_offers_share_and_both_copy_formats():
    html_out = export.page(["rice", "tomato <paste>"])
    assert "Share to Notes, Notion" in html_out and "Copy for Apple Notes" in html_out and "Copy for Notion" in html_out
    assert "tomato &lt;paste&gt;" in html_out                                   # escaped on the page
    assert _json.dumps("## Pantry\n- [ ] rice\n- [ ] tomato <paste>") in html_out   # Notion pastes these as to-dos
    assert 'name="robots" content="noindex"' in html_out


def test_export_link_is_served_and_unknown_links_are_not():
    server = _health_server(0)
    server.list_page = lambda token: ["milk", "eggs"] if token == "abc123" else None
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert "milk" in _urlreq.urlopen(base + "/list/abc123").read().decode()
        with pytest.raises(urllib.error.HTTPError):
            _urlreq.urlopen(base + "/list/nope")
    finally:
        server.shutdown()


def test_telegram_shows_card_buttons_under_the_message():
    sent = []
    sender = TelegramSender(call=lambda m, p, t=20: sent.append((m, p)) or {}, download=lambda p: b"", upload=lambda *a: {})
    sender.send("tg:7", messages.Card("🛒 Your shopping list (2)\n\n• rice", [("📋 Export", "https://x.dev/list/t")]))
    markup = _json.loads(sent[0][1]["reply_markup"])
    assert markup == {"inline_keyboard": [[{"text": "📋 Export", "url": "https://x.dev/list/t"}]]}
    sender.send("tg:7", "plain text")
    assert "reply_markup" not in sent[1][1]


def test_cook_along_says_its_recording_and_reuse_is_instant():
    svc = make(chef=FakeChef())
    svc.handle_message(A, "recipe jollof rice", at(0, 13))
    msg = svc.handle_message(A, "AUDIO", at(0, 13))[0]
    calls = []
    sender = TelegramSender(call=lambda m, p, t=20: calls.append((m, p)) or {}, download=lambda p: b"",
                            upload=lambda method, fields, name, data, mime: calls.append(("upload", fields)) or {"audio": {"file_id": "F1"}})
    App(svc, sender, synth=lambda s: b"M4A").send("tg:7", msg)
    texts = [p.get("text", "") for m, p in calls if m == "sendMessage"]
    assert texts and texts[0].startswith("🎧 Recording your cook-along… about")
    assert [m for m, _ in calls].index("sendMessage") < [m for m, _ in calls].index("upload")   # status before audio
    calls.clear()
    App(svc, sender, synth=lambda s: b"M4A").send("tg:8", msg)
    assert not [p for m, p in calls if m == "sendMessage"]                       # reused: no "recording" message


def test_typing_stays_on_until_bud_is_done():
    shown = []

    class Sender:
        def typing(self, who, action="typing"):
            shown.append(action)
    done = _threading.Event()
    t = _threading.Thread(target=keep_acting, args=(Sender(), "tg:1", done), kwargs={"every": 0.01})
    t.start()
    import time as _t
    _t.sleep(0.08)
    done.set()
    t.join(1)
    assert len(shown) >= 3 and set(shown) == {"typing"}


def test_recipe_card_is_readable_for_a_long_video_recipe():
    """The shawarma bowl a tester saved: 24 raw ingredients, sub-recipe notes in brackets, garlic three times."""
    missing = ["boneless chicken thighs", "minced garlic (for chicken)", "onion powder (shawarma spice mix)",
               "paprika (shawarma spice mix)", "cumin (shawarma spice mix)", "cinnamon (shawarma spice mix)",
               "sumac (shawarma spice mix)", "tomato paste", "lemon juice (a squeeze)", "low fat yogurt", "light mayo",
               "honey", "garlic powder or minced garlic (garlic white sauce)", "pickle juice", "light butter",
               "minced garlic (for rice)", "turmeric", "smoked paprika", "basmati rice, washed and uncooked",
               "cherry tomatoes, halved", "pickles, sliced", "parsley, chopped", "sumac (slaw)"]
    staples = ["salt (shawarma spice mix)", "black pepper (shawarma spice mix)", "olive oil (for chicken)", "salt (rice)",
               "water (for rice)", "olive oil (slaw)"]
    card = messages.recipe_card("High Protein Chicken Shawarma Rice Bowls",
                                "🔗 " + messages.clean_url("https://youtube.com/watch?v=Zx9&si=abc&utm_source=x")
                                + "\n" + messages.WATCHED, "4", 45, ["red onion"], missing, staples,
                                "your last update (Tue Nov 17, 2:17 PM):")
    print(card)
    assert "🔗 https://youtube.com/watch?v=Zx9\n" in card
    assert "🛒 Needs a shop: 21 things missing." in card                      # 23 raw lines, garlic and sumac de-duplicated
    assert card.count("minced garlic") == 1 and "(" not in card.split("Serves")[1].split("🧊")[0]
    assert "• Spices: onion powder, paprika, cumin, cinnamon, sumac, garlic powder, turmeric, smoked paprika" in card
    assert "• basmati rice" in card and "more" not in card       # the main things are never hidden
    assert "Assumes you have: salt, black pepper, olive oil, water." in card
    assert card.rstrip().endswith("Reply SHOP to add them all · COOK for steps · AUDIO to listen")
    assert len(card.splitlines()) <= 28


def test_shop_after_a_recipe_also_offers_export():
    svc = _svc_with_url()
    svc.handle_message(A, "recipe jollof rice", at(0, 13))
    reply = svc.handle_message(A, "SHOP", at(0, 14))[0]
    assert reply.startswith("🛒 Added to your shopping list:")
    assert "(" not in reply.split("\n")[1]                                  # tidy names on the list too
    assert reply.buttons[0][0] == messages.EXPORT_BUTTON


def test_list_page_groups_by_aisle_and_ticks_off():
    items = ["boneless chicken thighs", "minced garlic", "tomato paste", "low fat yogurt", "basmati rice", "cumin",
             "cherry tomatoes", "pita bread", "frozen peas", "light mayo", "lemon juice", "paper towels"]
    assert [name for name, _ in export.grouped(items)] == ["Produce", "Meat & fish", "Dairy & eggs", "Bakery", "Pantry",
                                                            "Spices", "Frozen", "Other"]
    assert dict(export.grouped(items))["Pantry"] == ["tomato paste", "basmati rice", "light mayo", "lemon juice"]
    out = export.page(items)
    assert out.count('type="checkbox"') == 12 and "0 of 12 items in the cart" in out
    assert "localStorage" in out and "try {" in out                     # ticks survive a reload, never crash the page
    assert "Your list is empty" in export.page([])



@pytest.mark.parametrize("ask", ["I mean my jollof rice shopping list", "shopping list for jollof rice",
                                 "what do I need for the jollof rice?", "SHOPPING jollof rice"])
def test_one_recipes_shopping_list(ask):
    from fridgebud.commands import service_command
    assert service_command(ask) == ("shopfor", ["jollof rice"])
    svc = _svc_with_url()
    svc.handle_message(A, "recipe jollof rice", at(0, 13))
    svc.book.add_shopping(1, ["paper towels"], at(0, 13))
    card = svc.handle_message(A, ask, at(0, 14))[0]
    assert card.startswith("🛒 For Jollof rice:") and "paper towels" not in card    # only this dish, not the whole list
    assert "• long-grain rice ✓" in card and "Reply SHOP to add the other" in card
    url = card.buttons[0][1]
    title, items = svc.shopping_page(url.split("/list/")[1])
    assert title == "Shopping for Jollof rice" and "paper towels" not in items and "long-grain rice" in items


def test_plain_list_phrases_still_mean_the_whole_list():
    from fridgebud.commands import service_command
    for ask in ("my shopping list", "show me my grocery list", "can I see my shopping list", "the shopping list"):
        assert service_command(ask)[0] == "shopping", ask


def test_unknown_dish_list_says_so():
    svc = _svc_with_url()
    assert "don't have a pad thai recipe saved" in svc.handle_message(A, "my pad thai shopping list", at(0, 14))[0]


def test_stored_list_names_are_shown_tidy():
    svc = _svc_with_url()
    svc.book.add_shopping(1, ["garlic, minced", "Thai bird's eye chilies, chopped", "garlic (for rice)"], at(0, 13))
    card = svc.handle_message(A, "shopping", at(0, 14))[0]
    assert "• garlic\n" in card and card.count("garlic") == 1 and "chopped" not in card


# pictures on recipe cards ---------------------------------------------------
class PhotoSender:
    def __init__(self):
        self.out, self.uploads = [], 0

    def send(self, to, body):
        self.out.append(("text", str(body)))

    def send_photo_ref(self, to, ref, caption=""):
        self.out.append(("photo", ref, caption))

    def send_photo(self, to, data, caption="", mime="image/png"):
        self.uploads += 1
        self.out.append(("upload", data, caption))
        return "FILE123"


class FakeImager:
    daily_cap = 40

    def __init__(self, fail=False):
        self.prompts, self.fail = [], fail

    def draw(self, prompt):
        if self.fail:
            raise RuntimeError("quota")
        self.prompts.append(prompt)
        return b"PNG", "image/png"


def test_youtube_recipe_card_comes_with_the_videos_thumbnail():
    chef, watcher = NotesChef(), FakeWatcher()
    svc = make(chef=chef, fetch=video_page)
    svc.watcher = watcher
    card = svc.handle_message(A, "https://youtu.be/abc12345?si=x", at(0, 12))[0]
    assert card.photo == {"url": "https://img.youtube.com/vi/abc12345/hqdefault.jpg", "caption": "▶️ Jollof rice"}
    sender = PhotoSender()
    App(svc, sender).send(A, card)
    assert sender.out[0] == ("photo", "https://img.youtube.com/vi/abc12345/hqdefault.jpg", "▶️ Jollof rice")
    assert sender.out[1][0] == "text" and "📖 Jollof rice" in sender.out[1][1]       # picture first, card under it


def test_ai_recipe_gets_a_labelled_ai_photo_drawn_once_then_reused():
    svc = make(chef=FakeChef())
    card = svc.handle_message(A, "recipe jollof rice", at(0, 12))[0]
    assert card.photo["caption"] == "Jollof rice" and "AI image" not in card.photo["caption"] and "No text" in card.photo["prompt"]
    sender, imager = PhotoSender(), FakeImager()
    app = App(svc, sender)
    app.imager = imager
    app.send(A, card)
    assert sender.out[0] == ("upload", b"PNG", card.photo["caption"]) and sender.out[1][0] == "text"
    app.send(A, card)                                                   # same dish again: Telegram's copy, no new drawing
    assert len(imager.prompts) == 1 and sender.out[2] == ("photo", "FILE123", card.photo["caption"])
    assert svc.store.events(type_="image_made")


def test_picture_trouble_never_stops_the_recipe():
    svc = make(chef=FakeChef())
    card = svc.handle_message(A, "recipe jollof rice", at(0, 12))[0]
    sender = PhotoSender()
    app = App(svc, sender)
    app.imager = FakeImager(fail=True)
    app.send(A, card)
    assert [o[0] for o in sender.out] == ["text"] and svc.store.events(type_="photo_error")
    app.imager = None
    app.send(A, card)                                                   # no imager at all: just the card
    assert [o[0] for o in sender.out] == ["text", "text"]


def test_saved_blog_recipe_gets_no_made_up_picture():
    svc = make(fetch=site_page)
    card = svc.handle_message(A, "https://example.com/jollof", at(0, 12))[0]
    assert getattr(card, "photo", None) is None


def test_telegram_sends_photos_by_upload_and_by_reference():
    calls, uploads = [], []
    sender = TelegramSender(call=lambda m, p, t=20: calls.append((m, p)) or {}, download=lambda p: b"",
                            upload=lambda m, f, n, d, mime: uploads.append((m, f, n, mime)) or {"photo": [{"file_id": "s"}, {"file_id": "BIG"}]})
    assert sender.send_photo("tg:7", b"x", "🧪 AI image of Stew", "image/jpeg") == "BIG"
    assert uploads[0][0] == "sendPhoto" and uploads[0][2] == "dish.jpg"
    sender.send_photo_ref("tg:7", "https://img.youtube.com/vi/a/hqdefault.jpg", "▶️ Stew")
    assert calls[0] == ("sendPhoto", {"chat_id": "7", "photo": "https://img.youtube.com/vi/a/hqdefault.jpg", "caption": "▶️ Stew"})


def test_imager_reads_the_picture_out_of_geminis_reply():
    import base64
    from fridgebud.imagery import Imager
    seen = {}

    def transport(url, headers, body):
        seen.update(url=url, body=json.loads(body))
        return {"candidates": [{"content": {"parts": [{"text": "here"}, {"inlineData": {"mimeType": "image/png",
                                                                                         "data": base64.b64encode(b"IMG").decode()}}]}}]}
    data, mime = Imager("k", transport=transport).draw("a bowl of jollof")
    assert data == b"IMG" and mime == "image/png"
    assert "gemini-2.5-flash-image:generateContent" in seen["url"] and seen["body"]["generationConfig"]["responseModalities"] == ["IMAGE"]


@pytest.mark.parametrize("ask", ["What are my saved recipes ?", "show me my recipes", "what recipes have I saved",
                                 "which recipes did I save", "list my saved recipes", "my favourite recipes"])
def test_asking_for_saved_recipes_is_never_read_as_a_dish(ask):
    from fridgebud.commands import service_command
    assert service_command(ask) == ("recipes", [])
    svc = make()
    assert "couldn't turn" not in svc.handle_message(A, ask, at(0, 12))[0]


def test_question_shaped_text_is_not_a_dish_name():
    from fridgebud.commands import service_command
    assert service_command("what are the best recipes") != ("recipe", ["what are the best"])


# the daily idea arrives as a picture of the dish, with the short text as its caption -------------
def test_daily_idea_is_one_message_picture_first():
    svc = make()
    text = svc.tick(at(0))[0][1]
    assert text.photo["inline"] and "No text" in text.photo["prompt"]
    sender, imager = PhotoSender(), FakeImager()
    app = App(svc, sender)
    app.imager = imager
    app.send(A, text)
    assert sender.out == [("upload", b"PNG", str(text))]                 # one message: the dish, the idea under it
    assert len(str(text)) < 1000 and str(text).count("\n") <= 8        # short enough to read tired


def test_no_picture_means_the_idea_still_arrives_as_text():
    svc = make()
    text = svc.tick(at(0))[0][1]
    sender = PhotoSender()
    app = App(svc, sender)
    app.imager = FakeImager(fail=True)
    app.send(A, text)
    assert sender.out == [("text", str(text))]


def test_asking_for_dinner_or_swapping_also_comes_with_the_picture():
    svc = make()
    reply = svc.handle_message(A, "DINNER", at(0, 18))[0]
    assert reply.photo["inline"] and "👍 cook it" in reply
    lunch = svc.handle_message(A, "lunch", at(0, 12))[0]
    assert getattr(lunch, "photo", None) and lunch.photo["inline"]
