"""ci-phantom-columns: a column the table lacks -> 1, real columns -> 0, no / empty schema -> 2."""
from __future__ import annotations

import json

from _sqlrepo import tracked_repo
from conftest import NL, run_tool

SCHEMA = json.dumps({"quant.operator_trades": ["trade_id", "entry_px", "setup"]})
BAD = 'q("SELECT trade_id, risk_usd::float8 AS risk_usd FROM quant.operator_trades")' + NL
GOOD = 'q("SELECT trade_id, entry_px::float8 AS entry_px FROM quant.operator_trades")' + NL


def _run(repo, schema="schema.json"):
    return run_tool("sql.phantom_columns", repo, "--root", str(repo), "--scan-dir", "app",
                    "--schema", str(repo / schema))


def test_a_phantom_column_fires(new_repo) -> None:
    r = _run(tracked_repo(new_repo, {"schema.json": SCHEMA, "app/repo.py": BAD}))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "no such column: risk_usd" in r.stdout


def test_real_columns_are_clean(new_repo) -> None:
    r = _run(tracked_repo(new_repo, {"schema.json": SCHEMA, "app/repo.py": GOOD}))
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_tests_dir_is_not_scanned(new_repo) -> None:
    files = {"schema.json": SCHEMA, "app/repo.py": GOOD, "app/tests/test_x.py": BAD}
    assert _run(tracked_repo(new_repo, files)).returncode == 0


def test_missing_schema_is_2(new_repo) -> None:
    r = _run(tracked_repo(new_repo, {"app/repo.py": BAD}))
    assert r.returncode == 2 and "could not read the schema" in r.stderr


def test_empty_schema_is_2(new_repo) -> None:
    r = _run(tracked_repo(new_repo, {"schema.json": "{}", "app/repo.py": BAD}))
    assert r.returncode == 2 and "names no tables" in r.stderr


def test_selftest_passes(new_repo) -> None:
    assert run_tool("sql.phantom_columns", new_repo(), "--selftest").returncode == 0
