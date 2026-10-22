"""ci-pit-reads: a GROWN count -> 1, within baseline -> 0, unparseable / empty -> 2."""
from __future__ import annotations

from _sqlrepo import tracked_repo
from conftest import NL, run_tool, write

READ = 'q("SELECT * FROM t WHERE ts < now()")' + NL


def _run(repo, *extra):
    return run_tool("sql.pit_reads", repo, "--root", str(repo), "--scan-dir", "app", *extra)


def test_a_new_wall_clock_read_fires(new_repo) -> None:
    r = _run(tracked_repo(new_repo, {"app/repo.py": READ}))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "app/repo.py: 1 x SQL now()" in r.stderr


def test_baselined_then_grown_fires_again(new_repo) -> None:
    repo = tracked_repo(new_repo, {"app/repo.py": READ})
    assert _run(repo, "--write-baseline").returncode == 0
    assert _run(repo).returncode == 0
    write(repo / "app/repo.py", READ + READ)
    r = _run(repo)
    assert r.returncode == 1 and "baseline 1" in r.stderr, r.stdout + r.stderr


def test_waiver_and_python_clock_are_quiet(new_repo) -> None:
    src = 'q("SELECT now()")  # pit-ok' + NL + "x = dt.now()" + NL
    r = _run(tracked_repo(new_repo, {"app/repo.py": src}))
    assert r.returncode == 0, r.stdout + r.stderr


def test_unparseable_is_2(new_repo) -> None:
    r = _run(tracked_repo(new_repo, {"app/repo.py": 'q("SELECT FROM WHERE now(")' + NL}))
    assert r.returncode == 2, r.stdout + r.stderr


def test_an_empty_scan_dir_is_2_not_clean(new_repo) -> None:
    r = _run(tracked_repo(new_repo, {"app/README.md": "no python" + NL}))
    assert r.returncode == 2, r.stdout + r.stderr


def test_selftest_passes(new_repo) -> None:
    assert run_tool("sql.pit_reads", new_repo(), "--selftest").returncode == 0
