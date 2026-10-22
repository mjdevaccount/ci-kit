"""ci-decimal-reads: an uncast NUMERIC projection -> 1, cast -> 0, no / empty ground truth -> 2."""
from __future__ import annotations

from _sqlrepo import tracked_repo
from conftest import NL, run_tool

COLS = "# schema.table.column" + NL + "quant.trades.realized_pnl" + NL
RAW = 'q("SELECT t.realized_pnl FROM quant.trades t")' + NL
CAST = 'q("SELECT t.realized_pnl::float8 AS p FROM quant.trades t")' + NL


def _run(repo, *extra):
    return run_tool("sql.decimal_reads", repo, "--root", str(repo), "--scan-dir", "app",
                    "--cols", "numeric_columns.txt", *extra)


def test_a_raw_numeric_read_fires(new_repo) -> None:
    r = _run(tracked_repo(new_repo, {"numeric_columns.txt": COLS, "app/repo.py": RAW}))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "realized_pnl" in r.stderr


def test_a_cast_read_is_clean(new_repo) -> None:
    r = _run(tracked_repo(new_repo, {"numeric_columns.txt": COLS, "app/repo.py": CAST}))
    assert r.returncode == 0, r.stdout + r.stderr


def test_baseline_grandfathers_and_list_ignores_it(new_repo) -> None:
    repo = tracked_repo(new_repo, {"numeric_columns.txt": COLS, "app/repo.py": RAW})
    assert _run(repo, "--write-baseline").returncode == 0
    assert _run(repo).returncode == 0
    assert "1 raw finding(s)" in _run(repo, "--list").stdout


def test_missing_ground_truth_is_2(new_repo) -> None:
    r = _run(tracked_repo(new_repo, {"app/repo.py": RAW}))
    assert r.returncode == 2 and "cannot read" in r.stderr, r.stdout + r.stderr


def test_empty_ground_truth_is_2(new_repo) -> None:
    r = _run(tracked_repo(new_repo, {"numeric_columns.txt": "# none" + NL, "app/repo.py": RAW}))
    assert r.returncode == 2 and "names no NUMERIC" in r.stderr, r.stdout + r.stderr


def test_selftest_passes(new_repo) -> None:
    assert run_tool("sql.decimal_reads", new_repo(), "--selftest").returncode == 0
