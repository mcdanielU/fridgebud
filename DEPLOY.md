# Running and deploying FridgeBud

## 1. Try it on your laptop (no accounts needed)

```bash
cd ~/Desktop/Business/fridgebud
python3 -m fridgebud.server
```

Open http://localhost:8080 for the landing page. With no Twilio settings the
server is in console mode: every text FridgeBud would send is printed in the
terminal. Pretend to be a phone with curl:

```bash
text() { curl -s -o /dev/null -X POST localhost:8080/sms --data-urlencode "From=+15195550100" --data-urlencode "Body=$1"; }
text hi; text yes; text 2; text "bought ground beef, tortillas, baby spinach"
```

The 4:30 PM text prints by itself at 4:30 Toronto time (the scheduler checks every minute).

Tests: `python3 -m pip install pytest && python3 -m pytest -q`

## 1b. Use it for real on your phone, free (Telegram)

The test channel before SMS: you chat with FridgeBud in the Telegram app, photos included.
It runs on your Mac, so it only answers while this is running and the Mac is awake.

1. In Telegram, message **@BotFather**, send `/newbot`, pick a name and a username ending in `bot`. Copy the token it gives you.
2. Create an Anthropic API key at console.anthropic.com (add a few dollars of credit). This reads photos.
3. In this folder, create a file named `.env` with two lines (it is git-ignored; never share it):

   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   ANTHROPIC_API_KEY=sk-ant-...
   GEMINI_API_KEY=...
   ```

   `GEMINI_API_KEY` is optional and lets Bud watch videos: public YouTube links, and video files you send in the chat
   (a saved TikTok or Reel, up to about 14 MB). Get a key at aistudio.google.com. On Gemini's free tier Google may use
   what is sent to improve its products.

4. Run `python3 -m fridgebud.telegram`, open the link it prints, press **Start**, reply YES, then send a photo of your fridge.

It uses the same `fridgebud.db` file as the web server, so don't run both against the same file at once.

## 2. Go live with SMS

You need three accounts. Everything is set through environment variables; nothing goes in the code.

**Twilio (texts)**
1. Create an account and buy a Canadian number with SMS and MMS.
2. After step "Fly" below, open the number's settings and set "A message comes in" to
   `POST https://<your-app>.fly.dev/sms`.
3. Note: Canadian carriers filter business texts sent from ordinary 10-digit numbers.
   That is fine for dogfooding and the first testers. Before the Nov 23 launch, ask Twilio
   about toll-free verification, which is what they recommend for this kind of traffic.

**Anthropic (photos and free-form replies)**
Create an API key. Without it the app still runs; photos get "couldn't read it" and
free-form replies get the help text.

**Fly.io (hosting)**

```bash
brew install flyctl && fly auth login
fly launch --no-deploy --copy-config          # pick your app name; keep the Toronto region
fly volumes create fridgebud_data --size 1 --region yyz
fly secrets set TWILIO_ACCOUNT_SID=... TWILIO_AUTH_TOKEN=... TWILIO_NUMBER=+1... \
  PUBLIC_URL=https://<your-app>.fly.dev ANTHROPIC_API_KEY=... \
  FRIDGEBUD_PRICE="$X.XX a month" FRIDGEBUD_CONTACT="FridgeBud, <address>, <email>" \
  FRIDGEBUD_ADMIN_KEY=$(openssl rand -hex 16)
fly deploy
fly scale count 1                              # exactly one machine, always
```

Then text your Twilio number "hi" from your own phone.

## 3. Day to day

| What | How |
| --- | --- |
| Scorecard (the six gates and Bud vs plain) | `https://<app>.fly.dev/admin/scorecard?key=<FRIDGEBUD_ADMIN_KEY>` |
| Mark a household as paid | `curl -X POST "https://<app>.fly.dev/admin/paid?key=<key>&phone=5195550100"` |
| Turn on the day-14 paywall | `fly secrets set FRIDGEBUD_PAYMENT_LINK=https://...` |
| Logs | `fly logs` |
| Back up the database | `fly ssh sftp get /data/fridgebud.db` |

## Things to know

- **One machine only.** The scheduler runs inside the server and the data is one SQLite file.
- **Consent.** A web signup records the time, IP address and the exact consent wording, then
  sends one welcome text. Nothing else is sent until the person replies YES. STOP always works.
- **CASL footer.** Set `FRIDGEBUD_CONTACT` to a real mailing address and a way to reach you.
- **Payments are recorded by hand** with `/admin/paid` for now. A payment-provider webhook can replace that later.
- **iMessage** is not wired up. SMS/MMS reaches iPhones and Android alike, so it was the fastest path to launch.
