"""Чистка мусора readli/AT: счётчики quoter, реклама, слипшиеся заголовки, промо-P.S."""

from backend.downloaders.textclean import clean_html, clean_title


def test_quoter_comment_removed():
    html = '<p>Текст.</p> <br/><!-- quoter = 1; --><div caramel-id="01j78"></div>'
    out = clean_html(html)
    assert "quoter" not in out
    assert "caramel-id" not in out
    assert "Текст." in out


def test_leaked_quoter_text_removed():
    """Уже собранный EPUB: комментарий потерял обёртку и стал голым текстом."""
    out = clean_html("<p>Конец.</p> <br/> quoter = 0; ")
    assert "quoter" not in out
    assert "Конец." in out


def test_ad_blocks_removed():
    html = (
        '<p>А.</p><div class="dc-feed" id="yandex_rtb_R-A-1692355-73"></div>'
        "<script>window.yaContextCb.push(()=>{})</script><p>Б.</p>"
    )
    out = clean_html(html)
    assert "yandex_rtb" not in out and "yaContextCb" not in out
    assert "А." in out and "Б." in out


def test_promo_tail_removed():
    html = (
        "<p>Ёб твою мать, я дома!</p><hr/>P.S. Эта книга находится в процессе "
        "написания, и для того, чтобы быть в курсе публикаций новых глав, "
        "рекомендуем добавить книгу в свою библиотеку либо подписаться на "
        "Автора.<br/>Спасибо.<br/><br/>"
    )
    out = clean_html(html)
    assert "процессе написания" not in out
    assert "Спасибо" not in out
    assert "я дома!" in out


def test_glued_chapter_titles_split():
    assert clean_title("Глава перваяЭскадрон") == "Глава первая. Эскадрон"
    assert clean_title("Глава одиннадцатаяГардемарины") == "Глава одиннадцатая. Гардемарины"
    assert clean_title("Глава 12Рука Фортуны") == "Глава 12. Рука Фортуны"
    assert clean_title("Глава двадцать перваяФинал") == "Глава двадцать первая. Финал"
    assert clean_title("ПрологНачало") == "Пролог. Начало"


def test_correct_titles_untouched():
    for good in (
        "Глава третья. Открытый потенциал",
        "Глава десятая... а для всего остального есть Студик",
        "Глава 12 — Рука Фортуны",
        "Эпилог",
    ):
        assert clean_title(good) == good


def test_empty_inputs():
    assert clean_title("") == ""
    assert clean_title(None) is None
    assert clean_html("") == ""
