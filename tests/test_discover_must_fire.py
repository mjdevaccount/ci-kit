"""ci_kit.discover -- must fire.

Both directions on real git repos: tracked files are found (repo-relative, POSIX, from a checkout
that lives under `.claude/worktrees/`), untracked ones are not, and every way of being unable to
look is exit 2 -- never an empty list that reads as a clean tree.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import git, write

from ci_kit import discover


def _repo_with(new_repo, files: dict[str, str], name: str = "r") -> Path:
    r = new_repo(name)
    for rel, text in files.items():
        write(r / rel, text)
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


def test_finds_tracked_skips_untracked_and_other_suffixes(new_repo) -> None:
    r = _repo_with(new_repo, {"pkg/a.py": "x=1\n", "pkg/sub/b.py": "y=2\n", "pkg/c.txt": "no\n"})
    write(r / "pkg/untracked.py", "z=3\n")  # a walk would see this; CI would not
    assert discover.tracked(["pkg"], root=r) == ["pkg/a.py", "pkg/sub/b.py"]
    assert discover.tracked(["pkg"], suffix=".txt", root=r) == ["pkg/c.txt"]


def test_a_dot_claude_checkout_location_does_not_blind_it(new_repo, tmp_path: Path) -> None:
    # THE DEFECT THIS MODULE EXISTS FOR: a checkout under `.claude/worktrees/` saw nothing.
    src = _repo_with(new_repo, {"svc/m.py": "x=1\n"}, name="src")
    wt = tmp_path / "host" / ".claude" / "worktrees" / "w1"
    wt.parent.mkdir(parents=True)
    assert git(src, "worktree", "add", "-q", str(wt)).returncode == 0
    got = discover.tracked(["svc"], root=wt)
    assert got == ["svc/m.py"]
    assert not any(p.startswith("/") or (len(p) > 1 and p[1] == ":") for p in got), "absolute path"
    assert not any(chr(92) in p for p in got), "backslash path"


def test_default_root_is_the_cwd_checkout(new_repo, monkeypatch: pytest.MonkeyPatch) -> None:
    r = _repo_with(new_repo, {"svc/m.py": "x=1\n", "svc/deep/n.py": "y=1\n"})
    monkeypatch.chdir(r / "svc" / "deep")  # from a SUBDIRECTORY, paths stay repo-relative
    assert discover.tracked(["svc"]) == ["svc/deep/n.py", "svc/m.py"]


def test_outside_any_checkout_is_exit_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lone = tmp_path / "not_a_repo"
    lone.mkdir()
    monkeypatch.chdir(lone)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    with pytest.raises(SystemExit) as e:
        discover.tracked(["anything"])
    assert e.value.code == 2


def test_missing_scan_dir_is_exit_2_not_empty(new_repo) -> None:
    r = _repo_with(new_repo, {"svc/m.py": "x=1\n"})
    with pytest.raises(SystemExit) as e:
        discover.tracked(["no_such_dir_at_all"], root=r)
    assert e.value.code == 2


def test_git_failure_is_exit_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # a directory that exists but is not a repository: ls-files fails, and that must not read clean
    d = tmp_path / "plain"
    (d / "svc").mkdir(parents=True)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    with pytest.raises(SystemExit) as e:
        discover.tracked(["svc"], root=d)
    assert e.value.code == 2


def test_git_missing_is_exit_2_not_a_traceback(new_repo, monkeypatch: pytest.MonkeyPatch) -> None:
    r = _repo_with(new_repo, {"svc/m.py": "x=1\n"})
    monkeypatch.setenv("PATH", "")  # no git binary reachable -> FileNotFoundError inside
    with pytest.raises(SystemExit) as e:
        discover.tracked(["svc"], root=r)
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        discover.repo_root(r)
    assert e.value.code == 2


def test_require_scanned_floor_both_directions() -> None:
    with pytest.raises(SystemExit) as e:
        discover.require_scanned(0, gate="probe", dirs=["svc"])
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        discover.require_scanned(99, gate="probe", dirs=["svc"], floor=100)
    assert e.value.code == 2
    discover.require_scanned(1, gate="probe", dirs=["svc"])           # a real count passes
    discover.require_scanned(100, gate="probe", dirs=["svc"], floor=100)


def test_count_by_dir_counts_every_ruled_dir_and_zero_for_a_dark_one() -> None:
    got = discover.count_by_dir(["svc/a.py", "svc/b/c.py", "src/d.py", "svcx/e.py"],
                                ("svc", "src", "scripts/"))
    assert got == {"svc": 2, "src": 1, "scripts/": 0}      # `svcx/` is not under `svc/`


def test_require_scanned_each_both_directions() -> None:
    floors = {"svc": 2, "src": 1}
    discover.require_scanned_each({"svc": 2, "src": 1}, floors, gate="probe")    # at floor passes
    for by_dir in ({"svc": 2, "src": 0}, {"svc": 1, "src": 5}, {"svc": 9}):       # last: src never counted
        with pytest.raises(SystemExit) as e:
            discover.require_scanned_each(by_dir, floors, gate="probe")
        assert e.value.code == 2


def test_require_scanned_each_names_the_dark_dir(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        discover.require_scanned_each({"svc": 5, "src": 0}, {"svc": 1, "src": 1}, gate="probe")
    assert "probe[src]" in capsys.readouterr().err


def test_env_has_no_git_dir_leak() -> None:
    # Guard the guard: a GIT_DIR in the test env would point every `git -C` at one repo and make
    # the cases above prove nothing.
    assert "GIT_DIR" not in os.environ
