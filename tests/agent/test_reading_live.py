"""USER-RUN ONLY in 10-20; collection is the only automated operation here.

Load credentials locally using 09-23's deploy-owner environment export:
  set +x
  P10_KEYS="$(cd ../ShelfShift-deploy && .venv/bin/python -m deploy.envfile export)"
  eval "$P10_KEYS"; unset P10_KEYS
Then in the reading checkout (the command is intentionally user-run only):
  GCP=on SHELFSHIFT_LIVE_TESTS=user-run SHELFSHIFT_LIVE_RUN_ID=<sandbox-id> \
    .venv/bin/python -m pytest -q -s -m live tests/agent/test_reading_live.py
Use the already approved model/location and the dedicated local reading test DB.
Never record credentials, cloud identifiers or report contents in test output.
These contracts make no claim that real model quality has been observed.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import time
import uuid

import pytest

pytestmark = pytest.mark.live


@pytest.mark.parametrize("sample", ["chiller-capacity", "quote-pdf", "blurry-photo", "injection", "photo-only", "photo-and-count"])
def test_reading_live(sample):
    # Guard before importing DB/SDK modules. No model exists at collection time.
    if os.environ.get("GCP") != "on" or os.environ.get("SHELFSHIFT_LIVE_TESTS") != "user-run":
        raise pytest.UsageError("Live readings require explicit user-run consent and GCP=on")
    rid = os.environ.get("SHELFSHIFT_LIVE_RUN_ID")
    if not rid:
        raise pytest.UsageError("Set SHELFSHIFT_LIVE_RUN_ID to an authorized local sandbox")
    from api import db, refdata
    from agent import reading, readings_job
    from api import config

    with db.tx() as conn:
        run = db.one(conn, "SELECT * FROM demo_runs WHERE run_id=:r", r=rid)
        assert run is not None
        ref = refdata.get(run["dataset_version"])
        store = next(iter(ref.branches))
        actor = {"role": "hq", "branch": None} if sample == "quote-pdf" else {"role": "manager", "branch": store}
        ctx = readings_job.context(conn, run, actor)
        if sample == "quote-pdf":
            from api import preload
            guide = preload.render_guide(conn, run)
            quote, values = guide["quote"], guide["values"]
            trip = ref.run_on(quote["run_code"], run["day"])
            # Same ordinary guide PDF bytes as api.preload's guide command.
            # The separately approved ambiguous s07 attachment is not changed.
            quote_data = preload.quote_pdf(
                quote["run_code"], quote["amount"], quote["currency"],
                origin=trip.origin, dest=trip.dest,
                trip_date=f"{values['weekday']} {values['date']}")
        if sample in ("photo-only", "photo-and-count"):
            from api import overlay
            lot = overlay.lot(conn, run, "shelfshift", ctx.lot_ids[0])
            ctx = replace(ctx, purpose="answer", skus=(lot["sku"],), lot_ids=(lot["lot_id"],),
                          case_prefill={"store": store, "sku": lot["sku"], "lot_ids": [lot["lot_id"]]})
    if sample == "quote-pdf":
        text = next(item["text"] for item in guide["samples"] if item["id"] == sample)
        data = quote_data
    else:
        samples = json.loads(Path("data/demo_signals/samples.json").read_text())
        item = next(item for item in samples if item["id"] == sample)
        text = item["text"].format(dest_store=store, store=store)
        data = Path("data/demo_signals", item["file"]).read_bytes() if item["file"] else None
    parts = reading.build_parts(text, data, None, ctx, uuid.uuid4().hex)

    async def call():
        client = readings_job.genai_client()
        try:
            return await reading.read(parts, client=client, model_id=ctx.model_id, settings=config.READING)
        finally:
            await client.aio.aclose()

    start = time.monotonic()
    result = asyncio.run(call())
    print(f"{sample}: latency_ms={(time.monotonic() - start) * 1000:.0f}")
    assert result.reason is None and result.reading is not None
    validated = (reading.validate_answer if ctx.purpose == "answer" else reading.validate)(result.reading, ctx, text)
    if sample in ("photo-only", "photo-and-count"):
        values = reading.answer_values(validated["rows"], list(ctx.lot_ids))
        if sample == "photo-only":
            assert validated["observations"] and values["pending_counts"] is None
        else:
            assert values["pending_counts"] == {ctx.lot_ids[0]: {"shelf_units": 0, "backroom_units": 0}}
        return
    if sample == "chiller-capacity":
        assert validated["kind"] == "CAPACITY" and result.reading.zone == "chilled"
        assert ctx.horizon[0] <= ctx.calendar[result.reading.valid_from] <= ctx.calendar[result.reading.valid_to] <= ctx.horizon[1]
        free = next(row for row in validated["rows"] if row["field"] == "free_totes")
        assert free["state"] == "calculated" and free["value"] is not None
    elif sample == "quote-pdf":
        assert validated["kind"] == "TRANSPORT"
        rows = {row["field"]: row for row in validated["rows"]}
        assert rows["run_code"]["value"] == quote["run_code"]
        assert rows["what"]["value"] == "QUOTE"
        assert rows["amount"]["value"] == pytest.approx(quote["amount"])
        # These terms are printed in the ordinary guide PDF; never infer them
        # for the distinct ambiguous eval attachment.
        assert rows["pricing_basis"]["value"] == "PER_TRIP"
        assert rows["includes_handling"]["value"] == "YES"
        assert all(rows[field]["source"] == "file"
                   for field in ("amount", "pricing_basis", "includes_handling"))
    elif sample == "blurry-photo":
        assert all(row["value"] is None for row in validated["rows"] if row["field"] != "kind")
    else:
        assert validated["note"] is True and validated["rows"] == []
