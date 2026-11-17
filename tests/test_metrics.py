"""The founder's numbers: is Bud used, and is it solving the problem?"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fridgebud import metrics
from fridgebud.engine import Engine
from fridgebud.service import Config, Service
from fridgebud.store import Store
from fridgebud.telegram import wants_stats

TZ = ZoneInfo("America/Toronto")
A, B, ME = "+15195550101", "+15195550102", "tg:999"
MON = datetime(2026, 11, 16, 9, 0, tzinfo=TZ)


def at(day, hour=16, minute=30):
    return (MON + timedelta(days=day)).replace(hour=hour, minute=minute)


@pytest.fixture
def svc():
    return Service(Engine(Store()), Config(number="+12265550000"))


def onboard(svc, phone, items="baby spinach, chicken thighs, milk"):
    for text in ("hi", "yes", "2", f"bought {items}"):
        svc.handle_message(phone, text, MON)


def test_report_answers_used_and_solving_and_leaves_the_owner_out(svc):
    onboard(svc, A)
    onboard(svc, B)
    onboard(svc, ME)                                            # the founder testing; must not count
    svc.handle_message(ME, "used milk", at(1, 18))
    svc.handle_message(A, "used chicken thighs", at(1, 18))
    svc.handle_message(B, "tossed milk", at(1, 18))
    svc.handle_message(A, "blorp zzzt", at(1, 19))             # no agent, no interpreter: a real miss

    rep = metrics.report(svc.store, at(1, 20), exclude={ME})
    u, v, f = rep["usage"], rep["value"], rep["friction"]
    assert u["homes"] == 2 and u["active_today"] == 2 and u["active_7d"] == 2
    assert u["retention"]["day_1"] == {"value": 100.0, "n": 2, "of": 2}
    assert u["retention"]["day_7"]["of"] == 0                     # nobody is a week in yet
    assert v["food"]["used"] == 1 and v["food"]["tossed"] == 1
    assert v["food"]["rescue_rate"]["value"] == 50.0
    assert v["helped_7d"] == 1                                    # A used food up; B only tossed
    assert f["misunderstood"]["n"] == 1 and f["misunderstood_examples"] == ["blorp zzzt"]
    assert u["features"]["Said what happened to food"]["n"] == 2

    text = metrics.render(rep)
    assert "IS IT USED?" in text and "IS IT SOLVING THE PROBLEM?" in text and "WHERE IT BREAKS" in text
    assert "Your own chats are left out." in text and 'Bud didn\'t get: "blorp zzzt"' in text
    assert text.count("\n#") == 2                                 # one line per tester, none for the owner


def test_a_message_the_parser_misses_but_bud_still_handles_is_not_counted_as_a_miss(svc):
    onboard(svc, A)
    svc.handle_message(A, "used chicken thighs", at(1, 18))
    assert not svc.store.events(1, "misunderstood")


def test_telegram_signups_are_labelled_telegram(svc):
    svc.handle_message("tg:42", "hi", MON)
    assert svc.accounts.get(1).source == "telegram"


def test_only_the_owner_can_pull_stats():
    owners = {ME}
    assert wants_stats(ME, "STATS", [], owners) and wants_stats(ME, "/stats", [], owners)
    assert not wants_stats("tg:1", "stats", [], owners)          # a tester just talks to Bud
    assert not wants_stats(ME, "stats", [("f", "image/jpeg")], owners)
    assert not wants_stats(ME, "stats please", [], owners)


# laptop copy hands over to the deployed one ---------------------------------
import threading

from fridgebud.telegram import TelegramError, poll


def _conflicting(times):
    calls = []

    def call(method, params, timeout):
        calls.append(method)
        if len(calls) > times:
            raise AssertionError("kept polling after it should have stopped")
        raise TelegramError("Telegram getUpdates failed (409): Conflict: terminated by other getUpdates request")
    return call, calls


def test_laptop_copy_stops_once_the_deployed_copy_takes_the_bot():
    call, calls = _conflicting(3)
    stop = threading.Event()
    poll(None, call, stop, step_down=True, retry=0)
    assert stop.is_set() and len(calls) == 3


def test_deployed_copy_never_steps_down_and_one_stray_conflict_is_not_a_handover():
    stop = threading.Event()
    seen = []

    def call(method, params, timeout):
        seen.append(1)
        if len(seen) == 6:
            stop.set()
            return []
        raise TelegramError("Telegram getUpdates failed (409): Conflict")
    poll(None, call, stop, step_down=False, retry=0)          # the Fly copy rides it out
    assert len(seen) == 6

    stop2, seen2 = threading.Event(), []

    def once(method, params, timeout):
        seen2.append(1)
        if len(seen2) == 1:
            raise TelegramError("Telegram getUpdates failed (409): Conflict")   # old process's poll after a restart
        stop2.set()
        return []
    poll(None, once, stop2, step_down=True, retry=0)
    assert len(seen2) == 2                                         # carried on after the stray one


# the public landing page, served by the same process ------------------------
import urllib.request

from fridgebud.telegram import _health_server


def test_landing_page_and_health_check_are_served():
    server = _health_server(0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "Try Bud on Telegram" in page and "Bud lives in Telegram" in page
        assert urllib.request.urlopen(base + "/healthz").read() == b"ok"
        try:
            urllib.request.urlopen(base + "/nope")
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as err:
            assert err.code == 404
    finally:
        server.shutdown()


# Telegram having a bad few minutes mustn't take Bud down ---------------------
from fridgebud.telegram import connect


class _Flaky:
    def __init__(self, failures, error="Telegram getMe failed (502): Bad Gateway"):
        self.failures, self.error, self.calls = failures, error, []

    def call(self, method, params, timeout):
        self.calls.append(method)
        if method == "getMe" and self.failures:
            self.failures -= 1
            raise TelegramError(self.error)
        return {"username": "fridgebud_uche_bot"} if method == "getMe" else True


def test_startup_waits_out_telegram_outages():
    waits = []
    me = connect(_Flaky(5), sleep=waits.append)
    assert me["username"] == "fridgebud_uche_bot"
    assert waits == [5, 10, 20, 40, 60]                       # backs off, capped at a minute


def test_startup_stops_on_a_rejected_token():
    with pytest.raises(SystemExit):
        connect(_Flaky(1, "Telegram getMe failed (401): Unauthorized"), sleep=lambda s: None)


# audio on a machine that isn't a Mac -------------------------------------------
import io
import shutil
import subprocess
import wave

from fridgebud import audio


def _wav(seconds=0.2, rate=24000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


def test_ffmpeg_is_used_when_afconvert_is_missing(monkeypatch):
    monkeypatch.setattr(audio.shutil, "which", lambda tool: "/usr/bin/ffmpeg" if tool == "ffmpeg" else None)
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd[0])
        open(cmd[-1], "wb").write(b"m4a-bytes")
    monkeypatch.setattr(audio.subprocess, "run", fake_run)
    assert audio.converter() == "ffmpeg" and audio._to_m4a(_wav()) == b"m4a-bytes" and seen == ["ffmpeg"]


def test_no_converter_means_text_instead_of_audio(monkeypatch):
    monkeypatch.setattr(audio.shutil, "which", lambda tool: None)
    assert audio.converter() is None and audio._to_m4a(_wav()) is None


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed here")
def test_real_ffmpeg_makes_an_m4a(monkeypatch):
    real = shutil.which("ffmpeg")
    monkeypatch.setattr(audio.shutil, "which", lambda tool: real if tool == "ffmpeg" else None)
    out = audio._to_m4a(_wav())
    assert out and out[4:8] == b"ftyp"                          # an MP4/M4A container
