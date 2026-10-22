"""ci-column-comments: an unenforced declared domain -> 1, CHECK-backed -> 0, no migrations -> 2."""
from __future__ import annotations

from _sqlrepo import tracked_repo
from conftest import NL, run_tool

TABLE = NL.join((
    "CREATE TABLE s.scored (",
    "    id        bigint PRIMARY KEY,",
    "    mae_pct   double precision,   -- max adverse excursion",
    "    note      text                -- free-text note",
    ");", ""))
CHECKED = TABLE + "ALTER TABLE s.scored ADD CHECK (mae_pct >= 0);" + NL


def _run(repo, *extra):
    return run_tool("sql.column_comments", repo, "--root", str(repo), *extra)


def test_an_unenforced_domain_fires(new_repo) -> None:
    r = _run(tracked_repo(new_repo, {"migrations/010_scored.sql": TABLE}))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "010_scored.sql:mae_pct" in r.stdout
    assert "010_scored.sql:note" not in r.stdout, "a descriptive comment cried wolf"


def test_a_check_backed_domain_is_clean(new_repo) -> None:
    r = _run(tracked_repo(new_repo, {"migrations/010_scored.sql": CHECKED}))
    assert r.returncode == 0, r.stdout + r.stderr


def test_legacy_max_and_baseline_grandfather(new_repo) -> None:
    repo = tracked_repo(new_repo, {"migrations/010_scored.sql": TABLE})
    assert _run(repo, "--legacy-max", "10").returncode == 0
    assert _run(repo, "--write-baseline").returncode == 0
    assert _run(repo).returncode == 0


def test_no_migrations_is_2(new_repo) -> None:
    r = _run(tracked_repo(new_repo, {"app/x.py": "x = 1" + NL}))
    assert r.returncode == 2 and "could not look" in r.stderr


def test_selftest_passes(new_repo) -> None:
    assert run_tool("sql.column_comments", new_repo(), "--selftest").returncode == 0
