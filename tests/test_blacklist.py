"""Unit tests for backend.app.blacklist + services._norm, against an in-memory DB."""

from backend.app import blacklist
from backend.app.services import _norm


def test_norm_strips_quotes_case_and_whitespace():
    assert _norm('  "Foo"   Bar ') == "foo bar"
    assert _norm("«Тест»Имя") == "тестимя"
    assert _norm("") == ""


def test_add_check_and_unblock(session):
    blacklist.add_entry(
        session, title="Foo", author="Bar", urls=["http://x/1", "http://x/2"]
    )
    # blocked by url and by normalized title/author
    assert blacklist.is_blacklisted(session, source_url="http://x/1")
    assert blacklist.is_blacklisted(session, source_url="http://x/2")
    assert blacklist.is_blacklisted(session, title="foo", author="bar")
    # not blocked for unknown url / title
    assert not blacklist.is_blacklisted(session, source_url="http://nope")
    assert not blacklist.is_blacklisted(session, title="unknown", author="nobody")
    # unblock one url; the title/author entry survives
    removed = blacklist.unblock(session, source_url="http://x/1")
    assert removed == 1
    assert not blacklist.is_blacklisted(session, source_url="http://x/1")
    assert blacklist.is_blacklisted(session, title="Foo", author="Bar")


def test_add_entry_is_deduped(session):
    blacklist.add_entry(session, title="A", author="B", urls=["http://u"])
    blacklist.add_entry(session, title="A", author="B", urls=["http://u"])
    from sqlmodel import select

    from backend.app.db.models import Blacklist

    rows = session.exec(
        select(Blacklist).where(Blacklist.source_url == "http://u")
    ).all()
    assert len(rows) == 1
