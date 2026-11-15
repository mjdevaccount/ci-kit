"""The shared applier never writes THROUGH a symlink (ci_kit/pr_apply.unsafe_destination) -- both directions.

A symlink in the checkout is data the PR controls. Written through, a lockfile path that is a link
lands its bytes elsewhere (inside `.git/`, outside the repository) in the job that holds the push
token. MUST FIRE, as a RED `refused` (never `nothing`): a tracked link at the path, a link at a
parent directory, a link leaving the repository, a link into `.git/` -- on both callers of the
applier (pr-heal's regen push and the relock push). MUST NOT FIRE: a plain file.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from test_heal_regen_must_fire import commit, g

from ci_kit import pr_apply, pr_relock


def _can_link(tmp: Path) -> bool:
    try:
        os.symlink(tmp / "nowhere", tmp / "probe")
        return True
    except (OSError, NotImplementedError):
        return False


@pytest.fixture
def linked(tmp_path: Path) -> tuple[Path, str, str]:
    """(work repo on branch renovate/x, base, head): head tracks `uv.lock` as a symlink to .git/config,
    `sub` as a symlink to the directory `real/`, and `out` as a symlink leaving the repository."""
    if not _can_link(tmp_path):
        pytest.skip("this platform will not create a symlink for an unprivileged user (Linux CI runs it)")
    d = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True)
    base = commit(d, {"pyproject.toml": "x\n", "real/keep": "k\n"}, "init")
    g(d, "checkout", "-qb", "renovate/x")
    os.symlink(".git/config", d / "uv.lock")
    os.symlink("real", d / "sub", target_is_directory=True)
    os.symlink(str(tmp_path / "outside.lock"), d / "out")
    head = commit(d, {}, "plant links")
    return d, base, head


def _artifact(tmp: Path, path: str, base: str, head: str) -> Path:
    art = tmp / "art"
    (art / "files" / path).parent.mkdir(parents=True, exist_ok=True)
    (art / "files" / path).write_text("[core]\n\tfsmonitor = probe\n")
    (art / "manifest.json").write_text(json.dumps({"tier": "regen", "base": base, "head": head, "merged": False,
                                                   "conflicted": [], "files": {path: "write"}}))
    return art


def test_a_plain_file_is_a_safe_destination_must_not_fire(tmp_path) -> None:
    d = tmp_path / "w"
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    commit(d, {"uv.lock": "x\n", "a/uv.lock": "y\n"}, "init")
    assert pr_apply.unsafe_destination(d, "uv.lock") == "" and pr_apply.unsafe_destination(d, "a/uv.lock") == ""
    assert pr_apply.unsafe_destination(d, "new/uv.lock") == ""          # a file the tier creates


def test_each_link_shape_is_refused_must_fire(linked) -> None:
    d, _, _ = linked
    assert "tracked as a symlink" in pr_apply.unsafe_destination(d, "uv.lock")
    assert "sub is a symlink" in pr_apply.unsafe_destination(d, "sub/uv.lock")
    assert "tracked as a symlink" in pr_apply.unsafe_destination(d, "out")


def test_the_walk_catches_an_untracked_link_the_index_does_not_know_must_fire(linked) -> None:
    d, _, _ = linked
    os.symlink(".git", d / "gitdir")
    assert "gitdir is a symlink" in pr_apply.unsafe_destination(d, "gitdir/config")


def test_the_resolved_location_is_checked_even_if_no_link_is_seen_must_fire(linked, monkeypatch) -> None:
    """The last layer: a link the walk cannot see (a Windows junction, say) still resolves somewhere."""
    d, _, _ = linked
    monkeypatch.setattr(pr_apply.os.path, "islink", lambda _p: False)
    os.symlink(str(d.parent / "elsewhere"), d / "farlink")
    assert "resolves outside the repository" in pr_apply.unsafe_destination(d, "farlink")
    os.symlink(".git", d / "gitlink")
    assert "resolves inside .git/" in pr_apply.unsafe_destination(d, "gitlink/config")
    assert "resolves inside .git/" in pr_apply.unsafe_destination(d, ".git/config")


def test_the_heal_push_refuses_a_linked_destination_red_must_fire(linked, tmp_path) -> None:
    d, base, head = linked
    before = (d / ".git" / "config").read_text()
    art = _artifact(tmp_path, "uv.lock", base, head)
    result, why = pr_apply.apply(d, art, head=head, base=base, ref="renovate/x", regen_globs=[], max_lines=300)
    assert result == "refused" and why.startswith("destination symlink:"), why
    assert (d / ".git" / "config").read_text() == before
    assert g(d, "rev-parse", "HEAD") == head                             # nothing committed


def test_the_relock_push_refuses_a_linked_destination_red_must_fire(linked, tmp_path) -> None:
    d, base, head = linked
    before = (d / ".git" / "config").read_text()
    art = _artifact(tmp_path, "uv.lock", base, head)
    result, why, sha = pr_relock.relock_push(d, art, paths=["uv.lock"], head=head, base=base, ref="renovate/x", repo="o/r")
    assert result == "refused" and "destination symlink" in why and not sha, why
    assert (d / ".git" / "config").read_text() == before
