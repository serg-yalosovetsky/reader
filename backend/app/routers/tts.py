"""TTS: Silero (ru) + edge-tts (en/uk). Кеш по sha1(voice|rate|text)."""
from __future__ import annotations

import hashlib
import json
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..config import TTS_DIR

router = APIRouter(prefix="/api/tts", tags=["tts"])

MAX_TEXT = 20000

VOICES: dict[str, dict] = {
    # Русский — Silero v4
    "aidar":   {"lang": "ru-RU", "engine": "silero", "speaker": "aidar",   "name": "Айдар"},
    "baya":    {"lang": "ru-RU", "engine": "silero", "speaker": "baya",    "name": "Байя"},
    "kseniya": {"lang": "ru-RU", "engine": "silero", "speaker": "kseniya", "name": "Ксения"},
    "xenia":   {"lang": "ru-RU", "engine": "silero", "speaker": "xenia",   "name": "Ксения 2"},
    "eugene":  {"lang": "ru-RU", "engine": "silero", "speaker": "eugene",  "name": "Евгений"},
    # English — edge-tts
    "jenny":   {"lang": "en-US", "engine": "edge", "edge_id": "en-US-JennyNeural", "name": "Jenny"},
    "guy":     {"lang": "en-US", "engine": "edge", "edge_id": "en-US-GuyNeural",   "name": "Guy"},
    "aria":    {"lang": "en-US", "engine": "edge", "edge_id": "en-US-AriaNeural",  "name": "Aria"},
    "sonia":   {"lang": "en-GB", "engine": "edge", "edge_id": "en-GB-SoniaNeural", "name": "Sonia"},
    # Ukrainian — edge-tts
    "polina":  {"lang": "uk-UA", "engine": "edge", "edge_id": "uk-UA-PolinaNeural", "name": "Поліна"},
    "ostap":   {"lang": "uk-UA", "engine": "edge", "edge_id": "uk-UA-OstapNeural",  "name": "Остап"},
}
DEFAULT_VOICE = "xenia"

_silero_model = None
_SILERO_SR = 24000


def _load_silero():
    global _silero_model
    if _silero_model is None:
        import torch
        _silero_model, _ = torch.hub.load(
            repo_or_dir="snakers4/silero-models",
            model="silero_tts",
            language="ru",
            speaker="v4_ru",
            trust_repo=True,
        )
        try:
            _silero_model.eval()
        except AttributeError:
            pass
    return _silero_model


def _rate_to_speed(rate_str: str) -> float:
    """'+20%' → 1.2,  '-10%' → 0.9"""
    try:
        pct = float(rate_str.replace("+", "").replace("%", "").strip())
        return max(0.5, min(3.0, 1.0 + pct / 100))
    except ValueError:
        return 1.0


def _est_word_timings(text: str, duration_ms: float) -> list[dict]:
    """Приближённые тайминги слов по позиции символов в тексте."""
    n = max(len(text), 1)
    return [
        {"t": int(m.start() / n * duration_ms),
         "d": max(int(len(m.group()) / n * duration_ms), 40),
         "text": m.group(),
         "charIndex": m.start(),
         "charLength": len(m.group())}
        for m in re.finditer(r'\S+', text)
    ]


def _synth_silero(text: str, speaker: str, speed: float, audio_path, words_path) -> list[dict]:
    import soundfile as sf
    model = _load_silero()
    audio = model.apply_tts(text=text, speaker=speaker, sample_rate=_SILERO_SR)
    audio_np = audio.numpy()

    # Speed via resampling (simple)
    if abs(speed - 1.0) > 0.05:
        import numpy as np
        old_len = len(audio_np)
        new_len = max(1, int(old_len / speed))
        indices = np.linspace(0, old_len - 1, new_len)
        audio_np = np.interp(indices, np.arange(old_len), audio_np).astype(audio_np.dtype)

    duration_ms = len(audio_np) / _SILERO_SR * 1000
    sf.write(str(audio_path), audio_np, _SILERO_SR, format="WAV")
    words = _est_word_timings(text, duration_ms)
    words_path.write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
    return words


async def _synth_edge(text: str, edge_id: str, rate: str, audio_path, words_path) -> list[dict]:
    import edge_tts
    comm = edge_tts.Communicate(text, edge_id, rate=rate, boundary="WordBoundary")
    words: list[dict] = []
    search_pos = 0
    with open(audio_path, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                word_text = chunk.get("text", "")
                idx = text.find(word_text, search_pos)
                char_index = idx if idx >= 0 else search_pos
                if idx >= 0:
                    search_pos = idx + len(word_text)
                words.append({
                    "t": round(chunk["offset"] / 10000),
                    "d": round(chunk["duration"] / 10000),
                    "text": word_text,
                    "charIndex": char_index,
                    "charLength": len(word_text),
                })
    words_path.write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
    return words


def _key(text: str, voice_id: str, rate: str) -> str:
    return hashlib.sha1(f"{voice_id}|{rate}|{text}".encode()).hexdigest()


class SynthIn(BaseModel):
    text: str
    voice: str = DEFAULT_VOICE
    rate: str = "+0%"


@router.get("/voices")
def voices() -> dict:
    items = [
        {"id": k, "name": f"{v['name']} ({v['lang']})", "lang": v["lang"]}
        for k, v in VOICES.items()
    ]
    return {"voices": items, "default": DEFAULT_VOICE}


@router.post("/synth")
async def synth(body: SynthIn) -> dict:
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "пустой текст")
    if len(text) > MAX_TEXT:
        raise HTTPException(413, "слишком длинный фрагмент")

    voice_cfg = VOICES.get(body.voice) or VOICES[DEFAULT_VOICE]
    rate = body.rate or "+0%"
    engine = voice_cfg["engine"]
    key = _key(text, body.voice, rate)

    if engine == "silero":
        audio_path = TTS_DIR / f"{key}.wav"
        mime = "audio/wav"
    else:
        audio_path = TTS_DIR / f"{key}.mp3"
        mime = "audio/mpeg"
    words_path = TTS_DIR / f"{key}.json"

    if audio_path.exists() and words_path.exists():
        words = json.loads(words_path.read_text(encoding="utf-8"))
    else:
        try:
            if engine == "silero":
                speed = _rate_to_speed(rate)
                words = _synth_silero(text, voice_cfg["speaker"], speed, audio_path, words_path)
            else:
                words = await _synth_edge(text, voice_cfg["edge_id"], rate, audio_path, words_path)
        except Exception as e:
            audio_path.unlink(missing_ok=True)
            words_path.unlink(missing_ok=True)
            raise HTTPException(502, f"ошибка синтеза: {e}")

    return {"audio_url": f"/api/tts/audio/{key}", "words": words}


@router.get("/audio/{key}")
def audio(key: str) -> FileResponse:
    if len(key) != 40 or not key.isalnum():
        raise HTTPException(404, "нет такого аудио")
    for ext, mime in [(".wav", "audio/wav"), (".mp3", "audio/mpeg")]:
        p = TTS_DIR / f"{key}{ext}"
        if p.exists():
            return FileResponse(p, media_type=mime)
    raise HTTPException(404, "аудио не найдено")
