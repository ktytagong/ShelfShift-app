"""Server sentences a product-mode request can put on screen say neither "demo", "today" nor "sandbox" (06.1 D-58,
D-61, D-62 and the copy rule; quick 261017-wzs finding 9).

The web shows a server message word for word (web/src/copy.ts), so each sentence below is read where the server
writes it: the db_busy answer through the real app, the dispatch recheck's resolution, the daily cap for a new
workspace and for AI requests, and Reset's locked and preparing answers. Sentences only demo mode can reach (Next day,
the retired workspace) are listed in the quick task's SUMMARY instead.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from api import caps, config, db, execution, sandbox, stock
from api.errors import ApiError

BANNED = re.compile(r"\b(demo|today|sandbox)\b", re.IGNORECASE)
WINDOW = datetime(2031, 1, 27, 3, 30, tzinfo=timezone.utc)  # a day window no other test uses (test_caps far_window)


def clean(sentence: str) -> None:
    assert BANNED.search(sentence) is None, f"{sentence!r} says {BANNED.search(sentence).group(0)!r}"


def test_the_db_busy_answer(client, monkeypatch):
    def exhausted():
        raise sa.exc.TimeoutError("QueuePool limit reached")
    monkeypatch.setattr(db, "tx", exhausted)
    r = client.get("/api/cases")
    assert (r.status_code, r.json()["error"]) == (503, "db_busy")
    clean(r.json()["message"])
    assert stock.BUSY == r.json()["message"]  # the series and analytics answers say the same words


def test_the_dispatch_recheck_resolution():
    clean(execution.RECHECK_RESOLUTION)


@pytest.mark.parametrize("kind", ["sandbox", "ai"])
def test_the_daily_cap_message(kind, monkeypatch):
    monkeypatch.setitem(config.GLOBAL_CAPS, kind, 0)
    with pytest.raises(ApiError) as e:
        db.in_tx(lambda conn: caps.spend(conn, None, kind, now=WINDOW))
    assert (e.value.status, e.value.code) == (429, "cap_reached")
    clean(e.value.message)


def test_resets_locked_and_preparing_answers(run, monkeypatch):
    with pytest.raises(ApiError) as e:
        db.in_tx(sandbox.reset, f"wzs-none-{uuid.uuid4().hex}")
    assert (e.value.status, e.value.code) == (401, "locked")
    clean(e.value.message)
    sid = f"wzs-prep-{uuid.uuid4().hex}"
    with db.tx() as conn:
        db.run(conn, "INSERT INTO sessions (session_id, role, branch, run_id) VALUES (:s, 'manager', NULL, :r)",
               s=sid, r=run["run_id"])
    monkeypatch.setattr(sandbox, "active_template", lambda conn: None)
    with pytest.raises(ApiError) as e:
        db.in_tx(sandbox.reset, sid)
    assert (e.value.status, e.value.code) == (503, "preparing")
    clean(e.value.message)
