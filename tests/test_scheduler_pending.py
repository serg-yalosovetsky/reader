def test_feed_skips_empty_download_pass(session, monkeypatch):
    from backend.app import scheduler
    from backend.accounts import feeds, check_job
    from backend.app.db.models import Monitored

    monkeypatch.setattr(scheduler, 'engine', session.get_bind())
    monkeypatch.setattr(feeds, 'pull_all', lambda *a, **kw: {})
    calls = []
    monkeypatch.setattr(check_job, 'run_blocking', lambda *a, **kw: calls.append(kw) or {'status': 'done'})
    scheduler._ficbook_feed_job()
    assert calls == []
    session.add(Monitored(source_url='https://ficbook.net/readfic/test', has_update=True))
    session.commit()
    scheduler._ficbook_feed_job()
    assert calls == [{'only_pending': True, 'pull_feeds': False}]
