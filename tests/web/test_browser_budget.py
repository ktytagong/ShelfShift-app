"""Browser cases get independent sandbox budgets; visitors in one case still share the real cap."""
from datetime import timedelta

import pytest
import sqlalchemy as sa

from api import caps, config
from tests.web.conftest import clear_browser_sandbox_budget, db_tx, new_context

pytestmark = pytest.mark.web


def sandbox_count():
    with db_tx() as conn:
        now = conn.execute(sa.text("SELECT clock_timestamp()")).scalar_one()
        return conn.execute(sa.text("SELECT count FROM usage_counts WHERE scope=:scope AND kind='sandbox' AND window_start=:start"),
                            {"scope": caps.GLOBAL, "start": caps.window_start("day", now)}).scalar_one_or_none() or 0


def test_browser_case_starts_with_an_unspent_sandbox_budget(browser, web):
    assert sandbox_count() == 0
    context = new_context(browser, web)
    try:
        response = context.request.post('/api/session', data={'account': web.store, 'passcode': web.passcode})
        assert response.status == 200
        assert sandbox_count() == 1
    finally:
        context.close()


def test_visitors_in_one_case_share_the_real_sandbox_cap(browser, web):
    limit = config.GLOBAL_CAPS['sandbox']
    with db_tx() as conn:
        now = conn.execute(sa.text("SELECT clock_timestamp()")).scalar_one()
        conn.execute(sa.text("INSERT INTO usage_counts(scope,kind,window_start,count) VALUES(:scope,'sandbox',:start,:count) "
                             "ON CONFLICT(scope,kind,window_start) DO UPDATE SET count=excluded.count"),
                     {'scope': caps.GLOBAL, 'start': caps.window_start('day', now), 'count': limit - 1})
    contexts = [new_context(browser, web), new_context(browser, web)]
    try:
        first = contexts[0].request.post('/api/session', data={'account': web.store, 'passcode': web.passcode})
        second = contexts[1].request.post('/api/session', data={'account': web.store, 'passcode': web.passcode})
        assert first.status == 200
        assert second.status == 429
        assert second.json()['error'] == 'cap_reached'
        assert second.json()['kind'] == 'sandbox'
        assert not any(cookie['name'] == 'ss_session' for cookie in contexts[1].cookies())
        assert sandbox_count() == limit
    finally:
        for context in contexts:
            context.close()


def test_budget_boundary_preserves_other_scopes_kinds_and_history(web):
    with db_tx() as conn:
        savepoint = conn.begin_nested()
        try:
            now = conn.execute(sa.text("SELECT clock_timestamp()")).scalar_one()
            start = caps.window_start('day', now)
            target = (caps.GLOBAL, 'sandbox', start, 300)
            sentinels = [(caps.GLOBAL, 'sandbox', start - timedelta(days=1), 123),
                         ('fixture-other-scope', 'sandbox', start, 7),
                         (caps.GLOBAL, 'fixture-other-kind', start, 9)]
            for scope, kind, window, count in [target, *sentinels]:
                conn.execute(sa.text("INSERT INTO usage_counts(scope,kind,window_start,count) VALUES(:scope,:kind,:start,:count) "
                                     "ON CONFLICT(scope,kind,window_start) DO UPDATE SET count=excluded.count"),
                             dict(scope=scope, kind=kind, start=window, count=count))
            clear_browser_sandbox_budget(conn)
            for scope, kind, window, expected in sentinels:
                actual = conn.execute(sa.text("SELECT count FROM usage_counts WHERE scope=:scope AND kind=:kind AND window_start=:start"),
                                      dict(scope=scope, kind=kind, start=window)).scalar_one()
                assert actual == expected
            assert conn.execute(sa.text("SELECT count FROM usage_counts WHERE scope=:scope AND kind=:kind AND window_start=:start"),
                                dict(scope=target[0], kind=target[1], start=target[2])).scalar_one_or_none() is None
        finally:
            savepoint.rollback()
