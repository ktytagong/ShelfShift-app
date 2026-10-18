"""The public BigQuery file path, end to end (DATA-13; 05.2-CONTEXT D-22). Marked live, so the default run deselects
it; the user runs it with their own credentials:

    .venv/bin/python -m pytest -q -m live tests/data/test_public_bq_live.py

It needs GOOGLE_CLOUD_PROJECT (set in the user's own shell, never written here) and Application Default Credentials,
and makes small paid BigQuery calls. Three series of tests/data/fixtures/frn_sample_synthetic.csv (a synthetic test
fixture in the FreshRetailNet-50K schema; not FreshRetailNet rows) go through the same path as the full run: the input
part loaded from a gz CSV file, AI.FORECAST into a destination table, the ordered paged read, the per-series checks
and, for any series that fails them, ARIMA_PLUS. Everything lands in a temporary dataset (the public set's dataset
name plus a random suffix), deleted afterwards.
"""
import os
import pathlib
import secrets
from datetime import date

import pytest

from data import bq
from data import forecast as F
from data import public as U
from data import public_config as P
from data import public_forecast as PF

FIX = pathlib.Path(__file__).resolve().parent / "fixtures"
CSV, SIDECAR = FIX / "frn_sample_synthetic.csv", FIX / "frn_sample_synthetic.json"
SERIES = 3


@pytest.mark.live
def test_three_fixture_series_through_the_public_file_path(tmp_path):
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        pytest.skip("set GOOGLE_CLOUD_PROJECT (and Application Default Credentials) to run the live BigQuery test")
    cfg = P.load_for(SIDECAR)
    fc = cfg["forecast"]
    source = (s for i, s in enumerate(U.series_iter(None, cfg, csv_path=CSV)) if i < SERIES)
    parts = tmp_path / "parts"
    meta = PF.prepare_input(source, cfg, parts)
    dataset = f"{fc['dataset']}_live_{secrets.token_hex(4)}"
    ids = bq.PublicIds(project, dataset)
    client = bq.make_client(project, fc["location"])
    try:
        ai, arima = bq.public_fetchers(project, fc["location"], dataset, fc["confidence_level"], fc["model"],
                                       date.fromisoformat(meta["day0"]), PF.forecast_horizon(meta),
                                       client_factory=lambda p, loc: client,
                                       max_bytes_billed=cfg["budget"]["max_bytes_billed"])
        summary = PF.stream_forecast(parts, PF.StreamProvider(F.AI_FORECAST, ai),
                                     PF.StreamProvider(F.ARIMA_PLUS, arima), fc["confidence_level"])
    finally:
        client.delete_dataset(ids.dataset_id, delete_contents=True, not_found_ok=True)

    # 3 series x (10 + 9 + 8 + 7 + 14) required days
    assert summary["points"] == SERIES * 48
    points = list(PF.read_csv_gz(parts / PF.POINTS_NAME))
    assert {p[7] for p in points} <= set(F.BIGQUERY)
    for p in points:
        assert 0 <= float(p[4]) <= float(p[5]) <= float(p[6])
