"""The FridgeBud web server: landing page, signup, SMS webhook and the 4:30 scheduler.

    python3 -m fridgebud.server

Standard library only. With no Twilio settings it runs in console mode: texts
are printed instead of sent, so you can try the whole flow on your laptop
(see DEPLOY.md). Settings come from environment variables:

    FRIDGEBUD_DB            SQLite file (default fridgebud.db)
    PORT                    default 8080
    PUBLIC_URL              e.g. https://fridgebud.fly.dev (needed to verify Twilio requests)
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_NUMBER
    ANTHROPIC_API_KEY       photo capture and free-form replies; both are off without it
    FRIDGEBUD_PRICE         e.g. "$6.99 a month" (landing page and paywall text)
    FRIDGEBUD_PAYMENT_LINK  the day-14 paywall is off until this is set
    FRIDGEBUD_CONTACT       footer line: who sends the texts and how to reach you (CASL)
    FRIDGEBUD_ADMIN_KEY     protects /admin/*; those routes are off without it
"""
from __future__ import annotations

import hmac
import re
import json
import os
import threading
import time
import traceback
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import audio, messages, metrics, web
from .agent import Agent
from .chef import Chef
from .capture import CapturedItem, ClaudeVisionExtractor
from .engine import Engine
from .fallback import ClaudeInterpreter
from .service import Config, Service
from .sms import ConsoleSender, Sender, TwilioSender, normalize_phone, valid_signature
from .store import Store

MAX_BODY = 64 * 1024
SIGNUPS_PER_IP_PER_HOUR = 5


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class App:
    """Everything the HTTP handler and the scheduler share."""

    def __init__(self, service: Service, sender: Sender, extractor=None, public_url: str = "",
                 auth_token: str = "", admin_key: str = "", contact: str = "", clock=now_utc, synth=audio.synthesize):
        self.service, self.sender, self.extractor, self.synth = service, sender, extractor, synth
        self.public_url, self.auth_token, self.admin_key, self.contact = public_url.rstrip("/"), auth_token, admin_key, contact
        self.clock = clock
        # Recipe screenshots usually arrive as an album: several photos within a second or two.
        # They are collected for `shot_delay` seconds and read as ONE recipe. 0 = handle at once (tests).
        self.shot_delay = 0.0
        self._shots: dict[str, dict] = {}
        self._shots_lock = threading.Lock()
        self._signup_hits: dict[str, list[float]] = {}
        self._hits_lock = threading.Lock()

    # sending ----------------------------------------------------------
    def send(self, phone: str, text) -> None:
        photo = getattr(text, "photo", None)
        if photo and hasattr(self.sender, "send_photo"):
            self._send_photo(phone, photo)          # the picture first, then the card under it
        try:
            if isinstance(text, audio.Audio):
                if self._reuse_audio(phone, text):
                    return
                # Spoken messages: make the audio here (outside the service lock; it can take a minute).
                data = None
                if self.synth and hasattr(self.sender, "send_audio"):
                    self.sender.send(phone, messages.recording(audio.eta_seconds(text.script)))
                    voicing = threading.Event()
                    if hasattr(self.sender, "typing"):
                        threading.Thread(target=self._show_recording, args=(phone, voicing), daemon=True).start()
                    try:
                        data = self.synth(text.script)
                    finally:
                        voicing.set()
                if data:
                    file_id = self.sender.send_audio(phone, data, text.title, text.caption)
                    with self.service.lock:
                        cache = getattr(self.service, "audio_cache", None)
                        if cache and text.key and file_id and cache.script(text.key):
                            cache.save_file_id(text.key, file_id)
                        self.service.store.log(None, self.clock(), "audio_voiced", phone=phone, chars=len(text.script))
                    return
                text = f"{text.title}\n(I can't make audio on this device, so here is the script.)\n\n{text.script}"
            self.sender.send(phone, text)
        except Exception as err:  # one failed text must not stop the rest
            with self.service.lock:
                self.service.store.log(None, self.clock(), "send_error", phone=phone, error=str(err)[:300])

    def _send_photo(self, phone: str, photo: dict) -> None:
        """Never lets a picture problem stop the recipe itself from arriving."""
        caption = photo.get("caption", "")
        try:
            if photo.get("url"):
                self.sender.send_photo_ref(phone, photo["url"], caption)
                return
            key, cache = photo.get("key"), getattr(self.service, "photo_cache", None)
            if not (key and cache):
                return
            with self.service.lock:
                file_id = cache.file_id(key)
            if file_id:
                try:
                    self.sender.send_photo_ref(phone, file_id, caption)
                    return
                except Exception:
                    with self.service.lock:
                        cache.save(key, None, self.clock())
            imager = getattr(self, "imager", None)
            if imager is None:
                return
            with self.service.lock:
                today = self.clock().date().isoformat()
                made = sum(1 for e in self.service.store.events(type_="image_made") if e["at"][:10] == today)
            if made >= imager.daily_cap:
                return
            if hasattr(self.sender, "typing"):
                self.sender.typing(phone, "upload_photo")
            data, mime = imager.draw(photo["prompt"])
            file_id = self.sender.send_photo(phone, data, caption, mime)
            with self.service.lock:
                cache.save(key, file_id, self.clock())
                self.service.store.log(None, self.clock(), "image_made", phone=phone)
        except Exception as err:
            with self.service.lock:
                self.service.store.log(None, self.clock(), "photo_error", phone=phone, error=str(err)[:300])

    def _show_recording(self, phone: str, done: threading.Event) -> None:
        while True:
            self.sender.typing(phone, "upload_voice")   # "sending a voice message…" at the top of the chat
            if done.wait(4.5):
                return

    def _reuse_audio(self, phone: str, msg) -> bool:
        """Already voiced this exact recipe? Resend Telegram's copy: instant, and no Gemini call."""
        cache = getattr(self.service, "audio_cache", None)
        if not (cache and msg.key and hasattr(self.sender, "send_audio_id")):
            return False
        with self.service.lock:
            file_id = cache.file_id(msg.key)
        if not file_id:
            return False
        try:
            self.sender.send_audio_id(phone, file_id, msg.title, msg.caption)
        except Exception:
            with self.service.lock:
                cache.save_file_id(msg.key, None)   # Telegram lost it: voice it again below
            return False
        with self.service.lock:
            self.service.store.log(None, self.clock(), "audio_reused", phone=phone)
        return True

    def tick(self) -> int:
        batch = self.service.tick(self.clock())
        for phone, text in batch:
            self.send(phone, text)
        return len(batch)

    # inbound ----------------------------------------------------------
    def inbound(self, phone: str, text: str, media: list[tuple[str, str]], order: int = 0) -> None:
        """Handle one inbound text and send the replies. Runs on a worker thread:
        reading a photo can take longer than Twilio waits for the webhook."""
        try:
            voices = [m for m in media if m[1].startswith("audio/")]
            if voices:
                transcript = self._voice_note(phone, *voices[0])
                if transcript:   # a spoken request, not their taste: answer it like a typed message
                    self.inbound(phone, transcript, [], order)
                return
            videos = [m for m in media if m[1].startswith("video/")]
            if videos:
                self.send(phone, messages.WATCHING)
                for reply in self._video(phone, *videos[0]):
                    self.send(phone, reply)
                return
            captured: list[CapturedItem] = []
            chef = self.service.chef
            as_recipe = bool(media) and self.service.wants_recipe_photo(phone, text, self.clock())
            recipe_shots: list[tuple[bytes, str]] = []
            menus: list[tuple[bytes, str]] = []
            as_menu = bool(media) and chef is not None and bool(re.search(r"\bmenu\b", text or "", re.I))
            for url, mtype in media:
                if not mtype.startswith("image/") or self.extractor is None:
                    continue
                try:
                    blob = self.sender.fetch_media(url) if hasattr(self.sender, "fetch_media") else b""
                    if not blob:
                        continue
                    if as_menu:
                        menus.append((blob, mtype))
                    elif as_recipe:
                        recipe_shots.append((blob, mtype))
                    elif hasattr(self.extractor, "extract_with_kind"):
                        kind, items = self.extractor.extract_with_kind(blob, mtype)
                        if kind == "recipe" and chef is not None:   # a recipe screenshot, not groceries
                            recipe_shots.append((blob, mtype))
                        elif kind == "menu" and chef is not None:   # a restaurant menu or delivery-app screen
                            menus.append((blob, mtype))
                        else:
                            captured += items
                    else:
                        captured += self.extractor.extract(blob, mtype)
                except Exception as err:
                    with self.service.lock:
                        self.service.store.log(None, self.clock(), "capture_error", phone=phone, error=str(err)[:300])
            if recipe_shots:
                self._queue_shots(phone, recipe_shots, order)
            if menus:
                try:
                    place, dishes = chef.read_menu(*menus[0])
                    replies = self.service.menu_seen(phone, place, dishes, self.clock())
                except Exception as err:
                    with self.service.lock:
                        self.service.store.log(None, self.clock(), "chef_error", phone=phone, error=str(err)[:300])
                    replies = [messages.AI_DOWN]
                for reply in replies:
                    self.send(phone, reply)
            if (recipe_shots or menus) and not captured:
                return
            for reply in self.service.handle_message(phone, text, self.clock(), captured, has_media=bool(media)):
                self.send(phone, reply)
        except Exception:
            traceback.print_exc()

    def _queue_shots(self, phone: str, shots: list[tuple[bytes, str]], order: int) -> None:
        with self._shots_lock:
            entry = self._shots.setdefault(phone, {"items": [], "timer": None})
            entry["items"] += [(order, n, blob, mtype) for n, (blob, mtype) in enumerate(shots)]
            if entry["timer"] is not None:
                entry["timer"].cancel()
            if self.shot_delay > 0:  # wait for the rest of the album before reading any of it
                entry["timer"] = threading.Timer(self.shot_delay, self._flush_shots, [phone])
                entry["timer"].daemon = True
                entry["timer"].start()
                return
        self._flush_shots(phone)

    def _flush_shots(self, phone: str) -> None:
        """Read every waiting screenshot for this person and save them as one recipe, with one reply."""
        try:
            with self._shots_lock:
                entry = self._shots.pop(phone, None)
            if not entry or not entry["items"]:
                return
            if hasattr(self.sender, "typing"):
                self.sender.typing(phone)
            texts, failed = [], 0
            for _, _, blob, mtype in sorted(entry["items"], key=lambda i: (i[0], i[1])):
                try:
                    shot = self.service.chef.read_image(blob, mtype)
                    if shot:
                        texts.append(shot)
                except Exception as err:
                    failed += 1
                    with self.service.lock:
                        self.service.store.log(None, self.clock(), "chef_error", phone=phone, error=str(err)[:300])
            if failed and not texts:
                return self.send(phone, messages.AI_DOWN)
            for reply in self.service.save_recipe_shot(phone, "\n---\n".join(texts) or None, self.clock()):
                self.send(phone, reply)
        except Exception:
            traceback.print_exc()

    def _voice_note(self, phone: str, ref: str, mtype: str) -> str | None:
        """Listen to a voice note. Taste and routine are saved and confirmed here; for anything else, return what
        they said so it can be handled like a typed message."""
        from .video import listen_voice
        watcher = self.service.watcher
        if watcher is None or not hasattr(self.sender, "fetch_media"):
            self.send(phone, messages.VOICE_NEEDS_KEY)
            return None
        try:
            info = listen_voice(watcher, self.sender.fetch_media(ref), mtype)
        except Exception as err:
            with self.service.lock:
                self.service.store.log(None, self.clock(), "voice_error", phone=phone, error=str(err)[:300])
            info = None
        replies = self.service.voice_note(phone, info, self.clock())
        if replies is None:
            return str(info.get("transcript") or "").strip() or None
        for reply in replies:
            self.send(phone, reply)
        return None

    def _video(self, phone: str, ref: str, mtype: str) -> list:
        """A video file (a saved TikTok, a Reel): have the video model watch it, then save the recipe."""
        from .video import MAX_INLINE_BYTES
        watcher = self.service.watcher
        if watcher is None or not hasattr(self.sender, "fetch_media"):
            return [messages.VIDEO_NEEDS_KEY]
        try:
            blob = self.sender.fetch_media(ref, MAX_INLINE_BYTES)
        except Exception as err:
            return [messages.VIDEO_TOO_BIG if "big" in str(err).lower() or "large" in str(err).lower()
                    else messages.video_failed(str(err))]
        try:
            notes = watcher.watch_file(blob, mtype)
        except Exception as err:
            with self.service.lock:
                self.service.store.log(None, self.clock(), "watch_error", phone=phone, error=str(err)[:300])
            return [messages.video_failed("the video model didn't answer")]
        return self.service.save_video(phone, notes, self.clock())

    def allow_signup(self, ip: str) -> bool:
        cutoff = time.time() - 3600
        with self._hits_lock:
            hits = [t for t in self._signup_hits.get(ip, []) if t > cutoff]
            allowed = len(hits) < SIGNUPS_PER_IP_PER_HOUR
            if allowed:
                hits.append(time.time())
            self._signup_hits[ip] = hits
            return allowed


def make_handler(app: App):
    cfg = app.service.config

    class Handler(BaseHTTPRequestHandler):
        server_version = "FridgeBud"

        def log_message(self, fmt, *args):  # keep phone numbers and bodies out of the access log
            print(f"{self.command} {self.path.split('?')[0]} -> {args[1] if len(args) > 1 else ''}", flush=True)

        def _send(self, status: int, body: str, ctype: str = "text/html; charset=utf-8") -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)

        def _form(self) -> dict[str, str]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return {}
            raw = self.rfile.read(length).decode(errors="replace")
            return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

        def _ip(self) -> str:
            forwarded = self.headers.get("Fly-Client-IP") or (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            return forwarded or self.client_address[0]

        def _admin(self, query: dict) -> bool:
            given = (query.get("key") or [""])[0]
            return bool(app.admin_key) and hmac.compare_digest(given, app.admin_key)

        def _landing(self, status: int = 200, **kw) -> None:
            self._send(status, web.landing(cfg.price, cfg.trial_days, app.contact, **kw))

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(url.query)
            if url.path == "/":
                self._landing(ref=(query.get("ref") or [""])[0][:8])
            elif url.path == "/healthz":
                self._send(200, "ok", "text/plain")
            elif url.path == "/admin/scorecard" and self._admin(query):
                with app.service.lock:
                    card = metrics.scorecard(app.service.store, app.clock())
                self._send(200, json.dumps(card, indent=2), "application/json")
            else:
                self._send(404, "Not found", "text/plain")

        def do_POST(self):
            url = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(url.query)
            if url.path == "/signup":
                self._signup(self._form())
            elif url.path == "/sms":
                self._sms(self._form())
            elif url.path == "/admin/paid" and self._admin(query):
                phone = app.service.mark_paid((query.get("phone") or [""])[0], app.clock())
                if phone:
                    app.send(phone, messages.PAID_THANKS)
                self._send(200 if phone else 404, "ok" if phone else "unknown number", "text/plain")
            else:
                self._send(404, "Not found", "text/plain")

        def _signup(self, form: dict[str, str]) -> None:
            phone, ref = form.get("phone", ""), form.get("ref", "")[:8]
            if form.get("consent") != "yes":
                return self._landing(400, error="Please tick the box so Bud is allowed to text you.", phone=phone, ref=ref)
            if not app.allow_signup(self._ip()):
                return self._landing(429, error="Too many signups from here. Try again in an hour.", phone=phone, ref=ref)
            ok, result = app.service.signup_web(phone, app.clock(), ip=self._ip(), consent_wording=web.CONSENT_WORDING, ref=ref)
            if not ok:
                return self._landing(400, error=result, phone=phone, ref=ref)
            app.send(result, messages.WELCOME)
            self._send(200, web.thanks(result))

        def _sms(self, form: dict[str, str]) -> None:
            if app.auth_token:
                given = self.headers.get("X-Twilio-Signature", "")
                if not valid_signature(app.auth_token, app.public_url + self.path, form, given):
                    return self._send(403, "Bad signature", "text/plain")
            phone = normalize_phone(form.get("From", ""))
            if phone:
                count = min(int(form.get("NumMedia") or 0), 4)
                media = [(form.get(f"MediaUrl{i}", ""), form.get(f"MediaContentType{i}", "")) for i in range(count)]
                threading.Thread(target=app.inbound, args=(phone, form.get("Body", ""), media), daemon=True).start()
            # Replies go out through the REST API, so the webhook answers with an empty response.
            self._send(200, '<?xml version="1.0" encoding="UTF-8"?><Response></Response>', "text/xml")

    return Handler


def scheduler(app: App, stop: threading.Event, every: float = 60) -> None:
    while not stop.is_set():
        try:
            app.tick()
        except Exception:
            traceback.print_exc()
        stop.wait(every)


def build_app(env=os.environ) -> App:
    api_key = env.get("ANTHROPIC_API_KEY", "")
    extractor = ClaudeVisionExtractor(api_key) if api_key else None
    interpreter = ClaudeInterpreter(api_key) if api_key else None
    chef = Chef(api_key) if api_key else None
    agent = Agent(api_key) if api_key else None
    engine = Engine(Store(env.get("FRIDGEBUD_DB", "fridgebud.db")))
    config = Config(number=env.get("TWILIO_NUMBER", ""), price=env.get("FRIDGEBUD_PRICE", ""),
                    payment_link=env.get("FRIDGEBUD_PAYMENT_LINK", ""))
    sid, token = env.get("TWILIO_ACCOUNT_SID", ""), env.get("TWILIO_AUTH_TOKEN", "")
    sender: Sender = TwilioSender(sid, token, config.number) if sid and token and config.number else ConsoleSender()
    return App(Service(engine, config, interpreter, chef, agent), sender, extractor, public_url=env.get("PUBLIC_URL", ""),
               auth_token=token if isinstance(sender, TwilioSender) else "", admin_key=env.get("FRIDGEBUD_ADMIN_KEY", ""),
               contact=env.get("FRIDGEBUD_CONTACT", ""))


def main() -> None:
    app = build_app()
    port = int(os.environ.get("PORT", "8080"))
    cfg = app.service.config
    print(f"FridgeBud on port {port}")
    print(f"  texts:   {'Twilio ' + cfg.number if isinstance(app.sender, TwilioSender) else 'console mode (printed, not sent)'}")
    print(f"  photos and free-form replies: {'on' if app.extractor else 'off (no ANTHROPIC_API_KEY)'}")
    print(f"  price:   {cfg.price or 'NOT SET (set FRIDGEBUD_PRICE before sharing the landing page)'}")
    print(f"  paywall: {'on' if cfg.payment_link else 'off (no FRIDGEBUD_PAYMENT_LINK)'}")
    if isinstance(app.sender, TwilioSender) and not app.public_url:
        raise SystemExit("Set PUBLIC_URL so Twilio webhook requests can be verified.")
    stop = threading.Event()
    threading.Thread(target=scheduler, args=(app, stop), daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(app))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
