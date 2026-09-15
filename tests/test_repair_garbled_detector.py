"""Детектор мусора в scripts/repair_at_garbled_tails.py не путает эмодзи со сбоем расшифровки.

Живой случай 2026-09-15 (serg/tasks#899): после фикса расшифровки по UTF-16 на месте
мусора в главах author.today появились НАСТОЯЩИЕ эмодзи («👽👽👽», «🤝🤱», «🥝»), а
детектор считал мусором любой символ вне BMP — и скрипт починки отказывался
заменять уже правильные файлы. Строки ниже — реальные абзацы из старых и новых
файлов книг 3406, 1489, 46.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_path = Path(__file__).resolve().parents[1] / "scripts" / "repair_at_garbled_tails.py"
_spec = importlib.util.spec_from_file_location("repair_at_garbled_tails", _path)
repair = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repair)


def test_emoji_paragraphs_are_not_garbage():
    for text in (
        "👽👽👽",
        "ям было «🤝🤱». — «Встретил мать», по-моему, вполне очевидно.",
        "P.S. Читает Киви 🥝.",
        "Помогите Котбито найти путь к Минато 🥺",
        "P.S. Спасибо, Егор! 👈",
        # переводы строк и табуляция из разметки абзаца — не мусор (первая версия их помечала)
        chr(10) + "      🐍🐍🐍" + chr(10) + chr(9) + "    ",
    ):
        assert repair._is_garbage(text) is False, text


def test_misdecoded_tails_are_garbage():
    for text in (
        "𦰬\U00012878𧱳h\n��O�KФнжГкѡѢ!бѩtмнвѮЖѬа vѭцвмхtѪДѯяБѩѫ h\n��",
        "\U0001385b𑀍𧡑l(39iO��M",
        "\U00012959{?}28nh.!h",
        "текст с управляющим \x05 символом",
    ):
        assert repair._is_garbage(text) is True, text


def test_plain_russian_text_is_not_garbage():
    assert repair._is_garbage("— Понял, не дурак. Был бы дураком — не понял бы.") is False
