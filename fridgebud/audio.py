"""Turn a cook-along script into an audio file.

Two voices, best first:
1. Gemini text-to-speech (needs GEMINI_API_KEY): a natural, expressive voice that can be told
   how to deliver the lines. FRIDGEBUD_TTS_VOICE picks the voice (default Charon).
2. The Mac's built-in `say` voice: free and offline, but flat. Used when there is no Gemini key
   or Gemini fails. FRIDGEBUD_VOICE picks the Mac voice.

Either way the result is an .m4a file, which needs macOS (`afconvert`). On other machines
`synthesize` returns None and the script is sent as text instead.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass

from .capture import Transport, _http_post

TTS_API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
TTS_MODELS = ("gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts")
STYLE = ("Read this aloud the way a popular podcast host talks: calm, warm and conversational, at a natural everyday "
         "pace. Keep it engaging but understated. Do not stretch words, do not act it out, no dramatic pauses. "
         "Read every word exactly as written and do not read punctuation out loud:\n\n")
CHUNK_CHARS = 650    # a voice model can cut a long passage short, so the script is spoken a paragraph at a time
PAUSE = b"\x00\x00" * 6000   # a quarter of a second of silence between passages (24 kHz, 16-bit)

_FRACTIONS = {"1/2": "half", "1/3": "a third", "2/3": "two thirds", "1/4": "a quarter", "3/4": "three quarters", "1/8": "an eighth"}


@dataclass
class Audio:
    """An outbound message that should be spoken. `script` is also the text fallback."""
    title: str
    script: str
    caption: str = ""
    key: str = ""        # audiocache key: same key, same audio, so it can be reused instead of re-made


def speakable(script: str) -> str:
    """Tidy a script so a voice reads it the way a person would say it."""
    text = script
    text = re.sub(r"(\d+) (\d)/(\d)", lambda m: f"{m.group(1)} and {'a half' if m.group(2) + '/' + m.group(3) == '1/2' else _FRACTIONS.get(m.group(2) + '/' + m.group(3), m.group(2) + ' over ' + m.group(3))}", text)
    text = re.sub(r"\b(\d)/(\d)\b", lambda m: _FRACTIONS.get(m.group(0), f"{m.group(1)} over {m.group(2)}"), text)
    text = re.sub(r"(\d+)\s*°?\s*F\b", r"\1 degrees Fahrenheit", text)
    text = re.sub(r"(\d+)\s*°\s*C\b", r"\1 degrees Celsius", text)
    text = re.sub(r"\btbsp\b\.?", "tablespoons", text, flags=re.I)
    text = re.sub(r"\btsp\b\.?", "teaspoons", text, flags=re.I)
    text = re.sub(r"(\d+)\s*g\b", r"\1 grams", text)
    text = re.sub(r"(\d+)\s*ml\b", r"\1 millilitres", text, flags=re.I)
    text = re.sub(r"\(([^()]*)\)", r", \1,", text)            # brackets become a spoken aside
    text = re.sub(r"[^\w\s.,;:!?'\-%&]", " ", text)           # symbols and emoji a voice would stumble on
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)              # " ," is what makes a voice say "comma"
    text = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", text)
    text = re.sub(r",\s*([.!?])", r"\1", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def converter() -> str | None:
    """What turns a WAV into an .m4a here: afconvert on a Mac, ffmpeg anywhere else (the Fly machine)."""
    return next((tool for tool in ("afconvert", "ffmpeg") if shutil.which(tool)), None)


def _to_m4a(wav: bytes) -> bytes | None:
    tool = converter()
    if tool is None:
        return None
    with tempfile.TemporaryDirectory() as folder:
        src, out = os.path.join(folder, "in.wav"), os.path.join(folder, "out.m4a")
        with open(src, "wb") as fh:
            fh.write(wav)
        cmd = (["afconvert", "-f", "m4af", "-d", "aac", "-b", "64000", src, out] if tool == "afconvert"
               else ["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-c:a", "aac", "-b:a", "64k", out])
        try:
            subprocess.run(cmd, capture_output=True, timeout=120, check=True)
            with open(out, "rb") as fh:
                return fh.read()
        except (subprocess.SubprocessError, OSError):
            return None


def eta_seconds(script: str) -> int:
    """Rough time to voice a script: each ~650-character passage is one voice request of several seconds."""
    return 5 + 8 * max(1, -(-len(script) // CHUNK_CHARS))


def chunks(script: str, limit: int = CHUNK_CHARS) -> list[str]:
    """Split a script into passages of whole sentences."""
    out, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", script.strip()):
        if current and len(current) + len(sentence) + 1 > limit:
            out.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    return out + ([current] if current else [])


def gemini_wav(script: str, api_key: str, transport: Transport | None = None, voice: str | None = None) -> bytes | None:
    """Speech from Gemini as a WAV file (24 kHz mono), or None if it didn't work."""
    post = transport or (lambda url, headers, body: _http_post(url, headers, body, timeout=180))
    voice = voice or os.environ.get("FRIDGEBUD_TTS_VOICE", "Charon")
    options = [(TTS_MODELS[0], voice), (TTS_MODELS[1], voice), (TTS_MODELS[1], "Kore")]

    def speak(text: str, model: str, name: str) -> bytes | None:
        body = {"contents": [{"parts": [{"text": STYLE + text}]}],
                "generationConfig": {"responseModalities": ["AUDIO"],
                                     "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": name}}}}}
        reply = post(TTS_API.format(model=model), {"x-goog-api-key": api_key, "content-type": "application/json"},
                     json.dumps(body).encode())
        parts = ((reply.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        data = next((p["inlineData"]["data"] for p in parts if p.get("inlineData", {}).get("data")), None)
        return base64.b64decode(data) if data else None

    pcm, working = [], None
    for passage in chunks(script):
        spoken = None
        for model, name in ([working] if working else options):
            for _attempt in range(2):          # one retry: free-tier speech calls fail now and then
                try:
                    spoken = speak(passage, model, name)
                except Exception as err:
                    print(f"(Gemini voice, {model}/{name}: {str(err)[:120]})", flush=True)
                    spoken = None
                    if "error 429" in str(err):
                        break                   # quota's gone for this model/voice; a retry won't help, move on
                if spoken:
                    break
            if spoken:
                working = (model, name)
                break
        if not spoken:
            return None                        # never send half a recipe; fall back to the Mac voice
        pcm.append(spoken)
    if not pcm:
        return None
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(PAUSE.join(pcm))
    return buffer.getvalue()


def mac_say(script: str, voice: str | None = None) -> bytes | None:
    if not shutil.which("say"):
        return None
    voice = voice or os.environ.get("FRIDGEBUD_VOICE", "")
    pick = ["-v", voice] if voice else []
    with tempfile.TemporaryDirectory() as folder:
        text, m4a, aiff = (os.path.join(folder, n) for n in ("script.txt", "cookalong.m4a", "cookalong.aiff"))
        with open(text, "w") as fh:
            fh.write(script)
        try:
            direct = subprocess.run(["say", *pick, "-f", text, "-o", m4a, "--file-format=m4af", "--data-format=aac"],
                                    capture_output=True, timeout=180)
            if direct.returncode != 0 or not os.path.exists(m4a):
                subprocess.run(["say", *pick, "-f", text, "-o", aiff], capture_output=True, timeout=180, check=True)
                subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", aiff, m4a], capture_output=True, timeout=180, check=True)
            with open(m4a, "rb") as fh:
                return fh.read()
        except (subprocess.SubprocessError, OSError):
            return None


def synthesize(script: str) -> bytes | None:
    """An .m4a of the script being read, or None if this machine can't make one."""
    script = speakable(script)
    key = os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")
    if key:
        wav = gemini_wav(script, key)
        m4a = _to_m4a(wav) if wav else None
        if m4a:
            return m4a
    return mac_say(script)
