"""Расшифровка глав author.today идёт по UTF-16-юнитам, как в JavaScript (serg/tasks#899).

Живой случай 2026-09-15, книга 3406 «Спокойный мир для нелюдя», «Глава 7»: последний
абзац превращался в мусор («𦰬𒡸𧱳h ��O�КФнжГкѡѢ…»). author.today шифрует текст
XOR'ом по charCodeAt — то есть по UTF-16-юнитам: символ вне BMP (эмодзи, значки)
занимает ДВА юнита. Python после json.loads видит такой символ ОДНИМ code point, и
индекс ключа после каждого из них уезжает на единицу — всё, что дальше, мусор. В
главе 7 было три таких символа, и хвост «Спасибо за подарки, очень приятно.»
расшифровывался только ключом со сдвигом ровно на 3.
"""

from __future__ import annotations

from backend.downloaders import authortoday as at

# Ключ собирается, а не пишется литералом: иначе gitleaks принимает его за API-ключ.
SECRET = "".join(chr(ord("a") + i % 26) for i in range(32))  # 32 символа, как у reader-secret
USER_ID = "315633"


def _utf16_units(s: str) -> list[int]:
    b = s.encode("utf-16-le", "surrogatepass")
    return [int.from_bytes(b[i:i + 2], "little") for i in range(0, len(b), 2)]


def _js_encrypt(plain: str, secret: str, user_id: str) -> str:
    """Шифрование так, как это делает сайт: XOR по UTF-16-юнитам (JS charCodeAt),
    затем строка приезжает в JSON и Python собирает суррогатные пары обратно."""
    key = _utf16_units(secret[::-1] + "@_@" + user_id)
    units = _utf16_units(plain)
    enc = [u ^ key[i % len(key)] for i, u in enumerate(units)]
    raw = b"".join(u.to_bytes(2, "little") for u in enc)
    return raw.decode("utf-16-le", "surrogatepass")


def test_plain_bmp_text_roundtrip():
    plain = "<p>Обычный текст главы без эмодзи.</p>" * 5
    assert at._decrypt(_js_encrypt(plain, SECRET, USER_ID), SECRET, USER_ID) == plain


def test_text_after_astral_symbols_is_not_garbled():
    plain = (
        "<p>Спасибо за подарки 🎁❤️, очень приятно. Ещё 𓀀 и 😀 внутри.</p>"
        "<p>— Понял, не дурак. Был бы дураком — не понял бы.</p>"
    ) * 4
    enc = _js_encrypt(plain, SECRET, USER_ID)
    assert at._decrypt(enc, SECRET, USER_ID) == plain


def test_anonymous_key_without_user_id():
    plain = "<p>Глава для анонима 🙂 и продолжение после эмодзи.</p>" * 3
    assert at._decrypt(_js_encrypt(plain, SECRET, ""), SECRET, "") == plain


def test_encrypted_units_that_form_lone_surrogates_survive():
    # XOR может дать одиночный суррогат в зашифрованной строке — расшифровка не должна падать.
    plain = "😀 текст" * 10  # собранный эмодзи 😀 в виде пары
    plain = plain.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")
    enc = _js_encrypt(plain, SECRET, USER_ID)
    assert at._decrypt(enc, SECRET, USER_ID) == plain
