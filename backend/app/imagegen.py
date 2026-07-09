"""Генерация обложек ИИ для книг без картинки.

Провайдер-абстракция (READER_IMAGE_PROVIDER):
  * ``comfy``        — локальный ComfyUI (FLUX.1-dev) по READER_COMFY_URL;
  * ``pollinations`` — бесплатный hosted-эндпоинт (FLUX), без ключа;
  * ``openai``       — gpt-image-1 (нужен OPENAI_API_KEY);
  * ``auto`` (деф.)  — ComfyUI, если доступен, иначе Pollinations.

Возвращают сырые байты картинки (JPEG/PNG) либо None. Само сохранение файлом —
в :mod:`covers` (``generate_cover``)."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from urllib.parse import quote

from . import config

log = logging.getLogger("reader.imagegen")

# Обложка книги: вертикаль 2:3. Кратно 16 (требование латент-сеток FLUX/SD).
_W, _H = 768, 1152


# --------------------------------------------------------------------------- #
#  Промпт
# --------------------------------------------------------------------------- #
def build_prompt(meta: dict) -> str:
    """Собрать художественный промпт из метаданных книги.

    Русские title/fandom/жанры оставляем как есть (проприетарные имена), а
    художественную рамку задаём по-английски — модели так адресуют стиль
    заметно лучше. Явно просим «без текста»: ИИ рисует буквы криво, а заголовок
    UI и так показывает рядом с обложкой."""
    title = (meta.get("title") or "").strip()
    fandom = (meta.get("fandom") or "").strip()
    desc = (meta.get("description") or "").strip()
    brief = (meta.get("cover_brief") or "").strip()

    # Хвост качества + жёсткий анти-текст. ВАЖНО: слова "book cover"/"poster"/
    # "title" и название в кавычках заставляют модель (особенно flux у
    # Pollinations, где нет негатив-промпта) РИСОВАТЬ кривой заголовок текстом.
    tail = (
        "Dramatic lighting, rich detail, sharp focus, evocative mood, painterly. "
        "Clean wordless artwork with absolutely no text, no letters, no title, "
        "no typography, no captions, no watermark, no signature, no border."
    )

    # Если есть арт-бриф (Ollama свела книгу в англ. визуальную сцену) — он и есть
    # лучший промпт: конкретная сцена, а не сырая русская аннотация.
    if brief:
        return f"Cinematic concept art, atmospheric matte painting. {brief} {tail}"

    genres = ""
    raw = meta.get("genres") or ""
    if raw:
        try:
            g = json.loads(raw) if raw.strip().startswith("[") else [raw]
            genres = ", ".join(str(x) for x in g if x)[:120]
        except Exception:  # noqa: BLE001
            genres = str(raw)[:120]

    # Фолбэк без брифа: название как тема сцены (без "cover/title"), стиль concept
    # art. Вертикаль книги держим размером картинки (_W×_H), а не словами.
    parts = ["Cinematic concept art, atmospheric matte painting."]
    if title:
        parts.append(f"A scene evoking the mood and imagery of {title}.")
    if fandom:
        parts.append(f"Setting: {fandom}.")
    if genres:
        parts.append(f"Genre and mood: {genres}.")
    if desc:
        parts.append(f"Scene: {desc[:240]}")
    parts.append(tail)
    return " ".join(parts)


# --------------------------------------------------------------------------- #
#  Арт-бриф (Ollama сводит книгу в англ. визуальную сцену)
# --------------------------------------------------------------------------- #
_BRIEF_SYS = (
    "You write a single vivid English visual scene description for a book cover "
    "illustration. Given the title, genres and synopsis (which may be in Russian "
    "or Ukrainian), output ONE striking sentence (max 45 words): concrete imagery, "
    "the main subject, mood, lighting and colour palette. The scene must contain "
    "NO text or letters. Output ONLY the sentence — no preamble, no quotes."
)


def summarize(meta: dict) -> str | None:
    """Свести книгу в короткий англ. визуальный арт-бриф через Ollama (SergPC).
    None, если брифы выключены/Ollama недоступна/пустой ответ."""
    if not config.BRIEF_ENABLED or not config.OLLAMA_URL:
        return None
    title = (meta.get("title") or "").strip()
    if not title:
        return None

    genres = ""
    raw = meta.get("genres") or ""
    if raw:
        try:
            g = json.loads(raw) if raw.strip().startswith("[") else [raw]
            genres = ", ".join(str(x) for x in g if x)[:200]
        except Exception:  # noqa: BLE001
            genres = str(raw)[:200]
    desc = (meta.get("description") or "").strip()[:600]
    usr = f"Title: {title}\nGenres: {genres}\nSynopsis: {desc}"

    import httpx

    try:
        with httpx.Client(timeout=httpx.Timeout(90.0, connect=6.0)) as c:
            r = c.post(
                f"{config.OLLAMA_URL}/api/chat",
                json={
                    "model": config.OLLAMA_MODEL,
                    "stream": False,
                    "think": False,
                    "messages": [
                        {"role": "system", "content": _BRIEF_SYS},
                        {"role": "user", "content": usr},
                    ],
                    "options": {"temperature": 0.7, "num_predict": 160},
                },
            )
        if r.status_code != 200:
            log.warning("Ollama brief %s: %s", r.status_code, r.text[:150])
            return None
        text = (r.json().get("message", {}).get("content") or "").strip()
        # Иногда модель добавляет кавычки/префикс — чистим до одной строки.
        text = text.strip().strip('"').strip()
        text = " ".join(text.split())
        return text[:600] or None
    except Exception as e:  # noqa: BLE001
        log.warning("Ollama brief недоступен: %s", e)
        return None


def _seed(meta: dict, salt: str = "") -> int:
    """Детерминированный seed из sha1/заголовка (одинаковая книга → тот же арт;
    salt меняем при force-регенерации)."""
    key = (meta.get("sha1") or meta.get("title") or "x") + salt
    return int(hashlib.sha1(key.encode("utf-8", "ignore")).hexdigest()[:12], 16)


# --------------------------------------------------------------------------- #
#  Диспетчер
# --------------------------------------------------------------------------- #
def generate(meta: dict, salt: str = "", provider: str | None = None) -> bytes | None:
    """Сгенерировать байты обложки. ``provider`` перекрывает глобальный дефолт
    (нужно батчу: ленивый путь = pollinations, батч = comfy)."""
    if not config.IMAGE_GEN_ENABLED:
        return None
    prompt = build_prompt(meta)
    seed = _seed(meta, salt)
    provider = (provider or config.IMAGE_PROVIDER).strip().lower()

    if provider == "comfy":
        return _gen_comfy(prompt, seed)
    if provider == "pollinations":
        return _gen_pollinations(prompt, seed)
    if provider == "openai":
        return _gen_openai(prompt)
    # auto: локальный ComfyUI (если поднят), иначе бесплатный Pollinations.
    if config.COMFY_URL and _comfy_alive():
        data = _gen_comfy(prompt, seed)
        if data:
            return data
        log.warning("ComfyUI дал сбой — откатываюсь на Pollinations")
    return _gen_pollinations(prompt, seed)


# --------------------------------------------------------------------------- #
#  Pollinations (бесплатно, без ключа)
# --------------------------------------------------------------------------- #
def _gen_pollinations(prompt: str, seed: int) -> bytes | None:
    import httpx

    url = (
        f"https://image.pollinations.ai/prompt/{quote(prompt[:1500])}"
        f"?width={_W}&height={_H}&nologo=true&model=flux&seed={seed % 1_000_000}"
    )
    try:
        with httpx.Client(timeout=config.IMAGE_TIMEOUT, follow_redirects=True) as c:
            r = c.get(url)
        if r.status_code == 200 and r.content[:3] in (b"\xff\xd8\xff", b"\x89PN"):
            return r.content
        log.warning("Pollinations вернул %s (%d байт)", r.status_code, len(r.content))
    except Exception as e:  # noqa: BLE001
        log.warning("Pollinations недоступен: %s", e)
    return None


# --------------------------------------------------------------------------- #
#  ComfyUI (локальный FLUX.1-dev)
# --------------------------------------------------------------------------- #
_COMFY_CLIENT_ID = "reader-cover-gen"


def _comfy_alive() -> bool:
    import httpx

    try:
        with httpx.Client(timeout=3) as c:
            return c.get(f"{config.COMFY_URL}/system_stats").status_code == 200
    except Exception:  # noqa: BLE001
        return False


def _comfy_workflow(prompt: str, seed: int) -> dict:
    """API-граф ComfyUI под all-in-one чекпоинт flux1-dev-fp8 (UNet+CLIP+VAE)."""
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": config.COMFY_CKPT},
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["1", 1]},
        },
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["1", 1]}},
        "4": {
            "class_type": "FluxGuidance",
            "inputs": {"guidance": 3.5, "conditioning": ["2", 0]},
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": _W, "height": _H, "batch_size": 1},
        },
        "6": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": 20,
                "cfg": 1.0,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
                "model": ["1", 0],
                "positive": ["4", 0],
                "negative": ["3", 0],
                "latent_image": ["5", 0],
            },
        },
        "7": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["6", 0], "vae": ["1", 2]},
        },
        "8": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "readercover", "images": ["7", 0]},
        },
    }


def _gen_comfy(prompt: str, seed: int) -> bytes | None:
    import httpx

    base = config.COMFY_URL
    graph = _comfy_workflow(prompt, seed)
    deadline = time.monotonic() + config.IMAGE_TIMEOUT
    try:
        # Таймаут на ОТДЕЛЬНЫЙ HTTP-запрос держим коротким: сама генерация
        # ожидается опросом /history, а не долгим висящим запросом. Иначе через
        # tailscale serve длинный idle-коннект рвётся 502-й.
        with httpx.Client(timeout=30.0) as c:
            # POST /prompt с ретраями: во время холодного релоада модели ComfyUI
            # (или его прокси) может разово отдать 502/не-JSON.
            pid = None
            for _ in range(6):
                try:
                    r = c.post(
                        f"{base}/prompt",
                        json={"prompt": graph, "client_id": _COMFY_CLIENT_ID},
                    )
                    if r.status_code == 200:
                        pid = r.json().get("prompt_id")
                        if pid:
                            break
                    else:
                        log.debug("ComfyUI /prompt %s: %s", r.status_code, r.text[:120])
                except Exception as e:  # noqa: BLE001 — транзиентный сбой прокси
                    log.debug("ComfyUI /prompt retry: %s", e)
                time.sleep(3.0)
            if not pid:
                log.warning("ComfyUI: не удалось поставить задачу (нет prompt_id)")
                return None

            # Опрос /history устойчив к разовым 502/reset/не-JSON (холодный
            # релоад делает ComfyUI на секунды неотзывчивым) — ретраим до дедлайна.
            hist = None
            while time.monotonic() < deadline:
                try:
                    resp = c.get(f"{base}/history/{pid}")
                    if resp.status_code == 200:
                        h = resp.json()
                        if pid in h and h[pid].get("outputs"):
                            hist = h[pid]
                            break
                except Exception as e:  # noqa: BLE001 — транзиент, продолжаем опрос
                    log.debug("ComfyUI /history retry: %s", e)
                time.sleep(1.5)
            if not hist:
                log.warning(
                    "ComfyUI: таймаут ожидания генерации (%ss)", config.IMAGE_TIMEOUT
                )
                return None

            img = None
            for out in hist.get("outputs", {}).values():
                if out.get("images"):
                    img = out["images"][0]
                    break
            if not img:
                log.warning("ComfyUI: в истории нет картинки")
                return None

            for _ in range(4):  # /view тоже может разово 502 через прокси
                try:
                    v = c.get(
                        f"{base}/view",
                        params={
                            "filename": img["filename"],
                            "subfolder": img.get("subfolder", ""),
                            "type": img.get("type", "output"),
                        },
                    )
                    if v.status_code == 200 and v.content:
                        return v.content
                except Exception as e:  # noqa: BLE001
                    log.debug("ComfyUI /view retry: %s", e)
                time.sleep(2.0)
            log.warning("ComfyUI: не удалось скачать готовую картинку")
            return None
    except Exception as e:  # noqa: BLE001
        log.warning("ComfyUI ошибка генерации: %s", e)
        return None


# --------------------------------------------------------------------------- #
#  OpenAI gpt-image-1 (задел; нужен OPENAI_API_KEY)
# --------------------------------------------------------------------------- #
def _gen_openai(prompt: str) -> bytes | None:
    import base64

    import httpx

    if not config.OPENAI_API_KEY:
        log.warning("OpenAI-провайдер выбран, но OPENAI_API_KEY пуст")
        return None
    try:
        with httpx.Client(timeout=config.IMAGE_TIMEOUT) as c:
            r = c.post(
                "https://api.openai.com/v1/images/generations",
                headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
                json={
                    "model": "gpt-image-1",
                    "prompt": prompt[:4000],
                    "n": 1,
                    "size": "1024x1536",
                },
            )
            r.raise_for_status()
            b64 = r.json()["data"][0]["b64_json"]
            return base64.b64decode(b64)
    except Exception as e:  # noqa: BLE001
        log.warning("OpenAI ошибка генерации: %s", e)
        return None
