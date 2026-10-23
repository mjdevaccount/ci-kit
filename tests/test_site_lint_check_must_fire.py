"""ci_kit.site_lint.check -- the pass/fail CLI, driven like a caller drives it.

No baseline: any finding is 1, none is 0, a scan that could not look is 2 (its own SystemExit or
any crash), and the selftest flag, mode flags and every voice hook are proven in both directions.
"""
from __future__ import annotations

import sys

import pytest

from ci_kit import site_lint


def _check(argv, findings=(), **kw) -> int:
    kw.setdefault("selftest", lambda: [])
    return site_lint.check(argv, gate="toy", scan=lambda: list(findings), **kw)


def test_findings_are_1_and_printed_with_the_default_voice(capsys) -> None:
    assert _check([], ["a.py: bad", "b.py: worse"]) == 1
    out = capsys.readouterr().out
    assert "toy: 2 finding(s)" in out
    assert "  a.py: bad" in out and "  b.py: worse" in out


def test_clean_is_0_with_the_default_ok(capsys) -> None:
    assert _check([], []) == 0
    assert capsys.readouterr().out.strip() == "toy: OK"


def test_crash_is_2_and_systemexit_2_passes_through(capsys) -> None:
    def boom():
        raise ValueError("bad input")
    assert site_lint.check([], gate="toy", scan=boom, selftest=lambda: []) == 2
    assert "toy: DID NOT FINISH LOOKING - ValueError: bad input" in capsys.readouterr().out

    def blind():
        print("toy: could not look -- no tree")
        raise SystemExit(2)
    with pytest.raises(SystemExit) as e:
        site_lint.check([], gate="toy", scan=blind, selftest=lambda: [])
    assert e.value.code == 2


def test_floor_refuses_a_scan_that_read_nothing() -> None:
    with pytest.raises(SystemExit) as e:
        _check([], [], scanned=lambda: 0, scan_dirs=["svc"])
    assert e.value.code == 2
    assert _check([], [], scanned=lambda: 3, scan_dirs=["svc"], floor=3) == 0


def test_selftest_both_directions_and_its_exit_code(capsys) -> None:
    assert _check(["--selftest"], ["ignored"], selftest=lambda: []) == 0
    assert "toy: selftest OK" in capsys.readouterr().out
    assert _check(["--selftest"], selftest=lambda: ["missed x"]) == 1
    out = capsys.readouterr().out
    assert "  SELFTEST FAIL: missed x" in out and "toy: selftest FAILED" in out
    assert _check(["--selftest"], selftest=lambda: ["m"], selftest_fail=2) == 2


def test_selftest_report_replaces_the_lines(capsys) -> None:
    assert _check(["--selftest"], selftest=lambda: ["m1", "m2"],
                  selftest_report=lambda f: "; ".join(f) or "fine") == 1
    assert capsys.readouterr().out.strip() == "m1; m2"
    assert _check(["--selftest"], selftest_report=lambda f: None) == 0
    assert capsys.readouterr().out == ""


def test_a_mode_flag_runs_instead_of_the_check() -> None:
    seen = []
    modes = {"--list-allowed": lambda: seen.append("list") or 0, "--write": lambda: 2}
    assert _check(["--list-allowed"], ["would fail"], modes=modes) == 0
    assert seen == ["list"]
    assert _check(["--write"], modes=modes) == 2                 # a refused write keeps its code
    assert _check([], ["f"], modes=modes) == 1                   # no flag -> the check


def test_line_hooks_replace_every_line(capsys) -> None:
    assert _check([], [("a.py", 3)], header=lambda n: f"[toy] {n} bad", describe=lambda f: f"{f[0]}:{f[1]}",
                  advice=["fix it"]) == 1
    assert capsys.readouterr().out.splitlines() == ["[toy] 1 bad", "  a.py:3", "  fix it"]
    assert _check([], [], ok=lambda: "[toy] clean, 4 judged") == 0
    assert capsys.readouterr().out.strip() == "[toy] clean, 4 judged"


def test_report_owns_all_output_on_both_outcomes_and_the_exit_stays_the_kits(capsys) -> None:
    calls = []

    def report(findings):
        calls.append(list(findings))
        print(f"stale: {len(findings)}", file=sys.stderr)
        print("toy: N flows checked -- no NEW pins")              # OK line that still exits 1
    assert _check([], ["stale x"], report=report, ok=lambda: "never") == 1
    assert _check([], [], report=report) == 0
    cap = capsys.readouterr()
    assert calls == [["stale x"], []]
    assert "never" not in cap.out and cap.err.count("stale:") == 2
