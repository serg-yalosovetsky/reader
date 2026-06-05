"""Роутер TTS: озвучивание текста через edge-tts с пословными таймингами.

Синтез кэшируется на диск по ключу sha1(voice|rate|text): аудио (mp3) + JSON со
списком слов [{t, d, text}] (миллисекунды от начала + произнесённое слово), снятых
из событий WordBoundary edge-tts. Клиент играет аудио и подсвечивает слово, сверяя
audio.currentTime с таймингами.

edge-tts по умолчанию отдаёт SentenceBoundary — пословные тайминги включаются
явным параметром boundary="WordBoundary" (проверено для русских голосов).
"""
from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..config import TTS_DIR

router = APIRouter(prefix="/api/tts", tags=["tts"])

# Короткое имя голоса -> идентификатор edge-tts.
VOICES = {
    "svetlana": "ru-RU-SvetlanaNeural",
    "dmitry": "ru-RU-DmitryNeural",
}
DEFAULT_VOICE = "svetlana"
MAX_TEXT = 20000  # клиент синтезирует посекционно/кусками; длиннее — ошибка


class SynthIn(BaseModel):
    text: str
    voice: str = DEFAULT_VOICE
    rate: str = "+0%"  # формат edge-tts: "+0%", "+20%", "-10%"


def _key(text: str, voice_id: str, rate: str) -> str:
    return hashlib.sha1(f"{voice_id}|{rate}|{text}".encode("utf-8")).hexdigest()


async def _synthesize(text: str, voice_id: str, rate: str, audio_path, words_path) -> list[dict]:
    """Синтез edge-tts: пишем mp3 на диск, собираем пословные тайминги."""
    import edge_tts

    comm = edge_tts.Communicate(text, voice_id, rate=rate, boundary="WordBoundary")
    words: list[dict] = []
    with open(audio_path, "wb") as f:
        async for chunk in comm.stream():
            kind = chunk["type"]
            if kind == "audio":
                f.write(chunk["data"])
            elif kind == "WordBoundary":
                # offset/duration в единицах по 100 нс → миллисекунды.
                words.append({
                    "t": round(chunk["offset"] / 10000),
                    "d": round(chunk["duration"] / 10000),
                    "text": chunk.get("text", ""),
                })
    words_path.write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
    return words


@router.get("/voices")
def voices() -> dict:
    """Список доступных голосов (для UI)."""
    return {"voices": [{"id": k, "name": v} for k, v in VOICES.items()], "default": DEFAULT_VOICE}


@router.post("/synth")
async def synth(body: SynthIn) -> dict:
    """Синтезировать речь для фрагмента. Возвращает url аудио и тайминги слов.

    Async-эндпоинт: edge-tts ходит в сеть (MS), не блокируя event loop.
    """
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "пустой текст")
    if len(text) > MAX_TEXT:
        raise HTTPException(413, "слишком длинный фрагмент — синтезируйте по частям")
    voice_id = VOICES.get(body.voice, VOICES[DEFAULT_VOICE])
    rate = body.rate or "+0%"

    key = _key(text, voice_id, rate)
    audio_path = TTS_DIR / f"{key}.mp3"
    words_path = TTS_DIR / f"{key}.json"

    if audio_path.exists() and words_path.exists():
        words = json.loads(words_path.read_text(encoding="utf-8"))
    else:
        try:
            words = await _synthesize(text, voice_id, rate, audio_path, words_path)
        except Exception as e:  # noqa: BLE001
            audio_path.unlink(missing_ok=True)
            words_path.unlink(missing_ok=True)
            raise HTTPException(502, f"ошибка синтеза речи: {e}")
    return {"audio_url": f"/api/tts/audio/{key}", "words": words}


@router.get("/audio/{key}")
def audio(key: str) -> FileResponse:
    """Отдать кэшированное аудио. FileResponse поддерживает Range (seek в плеере)."""
    if len(key) != 40 or not key.isalnum():
        raise HTTPException(404, "нет такого аудио")
    path = TTS_DIR / f"{key}.mp3"
    if not path.exists():
        raise HTTPException(404, "аудио не найдено")
    return FileResponse(path, media_type="audio/mpeg")
