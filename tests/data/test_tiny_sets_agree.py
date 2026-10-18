"""The format contract between build/web and build/api (05.2-17; 05.2-01 execution rules: the artifact manifest is
the contract between the branches, and build/api never imports a module that exists only on build/web).

build/api's tests load tests/api/fixtures/public_tiny; build/web's pipeline writes tests/data/fixtures/public_tiny.
After build/api is merged into build/web the two folders must be the same bytes, and the pipeline must still write
them: a fresh `tools/frn.py build` of the CSV fixture (a synthetic test fixture in the FreshRetailNet-50K schema, not
FreshRetailNet rows) equals both. A pipeline change that alters the format then fails here, on build/web, before the
API tests ever read a stale copy.
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "data" / "fixtures"
CSV, SIDECAR = FIX / "frn_sample_synthetic.csv", FIX / "frn_sample_synthetic.json"
DATA_TINY = FIX / "public_tiny"
API_TINY = ROOT / "tests" / "api" / "fixtures" / "public_tiny"


def files_of(root: pathlib.Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_the_api_fixture_is_the_pipeline_output(tmp_path):
    data, api = files_of(DATA_TINY), files_of(API_TINY)
    assert data, "tests/data/fixtures/public_tiny is empty"
    assert "manifest.json" in data
    assert sorted(api) == sorted(data), "the two tiny sets hold different files"
    differ = [name for name in data if api[name] != data[name]]
    assert not differ, f"files that differ between the branches' tiny sets: {differ}"

    out = tmp_path / "tiny"
    done = subprocess.run(
        [sys.executable, "-I", str(ROOT / "tools" / "frn.py"), "build", "--csv", str(CSV), "--fixture", str(SIDECAR),
         "--out", str(out), "--forecast", "local", "--reports", str(tmp_path / "reports")],
        capture_output=True, text=True, cwd=ROOT, timeout=300)
    assert done.returncode == 0, done.stderr
    fresh = files_of(out)
    assert fresh == data, sorted(n for n in set(fresh) | set(data) if fresh.get(n) != data.get(n))
    assert fresh == api
