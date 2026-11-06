"""v1.14.0 lint helpers -- must fire, both directions.

`discover.tracked_each` (strict: every dir contributes or exit 2), `discover.tracked_or_empty`
(lenient: no scan dir at all is [], a git failure is still exit 2), `site_lint.waived_lines`
(a waiver must say why) and `site_lint.repo_on_path` (only when the checkout holds the marker).
Each replaced copies in consumer gates, so each is proven to refuse AND to pass.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from conftest import git, write

from ci_kit import discover, site_lint


def _repo(new_repo, files: dict[str, str]) -> Path:
    r = new_repo()
    for rel, text in files.items():
        write(r / rel, text)
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


# --- tracked_each ---------------------------------------------------------------------------

def test_tracked_each_returns_absolute_paths_and_drops_excluded(new_repo) -> None:
    r = _repo(new_repo, {"svc/a.py": "x=1\n", "svc/self.py": "y=1\n", "src/b.py": "z=1\n"})
    write(r / "svc/untracked.py", "u=1\n")
    got = discover.tracked_each(("svc", "src"), gate="probe", root=r, exclude=(r / "svc/self.py",))
    assert got == [r / "svc/a.py", r / "src/b.py"]
    assert all(p.is_absolute() for p in got)


def test_tracked_each_a_dark_dir_is_exit_2_naming_it(new_repo, capsys) -> None:
    r = _repo(new_repo, {"svc/a.py": "x=1\n", "src/notes.txt": "no py here\n"})
    with pytest.raises(SystemExit) as e:
        discover.tracked_each(("svc", "src"), gate="probe", root=r)
    assert e.value.code == 2
    assert "probe[src]" in capsys.readouterr().err


def test_tracked_each_a_missing_dir_is_exit_2(new_repo) -> None:
    r = _repo(new_repo, {"svc/a.py": "x=1\n"})
    with pytest.raises(SystemExit) as e:
        discover.tracked_each(("svc", "gone"), gate="probe", root=r)
    assert e.value.code == 2


# --- tracked_or_empty -------------------------------------------------------------------------

def test_tracked_or_empty_finds_tracked_absolute(new_repo) -> None:
    r = _repo(new_repo, {"svc/a.py": "x=1\n", "svc/b.txt": "no\n"})
    write(r / "svc/untracked.py", "u=1\n")
    assert discover.tracked_or_empty(("svc", "absent"), root=r) == [r / "svc/a.py"]


def test_tracked_or_empty_no_scan_dir_is_empty_not_exit(new_repo) -> None:
    r = _repo(new_repo, {"svc/a.py": "x=1\n"})
    assert discover.tracked_or_empty(("absent", "also_absent"), root=r) == []


def test_tracked_or_empty_git_failure_is_still_exit_2(tmp_path: Path, monkeypatch) -> None:
    d = tmp_path / "plain"
    write(d / "svc/a.py", "x=1\n")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    with pytest.raises(SystemExit) as e:
        discover.tracked_or_empty(("svc",), root=d)
    assert e.value.code == 2


# --- waived_lines -----------------------------------------------------------------------------

def test_waived_lines_justified_bare_and_absent() -> None:
    text = "\n".join([
        "a = 0  # zero-ok: the feed sends 0 for a closed book",   # 1 justified
        "b = 0  # zero-ok",                                         # 2 bare
        "c = 0  # zero-ok: short",                                  # 3 under min_reason -> bare
        "d = 0  # zero-ok — a dash-led reason is fine",        # 4 justified (em dash stripped)
        "e = 0  # zero-okay this is another marker entirely",       # 5 word boundary: not a waiver
        "f = 0",                                                     # 6 no waiver
    ])
    good, bare = site_lint.waived_lines(text, "zero-ok")
    assert good == {1, 4}
    assert [ln for ln, _ in bare] == [2, 3]
    assert bare[0][1] == "b = 0  # zero-ok"


def test_waived_lines_min_reason_is_the_callers() -> None:
    good, bare = site_lint.waived_lines("x  # null-ok: abc", "null-ok", min_reason=3)
    assert (good, bare) == ({1}, [])
    good, bare = site_lint.waived_lines("x  # null-ok: abc", "null-ok")
    assert good == set() and [ln for ln, _ in bare] == [1]


def test_waived_lines_escapes_the_marker() -> None:
    # a marker with regex metacharacters must match literally, never as a pattern
    good, _ = site_lint.waived_lines("x  # a.b: a long enough reason", "a+b")
    assert good == set()
    good, _ = site_lint.waived_lines("x  # a+b: a long enough reason", "a+b")
    assert good == {1}


# --- repo_on_path -----------------------------------------------------------------------------

def test_repo_on_path_inserts_only_when_the_marker_exists(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    root = tmp_path / "checkout"
    root.mkdir()
    site_lint.repo_on_path(root, "pkg/registry.py")
    assert str(root) not in sys.path                      # fixture repo: no marker, no insert
    write(root / "pkg/registry.py", "X = 1\n")
    site_lint.repo_on_path(root, "pkg/registry.py")
    assert sys.path[0] == str(root)
    site_lint.repo_on_path(root, "pkg/registry.py")
    assert sys.path.count(str(root)) == 1                 # idempotent
