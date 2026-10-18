"""The real BigQuery forecast build, end to end (DATA-03). Marked live, so the default run deselects it."""
import json
import math
import os
import shutil

import pytest

from data import bq
from data import forecast as F


@pytest.mark.live
def test_bigquery_build_passes_the_candidate_checks(tmp_path):
    """Run by the user only: needs GOOGLE_CLOUD_PROJECT set to the demo project (the PROJECT
    key of deploy/.env.local) and Application Default Credentials;
    makes paid BigQuery calls.

    seed.json and truth.json are copied into tmp_path, so the committed data/reference/forecast.json is never read
    as an input or written. AI.FORECAST runs for every series and ARIMA_PLUS only for the series it got wrong, as
    `python -m data.forecast --provider ai_forecast` does; the candidate must pass check_candidate and score."""
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        pytest.skip("set GOOGLE_CLOUD_PROJECT (and Application Default Credentials) to run the live BigQuery test")
    for name in ("seed.json", "truth.json"):
        shutil.copy(F.REF / name, tmp_path / name)
    seed = json.loads((tmp_path / "seed.json").read_text(encoding="utf-8"))
    truth = json.loads((tmp_path / "truth.json").read_text(encoding="utf-8"))
    fc = seed["forecast"]

    ai, arima = bq.providers(project, fc["location"], fc["dataset"], fc["confidence_level"], fc["model"])
    forecast = F.build(seed, ai, arima)
    forecast["quality"] = quality = F.score(truth, forecast, seed)
    F.check_candidate(forecast, seed)

    assert {p["provider"] for p in forecast["points"]} <= set(F.BIGQUERY)
    assert isinstance(quality["wape"], (int, float)) and math.isfinite(quality["wape"])
