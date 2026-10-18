"""Confirmation order follows the serialized write, even when transactions start in reverse order."""
from datetime import timedelta

from api import db, facts, signals
from tests.api.conftest import seat
from tests.api.test_evidence import add_facts, answer, ask, counted, phantom, story, table


def test_confirmation_clock_follows_run_lock_order(run):
    case = story(run)
    check = phantom(run, case["case_id"])
    add_facts(run, case["case_id"], [{**check, "fact_id": "TEST:clock-second"}])
    first = ask(run, case["case_id"])
    second = ask(run, case["case_id"], "TEST:clock-second")
    with db.tx() as conn:
        observed = db.one(conn, "SELECT clock_timestamp() AS at")["at"] - timedelta(minutes=10)
    older = answer(run, first, counted(first, 1), observed=observed.isoformat())
    newer = answer(run, second, counted(second, 3), observed=(observed + timedelta(seconds=1)).isoformat())
    actor = seat(first["to_branch"])

    # SQL starts A before B. B acquires the run lock and commits its confirmation
    # first; A acquires that lock afterward, exactly the order that can occur
    # when workers start a transaction before entering the serialized pipeline.
    with db.tx() as transaction_a:
        start_a = db.one(transaction_a, "SELECT now() AS at")["at"]
        with db.tx() as transaction_b:
            start_b = db.one(transaction_b, "SELECT now() AS at")["at"]
            early = signals.confirm(transaction_b, db.lock_run(transaction_b, run["run_id"], mode="UPDATE"),
                                    actor, older["signal_id"], older["revision"], True)
        late = signals.confirm(transaction_a, db.lock_run(transaction_a, run["run_id"], mode="UPDATE"),
                               actor, newer["signal_id"], newer["revision"], True)

    stored = {row["fact_id"]: row for row in table(run["run_id"], "reported_facts")}
    assert start_a < start_b, "the transaction-start order is controlled before either lock acquisition"
    assert stored[early["fact_id"]]["confirmed_at"] < stored[late["fact_id"]]["confirmed_at"], (
        "confirmation timestamps must follow serialized fact insertion, not transaction start",
        start_a, start_b, stored[early["fact_id"]]["confirmed_at"], stored[late["fact_id"]]["confirmed_at"])
    with db.tx() as conn:
        assert all(facts.in_force(conn, run["run_id"], lot, "stock_count")["fact_id"] == late["fact_id"]
                   for lot in first["lot_ids"])
