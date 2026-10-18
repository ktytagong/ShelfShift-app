"""Fail-closed release scans (11-CONTEXT D-12 item 1).
The public export's full-history scan remains the user's H-10 step in 11-HANDOFF.md.
Private entries never appear in failures: only entry indexes and locations.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess

import pytest
from tests.data.test_honesty import denylist

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_gate_mode_fails_closed(monkeypatch):
    monkeypatch.delenv('SHELFSHIFT_DENYLIST', raising=False)
    monkeypatch.setenv('SHELFSHIFT_RELEASE_GATE', '1')
    with pytest.raises(pytest.fail.Exception, match='SHELFSHIFT_DENYLIST.*SHELFSHIFT_RELEASE_GATE'):
        try:
            denylist()
        except pytest.skip.Exception:
            raise AssertionError('Release gate skipped the required denylist') from None
    monkeypatch.delenv('SHELFSHIFT_RELEASE_GATE')
    with pytest.raises(pytest.skip.Exception):
        denylist()


def scan_paths(paths, names):
    found = []
    for path in paths:
        payload = path.read_bytes()
        if b'\x00' in payload: continue
        text = payload.decode('utf-8', errors='replace')
        for index, name in enumerate(names, 1):
            pattern = re.compile(r'(?<!\w)' + re.escape(name) + r'(?!\w)', re.I)
            for line, value in enumerate(text.splitlines(), 1):
                if pattern.search(value):
                    location = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path.name
                    found.append(f'denylist entry {index} at {location}:{line}')
    return found


def test_scan_rule_is_escaped_bounded_and_never_reports_a_name(tmp_path):
    entry = 'private' + '.fixture'
    text = tmp_path / 'probe.txt'
    text.write_text('PRIVATE.FIXTURE\nxprivate.fixturex\nprivateXfixture\n')
    assert scan_paths([text], [entry]) == ['denylist entry 1 at probe.txt:1']
    text.write_bytes(b'\x00PRIVATE.FIXTURE')
    assert scan_paths([text], [entry]) == []


def test_tracked_files_name_no_retailer():
    names = denylist()
    listed = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, check=True, capture_output=True).stdout
    paths = [ROOT / os.fsdecode(path) for path in listed.split(b'\x00') if path]
    found = scan_paths([p for p in paths if p.is_file()], names)
    if found: pytest.fail('\n'.join(found), pytrace=False)


def test_built_bundle_names_no_retailer():
    names = denylist()
    assets = ROOT / 'web/dist/assets'
    if not assets.is_dir():
        if os.environ.get('SHELFSHIFT_RELEASE_GATE', '').strip() == '1':
            pytest.fail('build web/dist first (make web-build or make test-web)', pytrace=False)
        pytest.skip('build web/dist first (make web-build or make test-web)')
    paths = [p for p in assets.rglob('*') if p.is_file()]
    if not paths: pytest.fail('build web/dist first (make web-build or make test-web)', pytrace=False)
    found = scan_paths(paths, names)
    if found: pytest.fail('\n'.join(found), pytrace=False)
