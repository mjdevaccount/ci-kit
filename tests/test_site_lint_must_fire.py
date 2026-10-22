"""ci_kit.site_lint -- the shared site-count gate, driven like a caller drives it.

A toy predicate (lines containing `now()`) over a real git repo: grown -> 1, unchanged or shrunk
-> 0, unreadable or under the floor -> 2, and the fixture selftest proven in both directions.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from conftest import git, write

from ci_kit import site_lint
from ci_kit.ratchet import RatchetCount, RatchetSet


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
    # a gate that shipped exiting 2 on a failed selftest keeps 2
    assert site_lint.run(["--selftest"], selftest_fail=2, **args(["nope"])) == 2


# -- the set-of-keys shape: same CLI, a key the baseline does not list is the regression ----------

def _set_cli(base: Path, keys: set[str], argv: list[str], **hooks) -> int:
    return site_lint.run(argv, gate="toy", scan=lambda: set(keys), scanned=lambda: 5,
                         scan_dirs=["svc"], ratchet=RatchetSet(base, header="# toy set\n"),
                         selftest=lambda: [], gained="handler(s)", advice=["fix it"], floor=1,
                         **hooks)


def test_set_new_key_is_1_and_names_it(tmp_path: Path, capsys) -> None:
    base = tmp_path / "b.txt"
    assert _set_cli(base, {"a.py::f"}, ["--write"]) == 0
    assert base.read_text(encoding="utf-8") == "# toy set\na.py::f\n"
    assert _set_cli(base, {"a.py::f"}, []) == 0                     # unchanged
    capsys.readouterr()
    assert _set_cli(base, {"a.py::f", "b.py::g"}, []) == 1          # new key -> fails
    out = capsys.readouterr().out
    assert "1 new handler(s) the baseline does not list" in out
    assert "  b.py::g" in out and "a.py::f" not in out.split("list:")[1]
    assert "  fix it" in out


def test_set_shrunk_is_0_with_a_tighten_note(tmp_path: Path, capsys) -> None:
    base = tmp_path / "b.txt"
    _set_cli(base, {"a", "b"}, ["--write"])
    capsys.readouterr()
    assert _set_cli(base, {"a"}, []) == 0
    assert "1 baseline key(s) gone - tighten with --write" in capsys.readouterr().out


def test_set_scan_returning_a_mapping_counts_by_keys(tmp_path: Path) -> None:
    base = tmp_path / "b.txt"
    rs = RatchetSet(base)
    rs.write({"a"})
    assert site_lint.run([], gate="toy", scan=lambda: {"a": 300, "b": 12}, scanned=lambda: 5,
                         scan_dirs=["svc"], ratchet=rs, selftest=lambda: [], gained="x",
                         advice=[], floor=1) == 1


def test_set_write_flags_spelling(tmp_path: Path) -> None:
    base = tmp_path / "b.txt"
    assert _set_cli(base, {"a"}, ["--write-baseline"], write_flags=("--write", "--write-baseline")) == 0
    assert base.exists()
    # a spelling the gate does not accept is NOT a write: the check runs (and here passes)
    assert _set_cli(base, {"a"}, ["--write-baseline"]) == 0
    assert base.read_text(encoding="utf-8").splitlines()[-1] == "a"


def test_set_under_floor_is_2(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as e:
        site_lint.run([], gate="toy", scan=set, scanned=lambda: 0, scan_dirs=["svc"],
                      ratchet=RatchetSet(tmp_path / "b.txt"), selftest=lambda: [], gained="x",
                      advice=[], floor=1)
    assert e.value.code == 2


def test_a_scan_raising_systemexit_2_passes_through(tmp_path: Path) -> None:
    """A gate's own refusal (a missing input, a second floor) raised inside scan stays 2."""
    def refuse() -> set[str]:
        raise SystemExit(2)
    with pytest.raises(SystemExit) as e:
        site_lint.run([], gate="toy", scan=refuse, scanned=lambda: 9, scan_dirs=["svc"],
                      ratchet=RatchetSet(tmp_path / "b.txt"), selftest=lambda: [], gained="x",
                      advice=[], floor=1)
    assert e.value.code == 2


# -- the voice hooks: the mechanics are the kit's, the words stay the gate's ---------------------

def test_count_hooks_replace_every_line(new_repo, capsys) -> None:
    r = _repo(new_repo, {"svc/a.py": "now()\n"})
    rc = RatchetCount(r / "baseline.txt", header="# toy\n")
    state: dict[str, int] = {}

    def scan() -> dict[str, int]:
        counts, state["n"] = site_lint.count_sites(r, ["svc"], [], _now_calls, gate="toy")
        return counts

    hooks = dict(header=lambda n: f"HEADER {n}",
                 describe=lambda rel, now, base: f"{'NEW' if base == 0 else 'GREW'}: {rel} {base}->{now}",
                 ok=lambda cur, slack: f"OKLINE {sum(cur.values())} slack={len(slack)}",
                 written=lambda cur: f"WROTE {len(cur)}")

    def cli(argv):
        return site_lint.run(argv, gate="toy", scan=scan, scanned=lambda: state["n"], scan_dirs=["svc"],
                             ratchet=rc, selftest=lambda: [], gained="g", advice=[], floor=1, **hooks)

    assert cli(["--write"]) == 0
    assert capsys.readouterr().out.strip() == "WROTE 1"
    assert cli([]) == 0
    assert capsys.readouterr().out.strip() == "OKLINE 1 slack=0"
    write(r / "svc/a.py", "now()\nnow()\n")
    write(r / "svc/b.py", "now()\n")
    git(r, "add", "svc/b.py")
    assert cli([]) == 1
    out = capsys.readouterr().out
    assert "HEADER 2" in out and "GREW: svc/a.py 1->2" in out and "NEW: svc/b.py 0->1" in out
    write(r / "svc/a.py", "x\n")
    git(r, "rm", "-q", "--cached", "svc/b.py")
    assert cli([]) == 0
    assert capsys.readouterr().out.strip() == "OKLINE 0 slack=1"


def test_hard_findings_fail_with_a_clean_ratchet_and_print(tmp_path: Path, capsys) -> None:
    """A finding no baseline grants (a bare waiver) fails even when the ratchet is satisfied --
    and the empty-hard path stays clean, the other direction."""
    base = tmp_path / "b.txt"
    _set_cli(base, {"a"}, ["--write"])
    capsys.readouterr()
    assert _set_cli(base, {"a"}, [], hard=lambda cur: []) == 0
    assert _set_cli(base, {"a"}, [], hard=lambda cur: [f"bare waiver near {sorted(cur)[0]}"]) == 1
    out = capsys.readouterr().out
    assert "  bare waiver near a" in out
    assert "the baseline does not list" not in out, "no regression -> no regression header"


def test_hard_findings_do_not_block_a_write(tmp_path: Path) -> None:
    base = tmp_path / "b.txt"
    assert _set_cli(base, {"a"}, ["--write"], hard=lambda cur: ["x"]) == 0


def test_set_describe_hook(tmp_path: Path, capsys) -> None:
    base = tmp_path / "b.txt"
    _set_cli(base, set(), ["--write"])
    capsys.readouterr()
    assert _set_cli(base, {"k"}, [], describe=lambda k: f"NEW: {k} -- explain") == 1
    assert "  NEW: k -- explain" in capsys.readouterr().out
