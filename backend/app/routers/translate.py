"""Перевод видимого текста книги на русский.

POST /api/translate  {items: [{id, text}], target?: "ru"} -> {items: [{id, text}], ...}

Движок — не свой, а inference-gateway меша (:8796): он сам выбирает машину по
паре (privacy_class, compute_class) и переживает падение отдельного провайдера,
переключаясь на следующий. Своя модель на VPS не ставится осознанно: GPU там
нет, и LLM на 12 ядрах EPYC давала бы 15-40 с на абзац — для листания
непригодно.

Класс приватности здесь НЕ задаётся: он живёт в шлюзе — дефолтом проекта
`reader` и метадатой промпта (`privacy: public`), которую видно и можно менять в
Langfuse без деплоя читалки. Пока это `public`: текст изданной книги не личные
данные, а разрешение облачных провайдеров даёт широкий пул бесплатных
(cerebras/groq/gemini/mistral/cloudflare/sambanova/nvidia) вместо одного-двух
no-train. Прибитое в коде per-request значение перебивало бы обе эти настройки,
и смена приватности в шлюзе ни на что бы не влияла.

Кэш переводов — в БД, ключом по содержимому абзаца (sha256), а не по позиции в
книге: возврат назад, перечитывание и одинаковые абзацы в разных книгах
обслуживаются без сети. Ключ включает пару языков — иначе перевод на другой
язык затирал бы предыдущий.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field as PField
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from ..db.models import Translation
from ..db.session import engine

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["translate"])

# Адрес и токен gateway. Токен — per-project, минтится в gateway и лежит в
# secrets-gateway; сюда попадает через окружение сервиса.
GATEWAY_URL = os.getenv("READER_INFGW_URL", "http://127.0.0.1:8796")
GATEWAY_TOKEN = os.getenv("READER_INFGW_TOKEN", "")
# Промпт живёт в Langfuse (правится в UI без деплоя читалки), его метадата
# задаёт compute_class=light — переводу не нужна сильная модель.
PROMPT_CODE = os.getenv("READER_TRANSLATE_PROMPT", "reader.translate")
# Сколько абзацев отдаём модели одним запросом. Замер на живом gateway
# (12 абзацев, sambanova_free/gemma-4-31B): один батч из 12 — 11.4 с, три
# параллельных по 4 — 15.7 с, шесть по 2 — 47 с. Бесплатный провайдер
# сериализует запросы и штрафует за частоту, поэтому дробить экран невыгодно:
# берём его целиком одним запросом.
BATCH = int(os.getenv("READER_TRANSLATE_BATCH", "24"))
# По той же причине параллельности нет: она замедляет, а не ускоряет. Батчи
# уходят по очереди, и первым — видимый экран.
CONCURRENCY = int(os.getenv("READER_TRANSLATE_CONCURRENCY", "1"))
TIMEOUT = float(os.getenv("READER_TRANSLATE_TIMEOUT", "60"))

# Потолки на один запрос. Экран книги — это десятки абзацев и тысячи знаков;
# всё, что заметно больше, приходит не от читалки, а от того, кто решил
# погонять чужой платный движок за счёт этого эндпоинта. Без потолков сюда
# принимались тело на 2 МБ и 5000 абзацев.
MAX_ITEMS = int(os.getenv("READER_TRANSLATE_MAX_ITEMS", "80"))
MAX_ITEM_CHARS = int(os.getenv("READER_TRANSLATE_MAX_ITEM_CHARS", "4000"))
MAX_TOTAL_CHARS = int(os.getenv("READER_TRANSLATE_MAX_TOTAL_CHARS", "40000"))

# Куда переводим. Whitelist, а не свободная строка: `target` подставляется в
# промпт, и произвольный текст оттуда — это инструкция модели, а не язык.
# Проверяющий уже получал через это поле ответ «verifier-pwned-7788» вместо
# перевода. Список — языки, на которых Серж реально читает.
ALLOWED_TARGETS = {"ru", "en", "uk", "de", "fr", "es", "pl", "it"}
DEFAULT_TARGET = "ru"

_CYR = re.compile(r"[Ѐ-ӿ]")
_LAT = re.compile(r"[A-Za-z]")


def detect_lang(text: str) -> str:
    """Грубое определение: нужен ответ только на вопрос «это уже русский?».

    Полноценный детектор языка тут был бы лишней зависимостью: решение бинарное,
    а кириллица против латиницы различает интересующий случай надёжно. 'ru' для
    кириллического текста, 'other' для латинского, '' — когда букв почти нет
    (числа, разделители: переводить нечего).
    """
    cyr = len(_CYR.findall(text))
    lat = len(_LAT.findall(text))
    if cyr + lat < 4:
        return ""
    return "ru" if cyr > lat else "other"


class Item(BaseModel):
    # id — ярлык узла DOM на стороне фронта, в промпт он не попадает; длину
    # всё равно ограничиваем, чтобы тело запроса не раздували им.
    id: str = PField(max_length=64)
    text: str = PField(max_length=100_000)


class TranslateReq(BaseModel):
    items: list[Item] = PField(default_factory=list)
    target: str = "ru"


def _key(text: str, src: str, dst: str) -> str:
    h = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
    return f"{src}:{dst}:{h}"


def _cache_get(keys: list[str]) -> dict[str, str]:
    if not keys:
        return {}
    with Session(engine) as s:
        rows = s.exec(select(Translation).where(Translation.key.in_(keys))).all()
        return {r.key: r.text for r in rows}


def _cache_put(pairs: list[tuple[str, str]]) -> None:
    """Записать переводы в кэш, не падая на гонке.

    Два экрана, переведённые одновременно, попадают на общий абзац регулярно.
    Проверка «нет ли уже такого ключа» перед вставкой от этого не спасает: между
    SELECT и INSERT успевает вставить сосед, и UNIQUE на `key` роняет запрос —
    причём ПОСЛЕ того, как инференс уже оплачен, а вместе с трейсом в трекер
    ошибок уезжает текст книги. Поэтому конфликт разрешает сама СУБД.
    """
    if not pairs:
        return
    rows = [{"key": k, "text": t} for k, t in dict(pairs).items()]
    table = Translation.__table__
    dialect = engine.dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _insert
    else:  # незнакомый диалект: вставляем по одной, конфликт гасим откатом
        with Session(engine) as s:
            for row in rows:
                try:
                    s.add(Translation(**row))
                    s.commit()
                except IntegrityError:
                    s.rollback()
        return
    with Session(engine) as s:
        s.exec(_insert(table).values(rows).on_conflict_do_nothing(index_elements=["key"]))
        s.commit()


async def _translate_batch(
    client: httpx.AsyncClient, texts: list[str], target: str
) -> list[str]:
    """Один запрос к gateway на батч абзацев. Возвращает список той же длины.

    Формат ответа — JSON-массив строк: нумерованный текст модель охотно
    переформатирует (склеивает абзацы, теряет пустые), а массив либо парсится,
    либо явно ломается — и тогда батч честно считается непереведённым.
    """
    payload = {
        "prompt_code": PROMPT_CODE,
        "vars": {
            "target_lang": target,
            "items": json.dumps(texts, ensure_ascii=False),
            "count": str(len(texts)),
        },
        "output": "json",
    }
    r = await client.post(
        f"{GATEWAY_URL}/infer",
        json=payload,
        headers={"Authorization": f"Bearer {GATEWAY_TOKEN}"},
        timeout=TIMEOUT,
    )
    if r.status_code >= 400:
        # Подробности — в лог, наружу нейтрально: ответ чужого сервиса может
        # нести и его внутренние детали, и эхо нашего запроса.
        log.warning("translate: gateway %s: %s", r.status_code, r.text[:400])
        raise HTTPException(502, "движок перевода недоступен")
    data = r.json()
    raw = data.get("text") or data.get("content") or ""
    out = _parse_items(raw, len(texts))
    if out is None:
        raise HTTPException(502, "движок вернул неразбираемый ответ")
    return out


def _parse_items(raw: str, n: int) -> list[str] | None:
    """Достать список из ответа модели. None — если формат не тот.

    Модель может обернуть массив в объект ({"items": [...]}) или в ```json```:
    это не повод терять готовый перевод, поэтому разбираем оба случая, но
    длину проверяем строго — короткий список сдвинул бы абзацы относительно
    текста на экране.
    """
    s = (raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    try:
        obj = json.loads(s)
    except Exception:
        return None
    if isinstance(obj, dict):
        for k in ("items", "translations", "result", "texts"):
            if isinstance(obj.get(k), list):
                obj = obj[k]
                break
    if not isinstance(obj, list) or len(obj) != n:
        return None
    return [str(x) if x is not None else "" for x in obj]


@router.post("/translate")
async def translate(req: TranslateReq) -> dict:
    """Перевести присланные абзацы. Уже русские возвращаются как есть.

    Порядок и состав items сохраняются: фронт сопоставляет ответ с узлами DOM
    по id, поэтому пропуск элемента сдвинул бы текст в книге.
    """
    target = (req.target or DEFAULT_TARGET).strip().lower()
    if target not in ALLOWED_TARGETS:
        raise HTTPException(400, f"язык {target[:20]!r} не поддерживается")
    if not req.items:
        return {"items": [], "translated": 0, "cached": 0, "skipped": 0}
    if len(req.items) > MAX_ITEMS:
        raise HTTPException(413, f"слишком много абзацев: {len(req.items)} > {MAX_ITEMS}")
    total = sum(len(it.text or "") for it in req.items)
    if total > MAX_TOTAL_CHARS:
        raise HTTPException(413, f"слишком много текста: {total} > {MAX_TOTAL_CHARS} знаков")
    if not GATEWAY_TOKEN:
        raise HTTPException(503, "перевод не настроен: нет READER_INFGW_TOKEN")

    # Что переводить, что отдать как есть.
    need: dict[str, str] = {}  # key -> исходный текст
    plan: list[tuple[str, str | None]] = []  # (id, key) — None: перевод не нужен
    skipped = 0
    for it in req.items:
        text = (it.text or "").strip()
        src = detect_lang(text)
        # Абзац длиннее потолка — не наш случай: в книгах таких нет, а в один
        # батч он утащил бы весь бюджет запроса.
        if len(text) > MAX_ITEM_CHARS:
            plan.append((it.id, None))
            skipped += 1
            continue
        if not text or src == "" or src == target:
            plan.append((it.id, None))
            skipped += 1
            continue
        k = _key(text, src, target)
        plan.append((it.id, k))
        need.setdefault(k, text)

    cached = await asyncio.to_thread(_cache_get, list(need.keys()))
    todo = [(k, t) for k, t in need.items() if k not in cached]

    fresh: dict[str, str] = {}
    if todo:
        batches = [todo[i : i + BATCH] for i in range(0, len(todo), BATCH)]
        sem = asyncio.Semaphore(CONCURRENCY)
        async with httpx.AsyncClient() as client:

            async def run(batch: list[tuple[str, str]]) -> None:
                async with sem:
                    outs = await _translate_batch(client, [t for _, t in batch], target)
                    for (k, _), out in zip(batch, outs):
                        fresh[k] = out

            results = await asyncio.gather(
                *(run(b) for b in batches), return_exceptions=True
            )
        # Часть батчей могла упасть: отдаём то, что перевелось. Экран с двумя
        # непереведёнными абзацами полезнее пустой ошибки на весь экран.
        errs = [r for r in results if isinstance(r, Exception)]
        if errs and not fresh:
            e = errs[0]
            if isinstance(e, HTTPException):
                raise e
            raise HTTPException(502, f"перевод не удался: {type(e).__name__}")
        for e in errs:
            log.warning("translate: батч не удался: %s", e)
        await asyncio.to_thread(_cache_put, list(fresh.items()))

    have = {**cached, **fresh}
    src_by_id = {it.id: (it.text or "") for it in req.items}
    items = []
    for pid, key in plan:
        if key is None:
            items.append({"id": pid, "text": src_by_id.get(pid, ""), "changed": False})
        elif key in have:
            items.append({"id": pid, "text": have[key], "changed": True})
        else:
            # Батч упал — оставляем оригинал, фронт не подменяет этот абзац.
            items.append({"id": pid, "text": src_by_id.get(pid, ""), "changed": False})
    return {
        "items": items,
        "translated": len(fresh),
        "cached": len(cached),
        "skipped": skipped,
        "failed": sum(1 for _, k in plan if k is not None and k not in have),
    }
