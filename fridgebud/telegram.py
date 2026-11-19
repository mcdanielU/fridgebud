"""Telegram channel: talk to FridgeBud from the Telegram app. Free, and photos work.

    python3 -m fridgebud.telegram

This is the test channel for you and a few friends before SMS is switched on.
It needs no hosting and no phone number: it runs on your Mac and asks
Telegram for new messages ("long polling"). Settings, from the environment or
a `.env` file in the folder you run it from:

    TELEGRAM_BOT_TOKEN   from @BotFather in Telegram
    ANTHROPIC_API_KEY    reads fridge, haul and receipt photos, and free-form replies
    FRIDGEBUD_DB         SQLite file (default fridgebud.db)

Each Telegram chat is one household, stored as "tg:<chat id>" where SMS would
store a phone number. Everything else (engine, onboarding, receipt) is shared.
"""
from __future__ import annotations

from collections import OrderedDict

import json
import re
import os
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from . import export, messages, metrics
from .agent import Agent
from .capture import ClaudeVisionExtractor
from .chef import Chef
from .engine import Engine
from .fallback import ClaudeInterpreter
from .instacart import Instacart
from .server import App, scheduler
from .service import Config, Service
from .store import Store
from .video import GeminiWatcher

API = "https://api.telegram.org"
MAX_PHOTO_BYTES = 5 * 1024 * 1024
PREFIX = "tg:"

# (method, params, timeout) -> the "result" part of Telegram's reply
Call = Callable[[str, dict, float], object]


class TelegramError(RuntimeError):
    pass


def _api_call(token: str) -> Call:
    def call(method: str, params: dict, timeout: float = 20):
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(f"{API}/bot{token}/{method}", data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                reply = json.loads(resp.read())
        except urllib.error.HTTPError as err:  # never include the URL: it contains the token
            try:
                detail = json.loads(err.read()).get("description", "")
            except Exception:
                detail = ""
            raise TelegramError(f"Telegram {method} failed ({err.code}): {detail}") from None
        except urllib.error.URLError as err:
            raise TelegramError(f"Telegram {method} failed: {err.reason}") from None
        if not reply.get("ok"):
            raise TelegramError(f"Telegram {method} failed: {reply.get('description', '')}")
        return reply.get("result")
    return call


def _download(token: str) -> Callable[..., bytes]:
    def download(file_path: str, max_bytes: int = MAX_PHOTO_BYTES) -> bytes:
        req = urllib.request.Request(f"{API}/file/bot{token}/{urllib.parse.quote(file_path)}")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read(max_bytes + 1)
        except urllib.error.URLError:
            raise TelegramError("Telegram photo download failed") from None
    return download


def _upload(token: str):
    def upload(method: str, fields: dict, filename: str, data: bytes, mime: str):
        boundary = "----fridgebud" + os.urandom(8).hex()
        parts = []
        for key, value in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        field = "photo" if method == "sendPhoto" else "audio"
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
                     f'Content-Type: {mime}\r\n\r\n'.encode() + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(f"{API}/bot{token}/{method}", data=b"".join(parts), method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                reply = json.loads(resp.read())
        except urllib.error.HTTPError as err:
            try:
                detail = json.loads(err.read()).get("description", "")
            except Exception:
                detail = ""
            raise TelegramError(f"Telegram {method} failed ({err.code}): {detail}") from None
        except urllib.error.URLError as err:
            raise TelegramError(f"Telegram {method} failed: {err.reason}") from None
        if not reply.get("ok"):
            raise TelegramError(f"Telegram {method} failed: {reply.get('description', '')}")
        return reply.get("result")
    return upload


def split_message(body: str, limit: int = 3800) -> list[str]:
    """Telegram caps a message at 4096 characters; split long ones at line breaks."""
    parts, current = [], ""
    for line in body.split("\n"):
        while len(line) > limit:
            parts, current, line = parts + ([current] if current else []) + [line[:limit]], "", line[limit:]
        if current and len(current) + len(line) + 1 > limit:
            parts, current = parts + [current], line
        else:
            current = f"{current}\n{line}" if current else line
    return parts + ([current] if current else [])


class TelegramSender:
    """Sends texts to Telegram chats and fetches the photos people send."""

    def __init__(self, token: str = "", call: Call | None = None, download: Callable[[str], bytes] | None = None,
                 upload: Callable[[str, dict, str, bytes, str], object] | None = None):
        self.call = call or _api_call(token)
        self.download = download or _download(token)
        self.upload = upload or _upload(token)
        self._sent: "OrderedDict[tuple[str, int], str]" = OrderedDict()   # (chat, message id) -> text, for reactions

    def sent_text(self, to: str, message_id: int) -> str | None:
        """What Bud's message said, so a reaction can be tied to the idea it was tapped on."""
        return self._sent.get((to, message_id))

    def _remember(self, to: str, sent, text: str) -> None:
        mid = sent.get("message_id") if isinstance(sent, dict) else None
        if mid is not None:
            self._sent[(to, mid)] = text
            while len(self._sent) > 400:
                self._sent.popitem(last=False)

    def send(self, to: str, body: str) -> None:
        if not to.startswith(PREFIX):
            raise TelegramError(f"{to} is not a Telegram chat")
        parts = split_message(body)
        buttons = getattr(body, "buttons", None)
        for i, part in enumerate(parts):
            params = {"chat_id": to[len(PREFIX):], "text": part}
            if buttons and i == len(parts) - 1:   # buttons go under the last piece of a long message
                params["reply_markup"] = json.dumps({"inline_keyboard": [[{"text": label, "url": url}] for label, url in buttons]})
            self._remember(to, self.call("sendMessage", params, 20), part)

    def send_audio(self, to: str, data: bytes, title: str, caption: str = "") -> None:
        if not to.startswith(PREFIX):
            raise TelegramError(f"{to} is not a Telegram chat")
        fields = {"chat_id": to[len(PREFIX):], "title": title[:60], "performer": "Bud", "caption": caption[:1000]}
        sent = self.upload("sendAudio", fields, "cookalong.m4a", data, "audio/mp4") or {}
        return ((sent.get("audio") or {}).get("file_id")) if isinstance(sent, dict) else None

    def send_audio_id(self, to: str, file_id: str, title: str, caption: str = "") -> None:
        """Resend audio Telegram already has (same bot, any chat): no upload, no new voice."""
        if not to.startswith(PREFIX):
            raise TelegramError(f"{to} is not a Telegram chat")
        self.call("sendAudio", {"chat_id": to[len(PREFIX):], "audio": file_id, "title": title[:60], "performer": "Bud",
                                "caption": caption[:1000]}, 30)

    def send_photo(self, to: str, data: bytes, caption: str = "", mime: str = "image/png") -> str | None:
        """Upload a picture; returns Telegram's file_id so the same picture can be resent for free."""
        if not to.startswith(PREFIX):
            raise TelegramError(f"{to} is not a Telegram chat")
        ext = "jpg" if "jpeg" in mime else "png"
        sent = self.upload("sendPhoto", {"chat_id": to[len(PREFIX):], "caption": caption[:1000]}, f"dish.{ext}", data, mime) or {}
        self._remember(to, sent, caption)          # a 👍 on the picture counts like one on the text
        sizes = sent.get("photo") if isinstance(sent, dict) else None
        return sizes[-1].get("file_id") if sizes else None

    def send_photo_ref(self, to: str, ref: str, caption: str = "") -> None:
        """A picture Telegram can fetch itself (an https URL) or already has (a file_id)."""
        if not to.startswith(PREFIX):
            raise TelegramError(f"{to} is not a Telegram chat")
        self._remember(to, self.call("sendPhoto", {"chat_id": to[len(PREFIX):], "photo": ref, "caption": caption[:1000]}, 30), caption)

    def fetch_media(self, file_id: str, max_bytes: int = MAX_PHOTO_BYTES) -> bytes:
        info = self.call("getFile", {"file_id": file_id}, 20)   # Telegram itself refuses files over 20 MB ("file is too big")
        if info.get("file_size", 0) > max_bytes:
            raise TelegramError("File is too large.")
        blob = self.download(info["file_path"], max_bytes) if max_bytes != MAX_PHOTO_BYTES else self.download(info["file_path"])
        if len(blob) > max_bytes:
            raise TelegramError("File is too large.")
        return blob

    def typing(self, to: str, action: str = "typing") -> None:
        """Telegram shows this for about 5 seconds: "typing…", or "recording audio…" for action="upload_voice"."""
        try:
            self.call("sendChatAction", {"chat_id": to[len(PREFIX):], "action": action}, 10)
        except TelegramError:
            pass


REACTION_WORDS = {"👍", "👎", "🔥", "👌"}    # the only reactions Bud treats as an answer; others are ignored


def parse_update(update: dict) -> tuple[str, str, list[tuple[str, str]]] | None:
    """One Telegram update -> (household id, text, [(file_id, media type)]), or None to ignore it."""
    react = update.get("message_reaction")
    if react:   # they tapped a reaction instead of typing: 👍 👎 🔥 👌 mean what the typed emoji means
        if (react.get("chat") or {}).get("type") != "private":
            return None
        added = [r.get("emoji") for r in react.get("new_reaction") or [] if r.get("type") == "emoji"]
        added = [e for e in added if e in REACTION_WORDS]
        return (f"{PREFIX}{react['chat']['id']}", added[0], []) if added else None
    msg = update.get("message")
    if not msg or msg.get("chat", {}).get("type") != "private":
        return None  # one-to-one chats only; ignore groups, edits and channel posts
    who = f"{PREFIX}{msg['chat']['id']}"
    text = msg.get("text") or msg.get("caption") or ""
    if text.strip().lower().startswith("/start"):
        text = "hi"   # Telegram's "Start" button
    media: list[tuple[str, str]] = []
    if msg.get("photo"):  # several sizes of the same photo; the last is the largest
        media.append((msg["photo"][-1]["file_id"], "image/jpeg"))
    doc = msg.get("document") or {}
    if doc.get("mime_type", "").startswith("image/") and doc.get("file_size", 0) <= MAX_PHOTO_BYTES:
        media.append((doc["file_id"], doc["mime_type"]))
    for key in ("video", "video_note", "animation"):  # a saved TikTok or Reel sent as a file
        clip = msg.get(key) or {}
        if clip.get("file_id") and not media:
            media.append((clip["file_id"], clip.get("mime_type") or "video/mp4"))
    if doc.get("mime_type", "").startswith("video/") and not media:
        media.append((doc["file_id"], doc["mime_type"]))
    voice = msg.get("voice") or {}
    if voice.get("file_id") and not media:   # a voice note: Bud listens (taste, or a spoken request)
        media.append((voice["file_id"], voice.get("mime_type") or "audio/ogg"))
    if not text and not media:
        return None  # stickers, etc.
    return who, text, media


def keep_acting(sender, who: str, done: threading.Event, action: str = "typing", every: float = 4.5) -> None:
    """Show "typing…" (or another chat action) until `done` is set. Telegram drops it after ~5 seconds."""
    sender.typing(who, action) if action != "typing" else sender.typing(who)
    while not done.wait(every):
        sender.typing(who, action) if action != "typing" else sender.typing(who)


def wants_stats(who: str, text: str, media: list, owners: set[str]) -> bool:
    """Only the founder's own chats can ask for the numbers; anyone else texting STATS just talks to Bud."""
    return who in owners and not media and text.strip().lower().lstrip("/") in {"stats", "metrics"}


def stats_text(app: App, daily: bool = False) -> str:
    owners = getattr(app, "owners", set())
    with app.service.lock:
        return metrics.render(metrics.report(app.service.store, app.clock(), exclude=owners), daily=daily)


def replied_recipe(update: dict) -> str | None:
    """The recipe title in the Bud message they replied to (a recipe card or a cook-along), if any."""
    replied = (update.get("message") or {}).get("reply_to_message") or {}
    if not (replied.get("from") or {}).get("is_bot"):
        return None
    return recipe_title_in(replied.get("text") or replied.get("caption") or "")


def recipe_title_in(body: str) -> str | None:
    for line in body.splitlines():
        if line.startswith("📖 "):
            return line[2:].strip() or None
    m = re.search(r"🎧 Cook-along: (.+?)\.(?:\s|$)", body)
    return m.group(1).strip() if m else None


OLDER_IDEA = "That one's from earlier 🙂 Tap 👍 or 👎 on my newest idea and I'll use it."


def reaction_applies(app: App, who: str, message_id: int) -> str | None:
    """A reaction means what it's tapped on. Returns None to go ahead, or a short note to send instead.
    Tapped on the current idea or a recipe card: fine (a card becomes the recipe being rated). On an older idea,
    or on a chat line that isn't an idea at all: not what they meant, so don't change their taste. A message
    from before a restart can't be identified, so it counts as the current idea."""
    text = app.sender.sent_text(who, message_id) if hasattr(app.sender, "sent_text") else None
    if text is None:
        return None
    title = recipe_title_in(text)
    if title:
        if hasattr(app.service, "focus_recipe"):
            app.service.focus_recipe(who, title)
        return None
    if messages.PICK_MARK in text:
        current = getattr(app.service, "is_current_pick", None)
        return None if current is None or current(who, text) else OLDER_IDEA
    return ""     # some other message: ignore quietly


def handle_update(app: App, update: dict) -> None:
    parsed = parse_update(update)
    if parsed is None:
        return
    who, text, media = parsed
    react = update.get("message_reaction")
    if react:
        note = reaction_applies(app, who, react.get("message_id", 0))
        if note is not None:
            if note:
                app.send(who, note)
            return
    if wants_stats(who, text, media, getattr(app, "owners", set())):
        app.send(who, stats_text(app))
        return
    replied = (update.get("message") or {}).get("reply_to_message") or {}
    if (replied.get("from") or {}).get("is_bot") and hasattr(app.service, "note_reply"):
        app.service.note_reply(who, replied.get("text") or replied.get("caption") or "")
    title = replied_recipe(update)
    if title and hasattr(app.service, "focus_recipe"):
        app.service.focus_recipe(who, title)   # "save this", "audio", "shop" now mean the recipe they replied to
    working = threading.Event()
    if hasattr(app.sender, "typing"):  # reading a photo or writing a recipe takes a while: never go silent
        threading.Thread(target=keep_acting, args=(app.sender, who, working), daemon=True).start()
    started = time.monotonic()
    try:
        app.inbound(who, text, media, order=(update.get("message") or {}).get("message_id", 0))
    finally:
        working.set()
    if not media:  # photos and screenshot albums wait on purpose, so only time plain texts
        secs = round(time.monotonic() - started, 2)
        with app.service.lock:
            h = app.service.store.get_household_by_phone(who)
            app.service.store.log(h.id if h else None, app.clock(), "handled", secs=secs, phone=who)


def digest(app: App, stop: threading.Event, hour: int = 9) -> None:
    """Every morning at `hour` (Toronto), send the owner the stats without being asked."""
    while not stop.is_set():
        now = datetime.now(metrics.LOCAL)
        due = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if due <= now:
            due += timedelta(days=1)
        if stop.wait((due - now).total_seconds()):
            return
        try:
            text = stats_text(app, daily=True)
            for owner in getattr(app, "owners", set()):
                app.send(owner, text)
        except Exception:
            traceback.print_exc()


def poll(app: App, call: Call, stop: threading.Event, step_down: bool = False, retry: float = 5) -> None:
    """step_down: this is the laptop copy. If another copy (the Fly deployment) starts reading the same bot,
    Telegram answers this one with 409 Conflict; after a few of those, quit so nobody gets every text twice.
    One stray 409 just after a restart (the old process's poll still open) is not enough to quit."""
    offset = 0
    conflicts: list[float] = []
    while not stop.is_set():
        try:
            updates = call("getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": '["message","message_reaction"]'}, 40)
        except TelegramError as err:
            if step_down and "(409)" in str(err):
                now = time.monotonic()
                conflicts = [t for t in conflicts if now - t < 120] + [now]
                if len(conflicts) >= 3:
                    print("Your deployed Bud is now answering this bot, so this laptop copy is stopping. "
                          "You can close this window.", flush=True)
                    stop.set()
                    return
            print(f"(waiting to reach Telegram: {err})", flush=True)
            stop.wait(retry)
            continue
        for update in updates or []:
            offset = max(offset, update["update_id"] + 1)
            threading.Thread(target=handle_update, args=(app, update), daemon=True).start()


def load_dotenv(path: str = ".env") -> None:
    """Read KEY=value lines from .env so keys don't have to be typed into the shell."""
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


SITE = os.path.join(os.path.dirname(__file__), "site", "index.html")


def _health_server(port: int) -> ThreadingHTTPServer:
    """GET /healthz so a host can tell this process is alive, and GET / for the public landing page.
    The real work is the poll loop below."""
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, head_only: bool):
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                body, kind, code = b"ok", "text/plain", 200
            elif path.startswith("/list/") and getattr(self.server, "list_page", None):
                found = self.server.list_page(path[len("/list/"):])
                if found is None:
                    body, kind, code = b"Not found", "text/plain", 404
                else:
                    title, items = found if isinstance(found, tuple) else ("Shopping list", found)
                    body, kind, code = export.page(items, title).encode(), "text/html; charset=utf-8", 200
            elif path in ("/", "/index.html") and os.path.exists(SITE):
                with open(SITE, "rb") as fh:
                    body, kind, code = fh.read(), "text/html; charset=utf-8", 200
            else:
                body, kind, code = b"Not found", "text/plain", 404
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store" if path.startswith("/list/") else "public, max-age=300")
            self.end_headers()
            if not head_only:
                self.wfile.write(body)

        def do_GET(self):
            self._reply(False)

        def do_HEAD(self):
            self._reply(True)

        def log_message(self, *a):
            pass  # quiet; Telegram polling already prints what matters
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def connect(sender: "TelegramSender", sleep: Callable[[float], None] = time.sleep) -> dict:
    """Check in with Telegram before starting. A rejected token stops Bud; anything else (Telegram's own
    502s, network blips) is waited out, so a brief Telegram outage can't take Bud down for good."""
    delay = 5
    while True:
        try:
            me = sender.call("getMe", {}, 20)
            sender.call("deleteWebhook", {}, 20)  # polling and webhooks can't both be on
            return me
        except TelegramError as err:
            if "(401)" in str(err) or "(404)" in str(err):
                raise SystemExit(f"Telegram rejected the bot token ({err}). Check TELEGRAM_BOT_TOKEN.")
            print(f"(Telegram isn't answering yet: {err}. Trying again in {delay}s.)", flush=True)
            sleep(delay)
            delay = min(delay * 2, 60)


def main() -> None:
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN (get one from @BotFather in Telegram). See DEPLOY.md.")
    sender = TelegramSender(token)
    port = os.environ.get("PORT")
    health = _health_server(int(port)) if port else None   # answer health checks straight away
    me = connect(sender)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    extractor = ClaudeVisionExtractor(api_key) if api_key else None
    interpreter = ClaudeInterpreter(api_key) if api_key else None
    engine = Engine(Store(os.environ.get("FRIDGEBUD_DB", "fridgebud.db")))
    # Everyone on Telegram gets Bud's voice unless the 50/50 voice test is switched back on.
    voice = "" if os.environ.get("FRIDGEBUD_VOICE_TEST") == "1" else "bud"
    fly_app = os.environ.get("FLY_APP_NAME", "")
    public_url = os.environ.get("PUBLIC_URL", "") or (f"https://{fly_app}.fly.dev" if fly_app else "")
    config = Config(number=f"@{me['username']} on Telegram", price=os.environ.get("FRIDGEBUD_PRICE", ""),
                    payment_link=os.environ.get("FRIDGEBUD_PAYMENT_LINK", ""), force_voice=voice, public_url=public_url)
    chef = Chef(api_key) if api_key else None
    agent = Agent(api_key) if api_key else None
    gemini_key = os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")
    watcher = GeminiWatcher(gemini_key) if gemini_key else None
    instacart = Instacart() if os.environ.get("INSTACART_API_KEY") else None
    app = App(Service(engine, config, interpreter, chef, agent, watcher=watcher, instacart=instacart), sender, extractor)
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        from .imagery import Imager
        app.imager = Imager()
    app.shot_delay = 4.0   # wait for the rest of a screenshot album, then answer once
    if health is not None:
        health.list_page = app.service.shopping_page   # /list/<token>: the export page
    app.owners = metrics.owners_from_env()

    print(f"FridgeBud is live on Telegram as @{me['username']}  (https://t.me/{me['username']})")
    print(f"  photos, recipes and free-form chat: {'on' if extractor else 'OFF: add ANTHROPIC_API_KEY to .env'}")
    from .audio import converter
    import shutil
    has_gemini = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
    voice_line = ("off on this machine: no afconvert or ffmpeg (script is sent as text)" if converter() is None
                  else "on (Gemini voice)" if has_gemini
                  else "on (Mac voice; add GEMINI_API_KEY for a natural one)" if shutil.which("say")
                  else "off: add GEMINI_API_KEY (script is sent as text)")
    print(f"  audio cook-along: {voice_line}")
    print(f"  voice: {'Bud for everyone' if voice else '50/50 Bud vs plain test'}")
    print(f"  Instacart links: {'on' if instacart else 'off (no INSTACART_API_KEY)'}")
    print(f"  Recipe pictures: YouTube thumbnails on · AI photos {'on' if getattr(app, 'imager', None) else 'off (no GEMINI_API_KEY)'}")
    print(f"  watching videos: {'on (Gemini)' if watcher else 'off: add GEMINI_API_KEY to .env (free key at aistudio.google.com)'}")
    if port:  # running on a host (Fly, etc.) that health-checks this process over HTTP
        print(f"  health check: GET /healthz on port {port}; landing page at /")
    else:
        print("  Leave this window open. The 4:30 PM text is sent from here. Ctrl+C to stop.")
        print("  Once your Fly deployment is live, this copy notices and stops by itself.")
    if app.owners:
        hour = int(os.environ.get("FRIDGEBUD_DIGEST_HOUR", "9"))
        print(f"  your stats: text STATS any time; morning digest at {hour}:00 Toronto; your chats left out of the numbers")
    else:
        print("  your stats: off. Add FRIDGEBUD_OWNER=tg:<your chat id> to .env to get STATS and the morning digest")
    stop = threading.Event()
    threading.Thread(target=scheduler, args=(app, stop), daemon=True).start()
    if app.owners:
        threading.Thread(target=digest, args=(app, stop, int(os.environ.get("FRIDGEBUD_DIGEST_HOUR", "9"))), daemon=True).start()
    try:
        poll(app, sender.call, stop, step_down=not port)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()


if __name__ == "__main__":
    main()
