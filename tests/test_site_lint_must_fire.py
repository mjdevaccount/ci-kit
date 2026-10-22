"""ci_kit.site_lint -- the shared site-count gate, driven like a caller drives it.

A toy predicate (lines containing `now()`) over a real git repo: grown -> 1, unchanged or shrunk
-> 0, unreadable or under the floor -> 2, and the fixture selftest proven in both directions.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from conftest import git, write

from ci_kit import site_lint
from ci_kit.ratchet import RatchetCount


def _now_calls(source: str, origin: str) -> list[int]:
    return [i for i, ln in enumerate(source.splitlines(), 1) if "now()" in ln]


def _repo(new_repo, files: dict[str, str]) -> Path:
    r = new_repo()
    for rel, text in files.items():
        write(r / rel, text)
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


def _cli(r: Path, argv: list[str], *, floor: int = 1) -> int:
    rc = RatchetCount(r / "baseline.txt", header="# toy\n")
    state: dict[str, int] = {}

    def scan() -> dict[str, int]:
        counts, n = site_lint.count_sites(r, ["svc"], [], _now_calls, gate="toy")
        state["n"] = n
        return counts

    return site_lint.run(argv, gate="toy", scan=scan, scanned=lambda: state["n"], scan_dirs=["svc"],
                         ratchet=rc, selftest=lambda: [], gained="now() sites",
                         advice=["use the clock"], floor=floor)


def test_count_sites_reads_tracked_only_and_bom_tolerant(new_repo) -> None:
    r = _repo(new_repo, {"svc/a.py": "x = now()\ny = now()\n", "svc/b.py": "clean\n"})
    (r / "svc" / "c.py").write_bytes(bytes([0xEF, 0xBB, 0xBF]) + b"z = now()\n")
    git(r, "add", "svc/c.py")
    write(r / "svc/untracked.py", "now()\n")
    counts, n = site_lint.count_sites(r, ["svc"], ["svc/b.py"], _now_calls, gate="toy")
    assert counts == {"svc/a.py": 2, "svc/c.py": 1}
    assert n == 2


def test_cli_grown_is_1_unchanged_and_shrunk_are_0(new_repo, capsys) -> None:
    r = _repo(new_repo, {"svc/a.py": "now()\n", "svc/b.py": "ok\n"})
    assert _cli(r, ["--write"]) == 0
    assert _cli(r, []) == 0                                  # unchanged
    write(r / "svc/a.py", "now()\nnow()\n")
    assert _cli(r, []) == 1                                  # grown -> fails
    assert "svc/a.py: 2 site(s), baseline 1" in capsys.readouterr().out
    write(r / "svc/a.py", "fine\n")
    assert _cli(r, []) == 0                                  # shrunk -> progress, never a failure
    assert "shrank - tighten with --write" in capsys.readouterr().out


def test_cli_new_file_with_sites_is_1(new_repo) -> None:
    r = _repo(new_repo, {"svc/a.py": "ok\n"})
    assert _cli(r, ["--write"]) == 0
    write(r / "svc/new.py", "now()\n")
    git(r, "add", "svc/new.py")
    assert _cli(r, []) == 1


def test_cli_under_floor_is_exit_2(new_repo) -> None:
    r = _repo(new_repo, {"svc/a.py": "ok\n"})
    with pytest.raises(SystemExit) as e:
        _cli(r, [], floor=100)
    assert e.value.code == 2


def test_cli_scan_crash_is_2(new_repo) -> None:
    r = _repo(new_repo, {"svc/a.py": "ok\n"})

    def boom() -> dict[str, int]:
        raise RuntimeError("scanner broke")

    assert site_lint.run([], gate="toy", scan=boom, scanned=lambda: 1, scan_dirs=["svc"],
                         ratchet=RatchetCount(r / "b.txt"), selftest=lambda: [], gained="x",
                         advice=[]) == 2


def test_undecodable_file_is_exit_2_not_skipped(new_repo) -> None:
    r = _repo(new_repo, {"svc/a.py": "ok\n"})
    (r / "svc" / "bad.py").write_bytes(b"x = 1\n\xff\xfe\xfa\n")
    git(r, "add", "svc/bad.py")
    with pytest.raises(SystemExit) as e:
        site_lint.count_sites(r, ["svc"], [], _now_calls, gate="toy")
    assert e.value.code == 2


def test_injected_reader_is_used(new_repo) -> None:
    r = _repo(new_repo, {"svc/a.py": "ok\n"})
    counts, _ = site_lint.count_sites(r, ["svc"], [], _now_calls, gate="toy",
                                      read=lambda p, rel: "now()\n")
    assert counts == {"svc/a.py": 1}


def test_fixture_selftest_both_directions(tmp_path: Path) -> None:
    fx = tmp_path / "fx"
    write(fx / "bad.py", "a = now()  # BAD\nb = 1\n")
    write(fx / "good.py", "c = 2\n")
    assert site_lint.fixture_selftest(tmp_path, fx, _now_calls, noun="now()") == []
    # a predicate that misses the marked line must fail the selftest
    assert site_lint.fixture_selftest(tmp_path, fx, lambda s, o: [], noun="now()") == [
        "MISSED now() at bad.py:1"]
    # a predicate that flags everything fails on the unmarked line AND on good.py
    every = site_lint.fixture_selftest(
        tmp_path, fx, lambda s, o: list(range(1, len(s.splitlines()) + 1)), noun="now()")
    assert "flagged an unmarked line bad.py:2" in every
    assert "FALSE POSITIVE at good.py:1" in every
    # a bad.py with no marker proves nothing
    write(fx / "bad.py", "a = now()\n")
    assert "bad.py has no `# BAD` lines - the fixture proves nothing" in site_lint.fixture_selftest(
        tmp_path, fx, _now_calls, noun="now()")
    (fx / "good.py").unlink()
    assert site_lint.fixture_selftest(tmp_path, fx, _now_calls, noun="now()") == [
        "fixture missing: fx/good.py"]


def test_selftest_flag_reports(capsys) -> None:
    def args(fails):
        return dict(gate="toy", scan=dict, scanned=lambda: 0, scan_dirs=["svc"],
                    ratchet=RatchetCount(Path("unused.txt")), selftest=lambda: fails,
                    gained="x", advice=[])
    assert site_lint.run(["--selftest"], **args([])) == 0
    assert site_lint.run(["--selftest"], **args(["nope"])) == 1
