"""Выбор загрузчика по URL и цепочка фоллбэков.

Порядок:
1. author.today  -> собственный адаптер (этап 2b);
2. известные FanFicFare-домены -> FanFicFare;
3. иначе -> FanFicFare (вдруг знает), при UnsupportedURL -> FicHub,
   а если и он не знает -> webarticle (обычная веб-страница как книга).
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from . import fanficfare_engine as fff
from . import fichub
from .base import DownloaderError, DownloadResult, PaidContentError, UnsupportedURL

log = logging.getLogger("reader.chain")


def is_url(s: str) -> bool:
    s = s.strip()
    return s.startswith("http://") or s.startswith("https://")


def fetch(query: str, creds: tuple[str, str] | None = None) -> DownloadResult:
    """Скачать по ссылке либо по названию.

    Если query — URL, идём по адаптерам/FanFicFare/FicHub. Если это просто
    название — ищем книгу в бесплатных агрегаторах (searchfloor → readli).
    creds (username, password) пробрасываются в FanFicFare для закрытого/18+."""
    if not is_url(query):
        title = query.strip()
        r = _search_free(title)
        if r:
            return r
        raise DownloaderError(
            f"По названию «{title}» ничего не найдено в бесплатных источниках "
            "(searchfloor/readli). Попробуйте вставить прямую ссылку на фанфик "
            "(ficbook.net, fanfics.me, author.today, AO3, fanfiction.net)."
        )
    url = query.strip()
    host = (urlparse(url).hostname or "").lower()
    opts = {"_creds": creds} if creds else None

    # 1) сайты со своими адаптерами.
    if host.endswith("author.today"):
        from . import authortoday

        try:
            res = authortoday.download(url, creds=creds)
        except PaidContentError as e:
            # Текст на AT недоступен (за деньги ЛИБО 18+ без входа) — ищем
            # полную книгу в бесплатных источниках. Причину несём дальше: от неё
            # зависит, что делать человеку, если зеркал тоже нет.
            return _fallback_free(e.title, e.author, getattr(e, "reason", "paid"))
        # Частично-платная (пролог бесплатно, хвост за деньги): AT отдал только
        # доступные главы. Ищем более полную бесплатную версию на зеркалах и
        # берём вариант с бо́льшим объёмом текста.
        if res.extra.get("partial_paid"):
            try:
                free = _search_free(res.title, res.author)
            except DownloaderError:
                free = None
            if free and _richness(free) > _richness(res):
                return free
        return res
    if host.endswith("readli.net"):
        from . import readli

        return readli.download(url)
    if host.endswith("searchfloor.org"):
        from . import searchfloor

        return searchfloor.download(url)

    # 2) известные FanFicFare-домены.
    if fff.supports(url):
        return fff.download(url, extra_options=opts)

    # 3) попытка FanFicFare, затем FicHub.
    try:
        return fff.download(url, extra_options=opts)
    except UnsupportedURL:
        pass
    try:
        return fichub.download(url)
    except DownloaderError:
        # Не фанфик, а обычная веб-страница (блог, лонгрид, разбор) — собираем
        # книгу из самой статьи: текст + встроенные картинки. Несколько ссылок
        # одной серией собирает /api/ingest/web (backend/downloaders/webarticle).
        from . import webarticle

        return webarticle.download(url)


def _fallback_free(title: str, author: str, reason: str = "paid") -> DownloadResult:
    """Текст на AT недоступен → пробуем найти полную книгу на бесплатных зеркалах.

    Если зеркал нет, ошибка обязана назвать НАСТОЯЩУЮ причину. Раньше оба
    случая — «за деньги» и «18+ без входа» — печатались как «Книга платная», и
    это уводило в сторону покупки книг, которые бесплатны (serg/tasks#319:
    work 46 и work 58 — «Читать книгу» на сайте, но `unadulted` в каждой главе
    при неработающем входе)."""
    r = _search_free(title, author) if title else None
    if r:
        return r
    if reason == "adult":
        raise DownloaderError(
            f"На author.today книга помечена 18+ и без входа в аккаунт не отдаётся "
            f"(проверьте учётку author.today), а в бесплатных источниках "
            f"(searchfloor/readli) не найдена: «{title}»."
        )
    raise DownloaderError(
        f"Книга платная на author.today, а в бесплатных источниках "
        f"(searchfloor/readli) не найдена: «{title}»."
    )


def _richness(result: "DownloadResult") -> int:
    """Грубая мера полноты книги — длина извлекаемого ТЕКСТА (символы).
    Надёжнее размера в байтах при сравнении разных форматов/зеркал (fb2 vs epub)."""
    import os
    import re
    import zipfile

    try:
        p = str(result.file_path)
        fmt = (result.file_format or "").lower()
        if fmt == "epub" or p.lower().endswith(".epub"):
            z = zipfile.ZipFile(p)
            parts = []
            for n in z.namelist():
                if n.lower().endswith((".xhtml", ".html", ".htm")):
                    parts.append(
                        re.sub(r"<[^>]+>", " ", z.read(n).decode("utf-8", "ignore"))
                    )
            return len(" ".join(parts))
        with open(p, encoding="utf-8", errors="ignore") as fh:
            data = fh.read()
        # fb2 <binary> — base64 обложки, не текст: иначе жирная обложка
        # «перевешивает» лишнюю главу другого зеркала.
        data = re.sub(r"<binary\b[^>]*>.*?</binary>", " ", data, flags=re.S)
        return len(re.sub(r"<[^>]+>", " ", data))
    except Exception:  # noqa: BLE001
        try:
            return os.path.getsize(result.file_path)
        except Exception:  # noqa: BLE001
            return 0


def _chapters(result: "DownloadResult") -> int:
    """Число реальных глав в скачанном варианте (структурная мера)."""
    from ..app.services import _real_chapters
    try:
        return _real_chapters(result.file_path, result.file_format)
    except Exception:  # noqa: BLE001
        return 0


def fetch_fullest(
    title: str,
    author: str = "",
    primary_url: str | None = None,
    creds: tuple[str, str] | None = None,
    descriptor: dict | None = None,
) -> DownloadResult | None:
    """Скачать книгу из ВСЕХ доступных источников и вернуть САМЫЙ ПОЛНЫЙ вариант.

    Источники: текущий primary_url + зеркала, найденные поиском (author.today,
    searchfloor, readli). Каждый кандидат верифицируется book_identity.same_book
    (защита от тёзок/чужих томов), полнота меряется _richness (длина текста).
    descriptor — наш дескриптор {title,author,annotation,file_path,file_format}
    для сверки идентичности. None — ни один источник не отдал валидный вариант."""
    from ..app import book_identity as bi

    cands: list[DownloadResult] = []

    def _consider(res: "DownloadResult | None") -> None:
        if not res or not getattr(res, "file_path", None):
            return
        if descriptor:
            cand = {
                "title": res.title or "",
                "author": res.author or "",
                "annotation": (res.extra or {}).get("annotation", ""),
            }
            gtb = lambda: bi.extract_text_sample(res.file_path, res.file_format)
            gta = (
                (lambda: bi.extract_text_sample(
                    descriptor.get("file_path"), descriptor.get("file_format")))
                if descriptor.get("file_path")
                else None
            )
            try:
                if not bi.same_book(descriptor, cand, get_text_a=gta, get_text_b=gtb):
                    return
            except Exception:  # noqa: BLE001 — сверка не должна валить докачку
                pass
        cands.append(res)

    # 1) текущий источник (как есть, с creds для закрытого контента)
    if primary_url:
        try:
            _consider(fetch(primary_url, creds=creds))
        except Exception:  # noqa: BLE001
            pass
    # 2) author.today по названию/автору (volume-aware search_work)
    if title:
        try:
            from . import authortoday

            at_url = authortoday.search_work(title, author)
            if at_url and at_url != primary_url:
                _consider(authortoday.download(at_url))
        except Exception:  # noqa: BLE001
            pass
        # 3) searchfloor
        try:
            from . import searchfloor

            bid = searchfloor.search_book(title, author)
            if bid:
                sf_url = f"https://searchfloor.org/b/{bid}"
                if sf_url != primary_url:
                    _consider(searchfloor._download_book(bid, sf_url))
        except Exception:  # noqa: BLE001
            pass
        # 4) readli
        try:
            from . import readli

            _consider(readli.search_and_download(title, author))
        except Exception:  # noqa: BLE001
            pass

    if not cands:
        return None
    # Полнота — по тексту, но структурный апгрейд (больше реальных глав при
    # почти равном объёме, >=90%) перевешивает: свежий источник с лишней
    # главой не должен проигрывать толстому, но отставшему зеркалу.
    best = max(cands, key=_richness)
    best_rich, best_ch = _richness(best), _chapters(best)
    for _r in cands:
        if _chapters(_r) > best_ch and _richness(_r) >= best_rich * 0.9:
            best, best_rich, best_ch = _r, _richness(_r), _chapters(_r)
    return best


def _search_free(title: str, author: str = "") -> DownloadResult | None:
    """Проверяем ВСЕ бесплатные зеркала (searchfloor + readli) и возвращаем самый
    ПОЛНЫЙ вариант (по длине текста), а не первый успешный."""
    if not title:
        return None
    cands: list[DownloadResult] = []
    from . import searchfloor

    try:
        bid = searchfloor.search_book(title, author)
        if bid:
            cands.append(
                searchfloor._download_book(bid, f"https://searchfloor.org/b/{bid}")
            )
    except DownloaderError:
        pass
    from . import readli

    try:
        r = readli.search_and_download(title, author)
        if r:
            cands.append(r)
    except DownloaderError:
        pass
    # Зеркало обязано быть ТОЙ ЖЕ книгой. Поиск по названию в серии легко
    # возвращает соседний том (живой случай: запрос «Вечно голодный студент 9»
    # → readli отдавал том 5), а вызывающий код подменял книге файл и переводил
    # на неё подписку. same_book сравнивает и номер тома в названии.
    from ..app.book_identity import same_book, title_matches

    want = {"title": title, "author": author}
    kept = []
    for _c in cands:
        cand = {"title": _c.title, "author": _c.author or ""}
        # Автора в запросе нет (искали по одному названию) — сверять его не с чем:
        # author_relation отдаёт UNKNOWN, аннотации и файла у запроса тоже нет, и
        # same_book консервативно отвечает «разные», отбрасывая КАЖДОЕ зеркало.
        # Единственный доступный сигнал — название, и он уже проверен: похожесть
        # + номер тома (том 5 вместо запрошенного 9 отсекается по-прежнему).
        ok = same_book(want, cand) or (
            not (author or "").strip() and title_matches(title, _c.title)
        )
        if ok:
            kept.append(_c)
        else:
            log.info(
                "_search_free: отброшено зеркало «%s» — это не «%s»", _c.title, title
            )
    cands = kept
    if not cands:
        return None
    # Полнота — по тексту, но структурный апгрейд (больше реальных глав при
    # почти равном объёме, >=90%) перевешивает: свежий источник с лишней
    # главой не должен проигрывать толстому, но отставшему зеркалу.
    best = max(cands, key=_richness)
    best_rich, best_ch = _richness(best), _chapters(best)
    for _r in cands:
        if _chapters(_r) > best_ch and _richness(_r) >= best_rich * 0.9:
            best, best_rich, best_ch = _r, _richness(_r), _chapters(_r)
    return best
