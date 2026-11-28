"""ci-guards-have-tests must fire: a guard named only in prose -> 1, one named in code -> 0, a guard
named only on a deselected lane is reported but passes, no tests / no guard / a broken test -> 2."""
from __future__ import annotations

from _pylint_repo import again, run
from conftest import write

ARGS = ("--dir", "tests:1", "--deselected-mark", "integration")
GUARDS = {"scripts/lint_a.py": "x = 1\n", "scripts/lint_b.py": "x = 1\n"}
TESTED = "from scripts import lint_a\n\n\ndef test_it() -> None:\n    assert lint_a\n"
PROSE = ('"""Mentions lint_b only in prose, which proves nothing."""\n# lint_b in a comment\n\n\n'
         "def test_x() -> None:\n    assert True\n")


def test_a_guard_named_only_in_prose_fires(new_repo) -> None:
    _, r = run(new_repo, "guards_have_tests", {**GUARDS, "tests/test_a.py": TESTED, "tests/test_b.py": PROSE}, *ARGS)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "scripts/lint_b.py" in r.stdout and "scripts/lint_a.py" not in r.stdout


def test_a_guard_named_in_code_passes_and_a_string_argument_counts(new_repo) -> None:
    load = 'def test_b() -> None:\n    assert _load("lint_b")\n'
    _, r = run(new_repo, "guards_have_tests", {**GUARDS, "tests/test_a.py": TESTED, "tests/test_b.py": load}, *ARGS)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "2/2 guards named by a test outside prose" in r.stdout


def test_a_baselined_guard_passes(new_repo) -> None:
    repo, r = run(new_repo, "guards_have_tests", {**GUARDS, "tests/test_a.py": TESTED}, *ARGS, "--write-baseline")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "lint_b" in (repo / "ci/guards_without_tests.txt").read_text(encoding="utf-8")
    assert again("guards_have_tests", repo, *ARGS).returncode == 0


def test_a_proof_only_on_a_deselected_lane_is_reported(new_repo) -> None:
    desel = "import pytest\n\npytestmark = pytest.mark.integration\n\n\ndef test_b() -> None:\n    assert 'lint_b'\n"
    _, r = run(new_repo, "guards_have_tests", {**GUARDS, "tests/test_a.py": TESTED, "tests/test_b.py": desel}, *ARGS)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ONLY in tests the merge lane deselects (integration): lint_b" in r.stdout


def test_no_tests_is_2(new_repo) -> None:
    _, r = run(new_repo, "guards_have_tests", GUARDS, *ARGS)
    assert r.returncode == 2, r.stdout + r.stderr


def test_no_guard_is_2(new_repo) -> None:
    _, r = run(new_repo, "guards_have_tests", {**GUARDS, "tests/test_a.py": TESTED}, *ARGS, "--guards", "tools")
    assert r.returncode == 2, r.stdout + r.stderr


def test_an_empty_suffix_or_prefix_is_2(new_repo) -> None:
    repo, _ = run(new_repo, "guards_have_tests", {**GUARDS, "tests/test_a.py": TESTED}, *ARGS)
    assert again("guards_have_tests", repo, *ARGS, "--suffix=").returncode == 2
    assert again("guards_have_tests", repo, *ARGS, "--prefix=").returncode == 2


def test_an_unparseable_test_is_2(new_repo) -> None:
    repo, _ = run(new_repo, "guards_have_tests", {**GUARDS, "tests/test_a.py": TESTED}, *ARGS)
    write(repo / "tests/test_a.py", "def broken(:\n")
    r = again("guards_have_tests", repo, *ARGS)
    assert r.returncode == 2 and "tests/test_a.py" in r.stderr, r.stdout + r.stderr
