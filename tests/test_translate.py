"""Перевод видимого текста книги (serg/tasks#404).

Проверяется то, что ломает чтение, а не то, что легко проверить:
соответствие абзацев один-к-одному, отказ от перевода уже русского текста,
кэш (второй заход не ходит в движок) и живучесть при сбое движка.
"""

from __future__ import annotations

import json

import pytest

from backend.app.routers import translate as tr


# ---------------------------------------------------------------- detect_lang
@pytest.mark.parametrize(
    "text,expect",
    [
        ("The rain had not stopped for three days.", "other"),
        ("Дождь не прекращался уже три дня.", "ru"),
        ("42", ""),
        ("— — —", ""),
        # Смешанный абзац с одним латинским словом всё ещё русский: переводить
        # его значило бы гонять по кругу уже готовый текст.
        ("Он открыл файл README и закрыл его.", "ru"),
    ],
)
def test_detect_lang(text, expect):
    assert tr.detect_lang(text) == expect


# --------------------------------------------------------------- _parse_items
def test_parse_items_plain_array():
    assert tr._parse_items('["а", "б"]', 2) == ["а", "б"]


def test_parse_items_wrapped_object():
    assert tr._parse_items('{"items": ["а", "б"]}', 2) == ["а", "б"]


def test_parse_items_fenced():
    raw = '```json\n{"items": ["а", "б"]}\n```'
    assert tr._parse_items(raw, 2) == ["а", "б"]


def test_parse_items_rejects_wrong_length():
    """Короткий ответ сдвинул бы перевод относительно абзацев на экране —
    такой батч должен считаться непереведённым, а не применяться частично."""
    assert tr._parse_items('["а"]', 2) is None


def test_parse_items_rejects_garbage():
    assert tr._parse_items("вот перевод: а, б", 2) is None


# ------------------------------------------------------------------- эндпоинт
@pytest.fixture
def gw(monkeypatch):
    """Подменяет вызов движка. Считает запросы и возвращает «ПЕРЕВОД:<текст>»."""
    calls = []

    async def fake_batch(client, texts, target):
        calls.append(list(texts))
        return [f"ПЕРЕВОД:{t}" for t in texts]

    monkeypatch.setattr(tr, "_translate_batch", fake_batch)
    monkeypatch.setattr(tr, "GATEWAY_TOKEN", "test-token")
    return calls


def test_translate_keeps_one_to_one(client, gw):
    items = [
        {"id": "0", "text": "The rain had not stopped."},
        {"id": "1", "text": "Уже был вечер, и стало холодно."},
        {"id": "2", "text": "He said nothing at all about it."},
    ]
    r = client.post("/api/translate", json={"items": items, "target": "ru"})
    assert r.status_code == 200, r.text
    data = r.json()
    # Порядок и состав сохранены: фронт сопоставляет ответ с узлами DOM по id.
    assert [i["id"] for i in data["items"]] == ["0", "1", "2"]
    assert data["items"][0]["text"] == "ПЕРЕВОД:The rain had not stopped."
    assert data["items"][0]["changed"] is True
    # Русский абзац отдан как есть и в движок не отправлялся.
    assert data["items"][1]["text"] == "Уже был вечер, и стало холодно."
    assert data["items"][1]["changed"] is False
    assert data["skipped"] == 1
    assert gw and all("Уже был вечер" not in t for batch in gw for t in batch)


def test_translate_uses_cache_on_second_call(client, gw):
    # Строка уникальна для этого теста: кэш переводов живёт в общей на прогон
    # БД, и текст из соседнего теста пришёл бы уже из кэша.
    items = [{"id": "0", "text": "A cache probe line, unique to this test."}]
    first = client.post("/api/translate", json={"items": items}).json()
    assert first["translated"] == 1 and first["cached"] == 0
    calls_after_first = len(gw)

    second = client.post("/api/translate", json={"items": items}).json()
    assert second["cached"] == 1 and second["translated"] == 0
    assert second["items"][0]["text"] == first["items"][0]["text"]
    # Ключевое: движок второй раз не дёрнут — иначе кэша фактически нет.
    assert len(gw) == calls_after_first


def test_translate_cache_is_content_keyed_across_books(client, gw):
    """Кэш по содержимому, а не по книге: тот же абзац в другой книге и на
    другой позиции берётся из кэша."""
    client.post(
        "/api/translate", json={"items": [{"id": "0", "text": "A shared line."}]}
    )
    n = len(gw)
    res = client.post(
        "/api/translate",
        json={"items": [{"id": "77", "text": "A shared line."}]},
    ).json()
    assert res["cached"] == 1
    assert len(gw) == n


def test_translate_survives_engine_failure(client, monkeypatch):
    """Движок упал — читатель должен получить оригинал, а не пустой экран."""
    monkeypatch.setattr(tr, "GATEWAY_TOKEN", "test-token")

    async def boom(client_, texts, target):
        raise RuntimeError("engine down")

    monkeypatch.setattr(tr, "_translate_batch", boom)
    r = client.post(
        "/api/translate", json={"items": [{"id": "0", "text": "Some english text."}]}
    )
    assert r.status_code == 502


def test_translate_partial_failure_keeps_good_batch(client, monkeypatch):
    """Один батч упал, другой прошёл: отдаём переведённое, а провалившиеся
    абзацы возвращаем оригиналом с changed=False."""
    monkeypatch.setattr(tr, "GATEWAY_TOKEN", "test-token")
    monkeypatch.setattr(tr, "BATCH", 1)
    monkeypatch.setattr(tr, "CONCURRENCY", 2)
    seen = []

    async def flaky(client_, texts, target):
        seen.append(texts[0])
        if "second" in texts[0]:
            raise RuntimeError("engine down")
        return [f"ПЕРЕВОД:{t}" for t in texts]

    monkeypatch.setattr(tr, "_translate_batch", flaky)
    r = client.post(
        "/api/translate",
        json={
            "items": [
                {"id": "0", "text": "The first english line."},
                {"id": "1", "text": "The second english line."},
            ]
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["items"][0]["changed"] is True
    assert data["items"][1]["changed"] is False
    assert data["items"][1]["text"] == "The second english line."
    assert data["failed"] == 1


def test_translate_without_token_is_explicit(client, monkeypatch):
    """Без токена gateway отвечаем 503, а не молчаливым «ничего не изменилось»:
    иначе кнопка выглядит сломанной без объяснения."""
    monkeypatch.setattr(tr, "GATEWAY_TOKEN", "")
    r = client.post(
        "/api/translate", json={"items": [{"id": "0", "text": "English text here."}]}
    )
    assert r.status_code == 503


def test_prompt_payload_shape(monkeypatch):
    """Промпт в Langfuse принимает items/count/target_lang — если поля разъедутся,
    перевод молча выродится в пустой ответ."""
    captured = {}

    class FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {"text": json.dumps({"items": ["раз", "два"]}, ensure_ascii=False)}

    class FakeClient:
        async def post(self, url, json=None, headers=None, timeout=None):  # noqa: A002
            captured["url"] = url
            captured["json"] = json
            return FakeResp()

    import asyncio

    out = asyncio.run(tr._translate_batch(FakeClient(), ["one", "two"], "ru"))
    assert out == ["раз", "два"]
    assert captured["url"].endswith("/infer")
    v = captured["json"]["vars"]
    assert json.loads(v["items"]) == ["one", "two"]
    assert v["count"] == "2"
    assert v["target_lang"] == "ru"
    assert captured["json"]["privacy_class"] == "public"
