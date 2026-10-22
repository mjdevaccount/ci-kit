"""ci-sql-casts: bad -> 1, good -> 0, blind -> 2, driven as a caller runs it."""
from __future__ import annotations

from _sqlrepo import tracked_repo
from conftest import NL, run_tool

GOOD = 'q("SELECT * FROM t WHERE ts > CAST(CAST(:since AS text) AS timestamptz)")' + NL
RULE_A = 'q("SELECT * FROM t WHERE ts > :since::timestamptz")' + NL
RULE_C = 'q("SELECT * FROM t WHERE ts > now() - :age")' + NL
RULE_B = 'q("SELECT * FROM t WHERE ts > CAST(:since AS timestamptz)")' + NL


def _run(repo, *extra):
    return run_tool("sql.casts", repo, "--root", str(repo), "--scan-dir", "app", *extra)


def test_rule_a_and_c_fire(new_repo) -> None:
    r = _run(tracked_repo(new_repo, {"app/a.py": RULE_A, "app/c.py": RULE_C}))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "app/a.py" in r.stderr and "app/c.py" in r.stderr


def test_a_new_rule_b_site_fires_until_baselined(new_repo) -> None:
    repo = tracked_repo(new_repo, {"app/b.py": RULE_B})
    assert _run(repo).returncode == 1
    assert _run(repo, "--write-baseline").returncode == 0
    r = _run(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 Rule-B site(s)" in r.stdout


def test_clean_tree_is_0(new_repo) -> None:
    r = _run(tracked_repo(new_repo, {"app/ok.py": GOOD}))
    assert r.returncode == 0, r.stdout + r.stderr


def test_unparseable_sql_is_2(new_repo) -> None:
    r = _run(tracked_repo(new_repo, {"app/x.py": 'q("SELECT FROM WHERE CAST(:x AS")' + NL}))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "DID NOT FINISH LOOKING" in r.stderr


def test_an_empty_scan_dir_is_2_not_clean(new_repo) -> None:
    r = _run(tracked_repo(new_repo, {"app/README.md": "no python" + NL}))
    assert r.returncode == 2, r.stdout + r.stderr


def test_no_scan_dir_is_2(new_repo) -> None:
    r = run_tool("sql.casts", tracked_repo(new_repo, {"app/ok.py": GOOD}))
    assert r.returncode == 2


def test_selftest_passes(new_repo) -> None:
    assert _run(tracked_repo(new_repo, {"app/ok.py": GOOD}), "--selftest").returncode == 0
    # bare, as the action's CI step runs it: no --scan-dir is not a reason to refuse a selftest
    r = run_tool("sql.casts", new_repo("bare"), "--selftest")
    assert r.returncode == 0, r.stdout + r.stderr
