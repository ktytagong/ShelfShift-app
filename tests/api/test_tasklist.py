"""Tasks as one list on the server (07-CONTEXT D-36 item 3; 06.1 D-17 / 07-SCREENS-MAP G5: the client never sorts or
filters a list page): GET /api/tasks keeps its keys and adds `tiles` (the six open counts) and `list` (one page of
task items for `view` open or closed and an optional `tile`, in the server's order: urgent and blocked first).

Open order: requests with a part still to answer by their nearest deadline (an overdue one keeps its place, it is
the nearest), then answers to confirm (oldest answer first), lot holds the store owns (oldest first), dispatches to
record (blocked ones first, then the transfer list's order), receipts to record, advisory tasks. A combined request
is one item with its two parts, never two items. Closed: confirmed, Can't check, superseded, held, released and done
items, newest first. Expectations are read from the services' own views, never pasted."""
from __future__ import annotations

from api import db, signals, tasklist
from tests.api import test_http
from tests.api.conftest import as_seat, fresh, key, seat
from tests.api.test_evidence import add_facts, answer, ask, confirm, counted, lot_row, past_due, phantom, story
from tests.api.test_holds import other_branch, place, release
from tests.api.test_planning_views import prepare_story, session
from tests.api.test_requests import ask_pair, cant_check, pair_fixture
from tests.api.test_signals import confirmed, filed, phantom_lot, report_action

TILES = {"to_answer", "to_confirm", "lot_holds", "advisory", "dispatches", "receipts"}
KEYS = {"requests", "holds", "advisory", "counts", "server_now", "tiles", "list"}
ITEM_KEYS = {"key", "kind", "tile", "open", "urgent", "blocked", "blocked_reasons", "owner_branch", "due_at", "at",
             "sku", "parts", "confirmations", "exposure", "hold", "advisory", "transfer"}


def listing(run, actor, view="open", tile=None) -> dict:
    with db.tx() as conn:
        return tasklist.view(conn, fresh(run["run_id"]), actor, view=view, tile=tile)


def keys(got: dict) -> list[str]:
    return [i["key"] for i in got["list"]["items"]]


def test_the_list_orders_filters_and_counts(run):
    """Two requests to answer come by deadline (the overdue one first), an answer to confirm after them, then the
    store's lot hold; a tile filters the list; Can't check and a release move items to Closed, newest first; the
    tiles count open items (a check per part, as `counts` does) and agree with `counts` and tasks_waiting."""
    case = story(run)
    cid = case["case_id"]
    fact = phantom(run, cid)
    add_facts(run, cid, [{**fact, "fact_id": "TEST:late", "kind": "STALE_COUNT"},
                         {**fact, "fact_id": "TEST:third", "kind": "STALE_COUNT"}])
    to = seat(fact["branch"])
    empty = listing(run, to)
    assert set(empty) == KEYS
    assert set(empty["tiles"]) == TILES and empty["list"]["items"] == []
    assert (empty["list"]["view"], empty["list"]["tile"], empty["list"]["total"]) == ("open", None, 0)

    first = ask(run, cid)
    second = ask(run, cid, "TEST:late")
    past_due(run, second)  # overdue: the nearest deadline, so it leads
    got = listing(run, to)
    assert keys(got) == [f"request:{second['request_id']}", f"request:{first['request_id']}"]
    lead = got["list"]["items"][0]
    assert set(lead) == ITEM_KEYS
    assert (lead["kind"], lead["tile"], lead["open"], lead["urgent"], lead["blocked"]) == (
        "request", "to_answer", True, True, False)
    assert lead["owner_branch"] == fact["branch"] and lead["due_at"] == lead["parts"][0]["due_at"]
    assert [p["request_id"] for p in lead["parts"]] == [second["request_id"]]
    assert lead["exposure"] == {second["request_id"]: fact["regret"]}  # the ask's stored decision regret
    assert got["list"]["items"][1]["urgent"] is False

    answer(run, first, counted(first, 0.0))  # complete: now an answer to confirm
    lot = lot_row(run, fact["lot_ids"][0])  # the counted lot: the addressed store owns it
    assert lot["branch"] == fact["branch"]
    hold = place(run, to, lot["lot_id"])
    got = listing(run, to)
    assert keys(got) == [f"request:{second['request_id']}", f"request:{first['request_id']}", f"hold:{hold['hold_id']}"]
    assert [i["tile"] for i in got["list"]["items"]] == ["to_answer", "to_confirm", "lot_holds"]
    assert got["list"]["items"][2]["hold"]["hold_id"] == hold["hold_id"]
    with db.tx() as conn:
        waits = signals.waiting(conn, fresh(run["run_id"]), to)
    tiles = got["tiles"]
    assert {k: tiles[k] for k in ("to_answer", "to_confirm", "lot_holds", "advisory")} == got["counts"]
    assert sum(got["counts"].values()) == waits
    assert keys(listing(run, to, tile="to_confirm")) == [f"request:{first['request_id']}"]
    assert keys(listing(run, to, tile="lot_holds")) == [f"hold:{hold['hold_id']}"]
    assert listing(run, to, view="closed")["list"]["items"] == []

    cant_check(run, second)
    release(run, to, hold["hold_id"], "Inspected, all sound")
    open_now = listing(run, to)
    assert keys(open_now) == [f"request:{first['request_id']}"]
    assert open_now["tiles"]["to_answer"] == 0 and open_now["tiles"]["lot_holds"] == 0
    closed = listing(run, to, view="closed")
    assert keys(closed) == [f"hold:{hold['hold_id']}", f"request:{second['request_id']}"]  # newest first
    assert all(not i["open"] for i in closed["list"]["items"])
    assert closed["list"]["items"][0]["hold"]["resolved_label"].startswith(f"Hold released by {fact['branch']} manager")
    assert keys(listing(run, to, view="closed", tile="lot_holds")) == [f"hold:{hold['hold_id']}"]

    seen = next(r for r in listing(run, to)["list"]["items"][0]["parts"])
    assert seen["request_id"] == first["request_id"] and lead["sku"] == case["sku"]
    confirm(run, to, seen["answer_signal_id"], seen["answer"]["revision"])
    closed = listing(run, to, view="closed")
    assert keys(closed)[0] == f"request:{first['request_id']}"  # the newest close leads
    done = closed["list"]["items"][0]
    assert done["tile"] == "to_confirm" and done["confirmations"][first["request_id"]]["labels"][0].startswith(
        f"Count confirmed by {fact['branch']} manager · ")

    for other in (seat(None), seat(other_branch(run, fact["branch"]))):  # HQ, and a store with nothing
        got = listing(run, other)
        assert got["list"]["items"] == [] and set(got["tiles"].values()) == {0}


def test_a_combined_request_is_one_item(run):
    """Edge UI-05 adjacency: the two parts of a combined request are one item with both parts (count first); the
    tiles count each check; confirming nothing yet, the item is to answer until both parts are answered."""
    case, count, expiry = pair_fixture(run)
    q = ask_pair(run, case["case_id"], [count["fact_id"], expiry["fact_id"]])
    to = seat(q["to_branch"])
    got = listing(run, to)
    [item] = got["list"]["items"]
    assert item["key"] == f"request:{q['request_id']}"
    assert [p["part"] for p in item["parts"]] == ["count", "expiry"]
    assert got["tiles"]["to_answer"] == 2  # two checks to answer, one task
    answer(run, q, counted(q, 0.0))
    [item] = listing(run, to)["list"]["items"]
    assert item["tile"] == "to_answer"  # the expiry part still waits for its answer


def test_tasks_over_http(client):
    """The route reads `view`, `tile`, `page` and `page_size`; a value it does not know is 400 bad_request; the
    session's nav_counts.tasks equals the six tiles' sum; HQ gets no items."""
    as_seat(client, None)
    hq = client.get("/api/tasks").json()
    assert hq["list"]["items"] == [] and set(hq["tiles"].values()) == {0}
    assert client.get("/api/session").json()["nav_counts"]["tasks"] == 0
    for bad in ({"view": "later"}, {"tile": "everything"}):
        r = client.get("/api/tasks", params=bad)
        assert (r.status_code, r.json()["error"]) == (400, "bad_request"), r.json()


def test_dispatches_and_their_blocks(template):
    """The source store's prepared transfer is a dispatch item; a lot hold on its lot makes it blocked with the hold's
    reason (the dispatch check's lot_hold block) and leads the dispatches, after the hold itself; the destination has
    no receipt to record until it is dispatched; nav_counts.tasks is the tiles' sum for both stores."""
    c, run_id = session()
    action = prepare_story(run_id)
    src, dest = action["origin"], action["dest"]
    as_seat(c, src)
    got = c.get("/api/tasks", params={"tile": "dispatches"}).json()
    [item] = got["list"]["items"]
    assert (item["key"], item["kind"], item["tile"], item["open"], item["blocked"]) == (
        f"dispatch:{action['action_id']}", "dispatch", "dispatches", True, False)
    assert item["transfer"]["action_id"] == action["action_id"] and item["owner_branch"] == src
    assert got["tiles"]["dispatches"] == 1
    assert c.get("/api/session").json()["nav_counts"]["tasks"] == sum(got["tiles"].values())

    r = c.post(f"/api/lots/{action['lot_id']}/holds", json={"reason": "Crushed crates"}, headers=key())
    assert r.status_code == 200, r.json()
    hold = r.json()
    got = c.get("/api/tasks").json()
    order = [i["key"] for i in got["list"]["items"]]
    assert order.index(f"hold:{hold['hold_id']}") < order.index(f"dispatch:{action['action_id']}")
    [blocked] = [i for i in got["list"]["items"] if i["kind"] == "dispatch"]
    assert blocked["blocked"] is True
    assert [(b["kind"], b["id"]) for b in blocked["blocked_reasons"]] == [("lot_hold", hold["hold_id"])]
    assert c.get("/api/session").json()["nav_counts"]["tasks"] == sum(got["tiles"].values())

    as_seat(c, dest)
    there = c.get("/api/tasks").json()
    assert there["tiles"]["receipts"] == 0
    assert c.get("/api/session").json()["nav_counts"]["tasks"] == sum(there["tiles"].values())


def test_advisory_tasks_come_last_and_close_when_done(client):
    """A confirmed display issue is an advisory item after the store's lot hold; Mark done moves it to Closed with
    its done label, and the hold stays open (D-13)."""
    card = test_http.story(client)
    lot_id, branch = phantom_lot(client, card)
    as_seat(client, branch)
    r = client.post(f"/api/lots/{lot_id}/holds", json={"reason": "Leaking crate"}, headers=key())
    assert r.status_code == 200, r.json()
    hold_id = r.json()["hold_id"]
    rep = filed(client, "CONDITION", {"lot_id": lot_id, "condition": "DISPLAY"}, text="Shelf tag missing")
    confirmed(client, rep["signal_id"])
    order = [i["key"] for i in client.get("/api/tasks").json()["list"]["items"]]
    assert order.index(f"hold:{hold_id}") < order.index(f"advisory:{rep['signal_id']}") == len(order) - 1
    assert report_action(client, rep["signal_id"], "done").status_code == 200
    got = client.get("/api/tasks").json()
    assert f"advisory:{rep['signal_id']}" not in [i["key"] for i in got["list"]["items"]]
    assert got["tiles"]["advisory"] == 0 and got["tiles"]["lot_holds"] == 1
    closed = client.get("/api/tasks", params={"view": "closed", "tile": "advisory"}).json()["list"]["items"]
    assert [i["key"] for i in closed] == [f"advisory:{rep['signal_id']}"]
    assert closed[0]["advisory"]["task"]["done_label"].startswith(f"Done by {branch} manager")
    assert closed[0]["sku"] == card["sku"]  # the product the task is about (the row's title)
