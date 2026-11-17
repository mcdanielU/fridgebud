# FridgeBud engine (v0)

A text agent that watches the 5 to 12 foods most likely to be wasted, plus
leftovers, and sends one 4:30 PM text with what to use and a dinner idea.
Every reply ("made it", "tossed spinach", "good milk") teaches it how fast food
goes off in that home.

Python 3.10+, standard library only. Runs with no API keys; Twilio and Anthropic keys switch on real texts, photos and free-form replies.

## Run it

```bash
python3 -m fridgebud.cli --demo      # a scripted week for one household
python3 -m fridgebud.cli             # text it yourself, with a fake clock
python3 -m pip install pytest && python3 -m pytest -q
```

To read a real photo (receipt, grocery haul, surprise bag or fridge) with Claude vision:

```bash
export ANTHROPIC_API_KEY=sk-ant-...            # your key; never commit it
python3 -m fridgebud.cli
> /realphoto ~/Desktop/receipt.jpg
```

It lists what it found, with prices from receipts. Lines it can't read clearly are skipped and mentioned, never guessed. Set `FRIDGEBUD_VISION_MODEL` to change the model.

In the interactive simulator, type messages as if texting. `/next` jumps to the
next 4:30 PM nudge, `/day 2` moves the clock, `/photo spinach, bread` fakes a
photo, `/receipt` shows the weekly rescue receipt, `/rates` shows what it has
learned.

## Run the whole app

```bash
python3 -m fridgebud.server          # http://localhost:8080, texts print in the terminal
```

See `DEPLOY.md` for trying it locally with curl, and for going live on Twilio and Fly.io.

## How it fits together

| File | Job |
| --- | --- |
| `data/shelf_life.json` | 40 foods with "use within" days. `hc` = Health Canada's fridge chart, `est` = quality estimate for produce it doesn't list |
| `shelf_life.py` | Matches messy names ("Organic Baby Spinach 312g") to a rule |
| `watchlist.py` | Picks what's at risk; learns per-home spoil rates from replies |
| `recipes.py` | Tonight's dinner (leftovers first), plus a breakfast idea for fruit and dairy |
| `parser.py` | Turns replies into intents (made, tossed, good, used, leftover, add, stop) |
| `messages.py` | Every word it sends, in one place |
| `engine.py` | The loop: `handle_inbound()` for replies, `due_nudges()` for the scheduler |
| `capture.py` | Photo capture: receipt, haul or fridge photo to items and prices, through Claude vision |
| `store.py` | SQLite: households, items, learned spoil rates, event log for the scorecard |
| `service.py` | The app around the engine: onboarding, fridge-mates, Bud vs plain, Sunday receipt, day-14 paywall |
| `accounts.py` | Voice variant, onboarding stage, consent record, invite code, members, trial and payment |
| `voice.py` | Bud's voice: restyles the plain texts for half of households |
| `receipt.py` | The Sunday rescue receipt, built to be screenshotted |
| `fallback.py` | Free-form replies: a small Claude model rewrites them as a command the parser knows |
| `sms.py` | Twilio SMS/MMS: send, verify webhooks, download photos |
| `telegram.py` | Telegram test channel: free, photos work, runs from your Mac (`python3 -m fridgebud.telegram`) |
| `plan.py`, `commands.py` | Instant rescue plan after a photo; DINNER, OLD, "do I have eggs?", "what's in my fridge?" |
| `recipebook.py` | Saved recipes (from links, pasted, or AI-written and labelled so) and the shopping list |
| `links.py` | Reads a recipe link: recipe sites are copied as published; video links give a title and caption |
| `chef.py` | AI kitchen jobs: recipe from a caption, recipe from scratch, fridge matching, cook-along script, fridge roast |
| `agent.py` | Bud as a tool-using agent for free-form messages; cards and labels still come from code |
| `audio.py` | Audio cook-along using the Mac's built-in voice (`say`); text fallback elsewhere |
| `video.py` | Watches cooking videos with Google's Gemini: public YouTube links and video files sent in chat |
| `web.py` | Landing page and signup form (price up front, CASL consent) |
| `server.py` | Web server: landing page, `/signup`, `/sms` webhook, scheduler, `/admin/scorecard` |
| `metrics.py` | The six launch gates and the voice test, from the event log |

## Rules it follows

- No texts until the person replies YES; STOP works at any time (CASL).
- No text on days when nothing is at risk.
- It never says food is safe. Past its window, an item is never used in a recipe; FridgeBud only asks "still good, or tossed?"
- Times are for a fridge at 4 °C or lower. Source: https://www.canada.ca/en/health-canada/services/general-food-safety-tips/safe-food-storage.html

## Known limits

- The recipe bank has 19 recipes, so the same dinner can repeat for a few days.
- Payments are recorded by hand (`/admin/paid`); there is no payment-provider webhook yet.
- A fridge-mate who joins shares the household's list and texts, but there is no group chat.
- Shares are counted as invites and referrals. Screenshots of the receipt can't be counted.
- iMessage is not wired up; the channel is SMS/MMS.
- One server, one SQLite file. Fine for the launch test, not for thousands of homes.

## Needs your accounts before it is live

Twilio number, Anthropic API key, a Fly.io app, the monthly price and a payment link. See `DEPLOY.md`.
