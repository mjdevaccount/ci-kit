"""ci-dup-code: a planted clone -> 1, a clean tree -> 0, nothing to analyze -> 2, driven as a caller
runs it -- the REAL jscpd binary (pinned, sha256-verified, cached under CI_KIT_CACHE_DIR), no mock.

The baseline cases are the ones a green CI run can never show: frozen clones pass, a NEW clone
fails, an edit INSIDE a frozen clone is a new clone (consolidate, not re-freeze), removing one shrinks
quietly, and --write-baseline REFUSES to grow.
"""
from __future__ import annotations

import json

from _sqlrepo import tracked_repo
from conftest import NL, git, run_tool, write

BODY = NL.join(f"    v{i} = compute({i}, {i + 1}, {i + 2}) + other(v{max(i - 1, 0)}, {i})" for i in range(14))
ONE = "def f():" + NL + BODY + NL + "    return v0" + NL
TWO = "import os" + NL * 3 + "def g():" + NL + BODY + NL + "    return v1" + NL
BODY2 = NL.join(f"    w{i} = load({i}) or fetch({i}, {i * 2}); emit(w{i}, {i + 3})" for i in range(14))
THREE = "def h():" + NL + BODY2 + NL + "    return w0" + NL
FOUR = "def k():" + NL + BODY2 + NL + "    return w1" + NL
# each clean file is longer than --min-lines: jscpd does not COUNT a shorter file as a source, and
# zero sources is exit 2 (could not look), so a clean pass needs files it actually read
CLEAN = {"a/one.py": "def f(x):" + NL + NL.join(f"    x = step{i}(x, {i})" for i in range(10)) + NL + "    return x" + NL,
         "a/two.py": "def g(y):" + NL + NL.join(f"    y = walk{i}(y) - {i * 3}" for i in range(10)) + NL + "    return y" + NL}


def _run(repo, *extra):
    return run_tool("dup_code", repo, "--root", str(repo), *extra)


def test_a_planted_clone_fires_with_both_positions(new_repo) -> None:
    r = _run(tracked_repo(new_repo, {"a/one.py": ONE, "a/two.py": TWO}))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "1 new clone(s)" in r.stdout and "a/one.py:1-" in r.stdout and "a/two.py:4-" in r.stdout, r.stdout


def test_a_clean_tree_passes(new_repo) -> None:
    r = _run(tracked_repo(new_repo, CLEAN))
    assert r.returncode == 0 and "none new" in r.stdout, r.stdout + r.stderr


def test_an_untracked_copy_is_not_a_finding(new_repo) -> None:
    # CI has only tracked files; a stray local copy must not fail (or pass) differently
    repo = tracked_repo(new_repo, {"a/one.py": ONE})
    write(repo / "a/scratch.py", TWO)
    r = _run(repo)
    assert r.returncode == 0, r.stdout + r.stderr


def test_nothing_analyzed_is_2_not_clean(new_repo) -> None:
    r = _run(tracked_repo(new_repo, {"README.md": "# only prose" + NL}))
    assert r.returncode == 2, r.stdout + r.stderr


def test_a_missing_binary_is_2(new_repo, monkeypatch) -> None:
    monkeypatch.setenv("CI_KIT_JSCPD", str(new_repo("elsewhere") / "no-such-jscpd"))
    r = _run(tracked_repo(new_repo, {"a/one.py": ONE}))
    assert r.returncode == 2 and "DID NOT FINISH LOOKING" in r.stdout + r.stderr, r.stdout + r.stderr


def test_frozen_clones_pass_and_a_new_one_fails(new_repo) -> None:
    repo = tracked_repo(new_repo, {"a/one.py": ONE, "a/two.py": TWO})
    w = _run(repo, "--write-baseline")
    assert w.returncode == 0 and "1 clone(s)" in w.stdout, w.stdout + w.stderr
    base = json.loads((repo / "ci/jscpd-baseline.json").read_text(encoding="utf-8"))
    assert base["version"] == 1 and sum(base["fingerprints"].values()) == 1
    assert _run(repo).returncode == 0
    write(repo / "b/three.py", THREE)
    write(repo / "b/four.py", FOUR)
    git(repo, "add", "-A")
    r = _run(repo)
    assert r.returncode == 1 and "1 new clone(s)" in r.stdout and "b/three.py" in r.stdout, r.stdout


def test_a_line_shift_in_a_frozen_clone_is_not_new(new_repo) -> None:
    repo = tracked_repo(new_repo, {"a/one.py": ONE, "a/two.py": TWO})
    assert _run(repo, "--write-baseline").returncode == 0
    write(repo / "a/two.py", "# moved" + NL * 4 + TWO)
    assert _run(repo).returncode == 0


def test_an_edit_inside_a_frozen_clone_is_a_new_clone(new_repo) -> None:
    repo = tracked_repo(new_repo, {"a/one.py": ONE, "a/two.py": TWO})
    assert _run(repo, "--write-baseline").returncode == 0
    write(repo / "a/two.py", TWO.replace("compute(6, 7, 8)", "compute(6, 7, 9)"))
    r = _run(repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "That split it" in r.stdout


def test_a_removed_clone_shrinks_quietly_and_the_refreeze_tightens(new_repo) -> None:
    repo = tracked_repo(new_repo, {"a/one.py": ONE, "a/two.py": TWO, "b/three.py": THREE, "b/four.py": FOUR})
    assert _run(repo, "--write-baseline").returncode == 0
    write(repo / "b/four.py", "from b.three import h" + NL)
    r = _run(repo)
    assert r.returncode == 0 and "1 baselined clone(s) gone" in r.stdout, r.stdout
    w = _run(repo, "--write-baseline")
    assert w.returncode == 0 and "1 clone(s)" in w.stdout and "(was 2)" in w.stdout, w.stdout


def test_write_baseline_refuses_to_grow(new_repo) -> None:
    repo = tracked_repo(new_repo, {"a/one.py": ONE, "a/two.py": TWO})
    assert _run(repo, "--write-baseline").returncode == 0
    before = (repo / "ci/jscpd-baseline.json").read_text(encoding="utf-8")
    write(repo / "b/three.py", THREE)
    write(repo / "b/four.py", FOUR)
    git(repo, "add", "-A")
    w = _run(repo, "--write-baseline")
    assert w.returncode == 1 and "REFUSING to grow the baseline 1 -> 2" in w.stdout, w.stdout + w.stderr
    assert (repo / "ci/jscpd-baseline.json").read_text(encoding="utf-8") == before, "a refused write changed the file"


def test_kit_ignores_skip_fixtures_and_migrations_and_extra_ignore_is_added(new_repo) -> None:
    repo = tracked_repo(new_repo, {"tests/fixtures/one.py": ONE, "db/migrations/two.py": TWO, "a/x.py": ONE,
                                   "a/y.py": TWO})
    r = _run(repo, "--ignore", "a/y.py")
    # one.py/two.py under fixtures + migrations: ignored; a/y.py: the caller's ignore; a/x.py alone pairs with nothing
    assert r.returncode == 0, r.stdout + r.stderr
    assert _run(repo).returncode == 1


def test_selftest_runs_both_directions(new_repo) -> None:
    r = _run(new_repo(), "--selftest")
    assert r.returncode == 0 and "selftest OK" in r.stdout, r.stdout + r.stderr
