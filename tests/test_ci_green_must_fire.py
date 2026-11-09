"""ci-green must fire: a diverged branch -> 1, every forward or no-op case -> 0, cannot look -> 2.

Real repos: a bare `origin` (HEAD = main) and a clone the tool runs inside, the way the reusable
workflow runs it inside the caller's checkout.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from conftest import git, run_tool


@pytest.fixture
def pair(tmp_path: Path, new_repo):
    """(clone, origin): origin is bare with main at c1; the clone has c1 and c2 (c2 pushed to main)."""
    seed = new_repo("seed")
    git(seed, "commit", "-q", "--allow-empty", "-m", "c1")
    origin = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    git(clone, "config", "user.email", "t@example.com")
    git(clone, "config", "user.name", "t")
    git(clone, "commit", "-q", "--allow-empty", "-m", "c2")
    git(clone, "push", "-q", "origin", "HEAD:main")
    return clone, origin


def sha(repo: Path, rev: str = "HEAD") -> str:
    return git(repo, "rev-parse", rev).stdout.strip()


def green(origin: Path) -> str:
    return git(origin, "rev-parse", "-q", "--verify", "refs/heads/ci-green").stdout.strip()


def test_absent_branch_is_created_at_the_sha(pair):
    clone, origin = pair
    r = run_tool("ci_green", clone, "--sha", sha(clone))
    assert r.returncode == 0, r.stderr
    assert green(origin) == sha(clone)
    assert "created" in r.stdout


def test_ancestor_branch_is_fast_forwarded(pair):
    clone, origin = pair
    git(clone, "push", "-q", "origin", f"{sha(clone, 'HEAD~1')}:refs/heads/ci-green")
    r = run_tool("ci_green", clone, "--sha", sha(clone))
    assert r.returncode == 0, r.stderr
    assert green(origin) == sha(clone)
    assert "advanced" in r.stdout


def test_older_sha_finishing_late_never_moves_it_back(pair):
    clone, origin = pair
    git(clone, "push", "-q", "origin", f"{sha(clone)}:refs/heads/ci-green")
    r = run_tool("ci_green", clone, "--sha", sha(clone, "HEAD~1"))
    assert r.returncode == 0, r.stderr
    assert green(origin) == sha(clone), "an older green run moved ci-green backwards"
    assert "older" in r.stdout


def test_current_is_a_no_op(pair):
    clone, origin = pair
    git(clone, "push", "-q", "origin", f"{sha(clone)}:refs/heads/ci-green")
    r = run_tool("ci_green", clone, "--sha", sha(clone))
    assert (r.returncode, "current" in r.stdout) == (0, True), r.stderr


def test_diverged_branch_is_a_finding_and_is_never_forced(pair):
    clone, origin = pair
    git(clone, "checkout", "-q", "-b", "side", "HEAD~1")
    git(clone, "commit", "-q", "--allow-empty", "-m", "hand-pushed")
    side = sha(clone)
    git(clone, "push", "-q", "origin", "side:refs/heads/ci-green")
    git(clone, "checkout", "-q", "main")
    r = run_tool("ci_green", clone, "--sha", sha(clone, "main"))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "DIVERGED" in r.stderr
    assert green(origin) == side, "a diverged ci-green was overwritten"


def test_dry_run_pushes_nothing(pair):
    clone, origin = pair
    r = run_tool("ci_green", clone, "--sha", sha(clone), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert green(origin) == ""


@pytest.mark.parametrize("branch", ["main", "master", "refs/heads/x"])
def test_refuses_to_move_a_merge_branch(pair, branch):
    clone, origin = pair
    before = sha(Path(origin), "main")
    r = run_tool("ci_green", clone, "--sha", sha(clone, "HEAD~1"), "--branch", branch)
    assert r.returncode == 2, r.stdout + r.stderr
    assert sha(Path(origin), "main") == before


def test_refuses_the_remotes_default_branch_by_name(pair):
    clone, origin = pair
    git(origin, "symbolic-ref", "HEAD", "refs/heads/trunk")
    git(clone, "push", "-q", "origin", "HEAD:refs/heads/trunk")
    r = run_tool("ci_green", clone, "--sha", sha(clone), "--branch", "trunk")
    assert r.returncode == 2, r.stdout + r.stderr


def test_blind_on_a_sha_not_in_the_checkout(pair):
    clone, _ = pair
    r = run_tool("ci_green", clone, "--sha", "0" * 40)
    assert r.returncode == 2, r.stdout + r.stderr


def test_blind_on_a_short_sha(pair):
    clone, _ = pair
    r = run_tool("ci_green", clone, "--sha", sha(clone)[:12])
    assert r.returncode == 2


def test_blind_when_the_remote_is_unreachable(pair, tmp_path):
    clone, _ = pair
    git(clone, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    r = run_tool("ci_green", clone, "--sha", sha(clone))
    assert r.returncode == 2, r.stdout + r.stderr


def test_a_commit_never_on_main_is_refused(pair):
    """Second layer behind the workflow guard: a run's head_branch is a name, main's history is fact."""
    clone, origin = pair
    git(clone, "push", "-q", "origin", f"{sha(clone)}:refs/heads/ci-green")
    git(clone, "checkout", "-q", "-b", "feature")
    git(clone, "commit", "-q", "--allow-empty", "-m", "never merged")
    git(clone, "push", "-q", "origin", "feature")
    r = run_tool("ci_green", clone, "--sha", sha(clone))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "NOT ON main" in r.stderr
    assert green(origin) == sha(clone, "main"), "an unmerged commit was published as green"


def test_a_detached_checkout_of_the_sha_advances(pair):
    """The workflow checks out head_sha detached (actions/checkout ref: <sha>)."""
    clone, origin = pair
    git(clone, "push", "-q", "origin", f"{sha(clone, 'HEAD~1')}:refs/heads/ci-green")
    git(clone, "checkout", "-q", "--detach", sha(clone))
    r = run_tool("ci_green", clone, "--sha", sha(clone))
    assert r.returncode == 0, r.stderr
    assert green(origin) == sha(clone)


def test_a_push_the_remote_refuses_is_a_finding(pair):
    clone, origin = pair
    git(clone, "push", "-q", "origin", f"{sha(clone, 'HEAD~1')}:refs/heads/ci-green")
    hook = Path(origin) / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho refused-by-test >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    r = run_tool("ci_green", clone, "--sha", sha(clone))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "REFUSED" in r.stderr
    assert green(origin) == sha(clone, "HEAD~1")
