"""ci-quarantine must fire: an expired or reasonless mark -> 1, a complete unexpired one -> 0, no
tracked test file or an unknown time zone -> 2; --summary writes the table."""
from __future__ import annotations

from _pylint_repo import run

MARK = 'import pytest\n\n@pytest.mark.quarantine({})\ndef test_flaky():\n    pass\n'
OK = MARK.format('reason="port race", issue="td#12", until="2026-10-10"')
TODAY = ("--today", "2026-09-28")


def test_an_expired_mark_fires(new_repo) -> None:
    _, r = run(new_repo, "quarantine", {"tests/test_a.py": OK}, "--today", "2026-10-11")
    assert r.returncode == 1 and "EXPIRED - until=2026-10-10" in r.stdout, r.stdout + r.stderr


def test_a_mark_with_no_reason_fires(new_repo) -> None:
    _, r = run(new_repo, "quarantine", {"tests/test_a.py": MARK.format('issue="#3", until="2026-10-01"')}, *TODAY)
    assert r.returncode == 1 and "missing reason=" in r.stdout, r.stdout + r.stderr


def test_a_complete_mark_passes_and_the_summary_lists_it(new_repo, tmp_path) -> None:
    out = tmp_path / "summary.md"
    _, r = run(new_repo, "quarantine", {"tests/test_a.py": OK}, *TODAY, "--summary", str(out))
    assert r.returncode == 0 and "1 quarantine mark(s)" in r.stdout, r.stdout + r.stderr
    assert "`tests/test_a.py::test_flaky` | td#12 | 2026-10-10 | port race |" in out.read_text(encoding="utf-8")


def test_no_tracked_test_file_is_2(new_repo) -> None:
    _, r = run(new_repo, "quarantine", {"src/a.py": "x = 1\n"}, *TODAY)
    assert r.returncode == 2, r.stdout + r.stderr


def test_an_unknown_zone_is_2_not_a_guessed_date(new_repo) -> None:
    _, r = run(new_repo, "quarantine", {"tests/test_a.py": OK}, "--tz", "Mars/Olympus_Mons")
    assert r.returncode == 2 and "--tz" in r.stderr, r.stdout + r.stderr
