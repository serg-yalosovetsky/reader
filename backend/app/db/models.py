"""Модели данных (SQLModel / SQLite).

Схема покрывает все этапы плана, но на этапе 1 реально используются Work и Progress.
Account / Monitored задействуются на этапе 4, SyncState — на этапе 3.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import uuid

from sqlalchemy import Index, text
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Work(SQLModel, table=True):
    """Произведение (фанфик/книга), известное читалке."""

    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = ""
    author: str = ""
    # Источник: ficbook | fanfics | authortoday | ao3 | ffn | calibre | upload
    site: str = ""
    source_url: str = ""
    # Файл на диске (EPUB/FB2), который рендерит читалка и который уходит в ReadEra.
    file_path: str = ""
    file_format: str = ""  # epub | fb2
    # SHA-1 файла — линчпин синхронизации с ReadEra (doc_sha1).
    sha1: str = Field(default="", index=True)
    # Привязка к Calibre, если книга добавлена/взята оттуда.
    calibre_id: Optional[int] = Field(default=None, index=True)
    chapters_count: int = 0
    cover_path: str = ""
    # Источник обложки: "" | embedded (из файла) | source (og:image) |
    # description (URL в аннотации) | generated (ИИ) | gen_failed (генерация не
    # удалась — не долбим повторно). generated/gen_failed заменяемы реальной.
    cover_source: str = ""
    # Англ. визуальный арт-бриф (Ollama сводит книгу), кеш для промпта обложки.
    cover_brief: str = ""
    # --- Метаданные для карточки/страницы книги (тянутся 1 раз из epub-opf при
    #     добавлении; бэкфилл существующих — из локального epub, без сети). ---
    description: str = ""  # аннотация (dc:description)
    genres: str = ""  # JSON-массив жанров/меток (dc:subject, очищенные)
    characters: str = ""  # JSON-массив персонажей (если удалось выделить)
    fandom: str = ""  # фандом/вселенная (для кроссоверов)
    series: str = ""  # цикл/серия (fb2 <sequence>, epub calibre:series, AT «Цикл»)
    series_index: int = 0  # номер книги в серии (1,2,3…); 0 — неизвестно
    rating: str = ""  # NC-17 | R | PG-13 | 18+ …
    status: str = ""  # в процессе | завершён
    words: int = 0  # объём в словах (если известно)
    meta_synced: bool = False  # метаданные уже разобраны (чтобы не тянуть снова)
    # --- EPUB-версия книги с фиксированной вёрсткой (PDF → EPUB, calibre) ---
    # Оригинал остаётся в file_path; сюда пишется производный файл, который
    # читалка отдаёт вместо PDF (перетекающий текст, темы, TTS, подсветки).
    converted_path: str = ""
    # "" (не пробовали) | pending | ready | failed
    converted_status: str = ""
    converted_error: str = ""  # последняя ошибка конвертации (видна в UI)
    created_at: datetime = Field(default_factory=utcnow)
    # ВНИМАНИЕ: updated_at — «последняя активность», его двигает и сохранение
    # прогресса чтения (см. PUT /api/progress). Как «дата выхода новых глав»
    # он НЕ годится: страница книги показывала им дату собственного чтения.
    updated_at: datetime = Field(default_factory=utcnow)
    # Когда менялось СОДЕРЖИМОЕ: докачаны главы, заменён файл. Это и есть
    # «обновлено» для человека. Значение по умолчанию ставится здесь, а не в
    # пяти местах создания Work (upload, calibre, drive, docker-загрузка,
    # регистрация): раскладывать его руками — тот же способ однажды забыть
    # строку. Обновляют дату только настоящие смены контента (_apply_file и
    # пересчёт глав по файлу). None остаётся у записей, созданных до появления
    # поля, — их заполняет бэкфилл по mtime файла (см. db/session.py).
    content_updated_at: Optional[datetime] = Field(default_factory=utcnow)
    # Скрытая книга не показывается в сетке библиотеки и находится только
    # поиском (serg/tasks#923). Это ВИДИМОСТЬ, а не архив: файл, прогресс,
    # подписки и докачка новых глав продолжают работать как раньше.
    hidden: bool = False


class Progress(SQLModel, table=True):
    """Прогресс чтения по произведению. Одна строка на work_id."""

    id: Optional[int] = Field(default=None, primary_key=True)
    work_id: int = Field(foreign_key="work.id", index=True, unique=True)
    # Доля прочитанного 0..1 — совместимо с ReadEra doc_position.ratio.
    ratio: float = 0.0
    # Точный локатор для foliate-js (CFI/href#frag) для возврата на место в вебе.
    locator: str = ""
    # Текстовый якорь — первые слова текста вверху экрана. Устойчив к пересборке
    # книги (FanFicFare добавил главы → CFI съезжает на другую секцию), поэтому
    # это основной способ восстановления позиции; locator/ratio — фолбэки.
    text_anchor: str = ""
    # Время последнего чтения (для last-write-wins при sync с ReadEra).
    last_read_time: datetime = Field(default_factory=utcnow)
    # Сколько глав было в книге, когда эту позицию сохранили. Нужен, чтобы
    # понять, что книга с тех пор выросла: доля 0.99 от 190 глав — это уже не
    # 0.99 от 197, и «дочитано» становится враньём. 0 — старая запись, до
    # появления поля.
    chapters_at_read: int = 0
    # Название главы, в которой стоит позиция. В восстановлении не участвует —
    # нужно подписью: когда позицию затирают, снимок уезжает в историю, и без
    # главы он читается как безымянные «43%».
    chapter: str = ""
    # Откуда пришло обновление: web | readera
    source: str = "web"


class PositionHistory(SQLModel, table=True):
    """Прошлые позиции чтения — чтобы «Назад» в читалке было куда нажимать.

    Progress хранит ОДНУ строку на книгу, и её перезаписывает любой релокейт.
    Из-за этого позиция терялась без следа: книга открыта в двух вкладках, во
    второй она стоит на первой странице — её сохранение затирает главу 100, и
    вернуться человеку некуда. Теперь прежняя позиция уезжает сюда: и при таком
    затирании (сервер видит скачок доли), и при явном прыжке из читалки
    (оглавление, ссылка, перемотка шкалой).

    Записи живут пачкой на книгу (последние MAX_HISTORY), старые вытесняются.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    work_id: int = Field(foreign_key="work.id", index=True)
    # Та же тройка координат, что и в Progress: якорь — основной способ
    # восстановления, locator/ratio — фолбэки (см. frontend/js/core/position.js).
    ratio: float = 0.0
    locator: str = ""
    text_anchor: str = ""
    # Название главы на момент снимка: список переходов должен читаться
    # человеком, а «43%» без главы не говорит ничего.
    chapter: str = ""
    # Почему позиция попала в историю: jump (явный переход из читалки),
    # overwrite (её затёрла другая вкладка/устройство), open (место, с которого
    # книгу открыли).
    reason: str = "jump"
    created_at: datetime = Field(default_factory=utcnow, index=True)


class ChapterMeta(SQLModel, table=True):
    """Главы книги с датами публикации — для оглавления (serg/tasks#1080).

    В самих файлах книг дат нет: FanFicFare пишет в EPUB только chapterurl и
    заголовки. Зато при запросе метаданных (`--json-meta`) он отдаёт `zchapters`
    с датой на каждую главу — у форумов (SufficientVelocity, SpaceBattles) с
    точностью до секунды, у ficbook до минуты. Эти даты и складываем сюда: файл
    книги не трогаем, а оглавление получает время выхода каждой главы.

    Ключ для сопоставления с оглавлением — url главы: номер ненадёжен, потому
    что в оглавлении EPUB бывают служебные страницы («Title Page»), из-за
    которых нумерация съезжает.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    work_id: int = Field(foreign_key="work.id", index=True)
    # Номер главы у источника (1..N), а не позиция в оглавлении файла.
    number: int = 0
    title: str = ""
    url: str = Field(default="", index=True)
    # Когда глава опубликована на сайте. None — источник даты не отдал; это
    # ОТДЕЛЬНОЕ состояние, а не «сегодня»: в оглавлении такая глава просто
    # остаётся без даты.
    published_at: Optional[datetime] = None
    # Кто дал данные: fff (FanFicFare) | at (свой загрузчик author.today).
    source: str = ""
    fetched_at: datetime = Field(default_factory=utcnow)


class Account(SQLModel, table=True):
    """Аккаунт пользователя на сайте-источнике (этап 4). Секрет зашифрован Fernet."""

    id: Optional[int] = Field(default=None, primary_key=True)
    site: str = Field(index=True)
    username: str = ""
    enc_secret: str = ""  # зашифрованный пароль
    cookies: str = ""  # зашифрованные cookie-сессии (опц.)
    last_check: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)


class Monitored(SQLModel, table=True):
    """Отслеживаемое произведение/подписка (этап 4)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: Optional[int] = Field(
        default=None, foreign_key="account.id", index=True
    )
    work_id: Optional[int] = Field(default=None, foreign_key="work.id", index=True)
    source_url: str = ""
    last_seen_chapters: int = 0
    # Хост источника, В ЕДИНИЦАХ КОТОРОГО посчитан last_seen_chapters. Разные
    # сайты меряют разное: readli отдаёт СТРАНИЦЫ пагинации, остальные — ГЛАВЫ.
    # Пока подписка не меняет источник, это неважно; но стоит перенацелить её
    # на зеркало другого класса (живой случай: 84 страницы readli против 25
    # глав author.today), и сравнение «на сайте больше, чем видели» становится
    # ложным навсегда — книга замирает недокачанной, не показывая ошибки.
    # Пустая строка = историческая запись, единицы неизвестны.
    last_seen_source: str = ""
    has_update: bool = False
    last_checked: Optional[datetime] = None
    # Ошибки автодокачки: счётчик подряд неудач (для backoff) и текст последней.
    fail_count: int = 0
    last_error: Optional[str] = None
    # Сколько глав было НА САЙТЕ, когда докачка упёрлась в платный хвост. Нужно,
    # чтобы отличать «книга неполная, потому что дальше платно» от «главу не
    # докачали». Первое — не ошибка: счётчик неудач не растёт, подписка живёт, и
    # книга не перекачивается каждый тик; новая БЕСПЛАТНАЯ глава всё равно будет
    # видна, потому что число на сайте станет больше запомненного
    # (serg/tasks#983). 0 = в платное не упирались.
    paid_tail_seen: int = 0


class SyncState(SQLModel, table=True):
    """Произвольные ключ-значение для состояния sync (этап 3)."""

    key: str = Field(primary_key=True)
    value: str = ""
    updated_at: datetime = Field(default_factory=utcnow)


class Blacklist(SQLModel, table=True):
    """Удалённые «крестиком» книги: не показывать в библиотеке и не докачивать."""

    id: Optional[int] = Field(default=None, primary_key=True)
    title_norm: str = Field(default="", index=True)
    author_norm: str = Field(default="", index=True)
    source_url: str = Field(default="", index=True)
    created_at: datetime = Field(default_factory=utcnow)


class Bookmark(SQLModel, table=True):
    """Закладка в книге. Много закладок на work_id (в отличие от Progress)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    work_id: int = Field(foreign_key="work.id", index=True)
    # Доля 0..1 — для сортировки списка и совместимости с ratio.
    ratio: float = 0.0
    # Точный локатор (Readium/foliate JSON-строка) для перехода.
    locator: str = ""
    # Необязательная подпись (например, первые слова абзаца).
    label: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class Highlight(SQLModel, table=True):
    """Выделение/цитата в книге. Много на work_id (как Bookmark)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    work_id: int = Field(foreign_key="work.id", index=True)
    # Доля 0..1 — сортировка списка и кросс-девайс якорь.
    ratio: float = 0.0
    # Точный локатор выделения (Readium/foliate JSON-строка) для перехода/рендера.
    locator: str = ""
    # Выделенный текст — для списка цитат и поиска.
    text: str = ""
    # Цвет подсветки (yellow|green|blue|pink…).
    color: str = "yellow"
    created_at: datetime = Field(default_factory=utcnow)


class Translation(SQLModel, table=True):
    """Кэш перевода одного абзаца.

    Ключ — по СОДЕРЖИМОМУ абзаца (sha256) плюс пара языков, а не по книге и
    позиции: возврат назад, перечитывание и повторяющиеся абзацы (в том числе в
    разных книгах) тогда обслуживаются без сети. Привязка к work_id, наоборот,
    заставляла бы переводить заново после каждой докачки новых глав, потому что
    позиции в книге сдвигаются.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    # "<src>:<dst>:<sha256 текста>"
    key: str = Field(index=True, unique=True)
    text: str = ""
    created_at: datetime = Field(default_factory=utcnow)


def utcnow_naive() -> datetime:
    """UTC без tzinfo — так Postgres отдаёт timestamp without time zone; в таблице
    заданий храним одну конвенцию, чтобы разность времён не падала."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class IngestJob(SQLModel, table=True):
    """Фоновое задание скачивания книги (POST /api/ingest {background: true}).

    Живёт в БД, а не в памяти процесса: рестарт сервиса не должен молча убивать
    идущее скачивание (serg/tasks#892). Жизненный цикл и правила возобновления —
    backend/app/ingestjob.py.
    """

    __tablename__ = "ingest_job"
    __table_args__ = (
        # Одно активное задание на один запрос: повторный POST (двойной тап,
        # перезагрузка страницы) не должен ставить то же скачивание второй раз.
        Index(
            "ux_ingest_job_active_query",
            "query",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True, max_length=64)
    kind: str = "ingest"
    query: str = ""
    status: str = Field(default="queued", index=True)  # queued | running | done | error
    # Запусков задания; плановая остановка сервиса попытку не сжигает (см. ingestjob).
    attempts: int = 0
    max_attempts: int = 3
    interruptions: int = 0
    interrupted_by: Optional[str] = None  # shutdown | crash
    not_before: Optional[datetime] = None
    # Какой процесс держит задание: юнит стабилен между рестартами, boot — нет.
    worker_unit: str = ""
    worker_boot: str = ""
    heartbeat_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow_naive)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    title: str = ""
    author: str = ""
    source_host: str = ""
    work_id: Optional[int] = None
    chapters: int = 0
    error: Optional[str] = None
    # Прогресс для панели скачиваний (serg/tasks#893); колонки заведены сразу,
    # чтобы не делать второй ALTER на общем Postgres.
    progress_done: int = 0
    progress_total: Optional[int] = None
    progress_unit: str = ""
    progress_stage: str = ""
