"""ci_kit/pr_relock.py on throwaway REAL git repos -- both directions.

MUST FIRE: a bot branch whose manifest moved gets ONE commit touching only the lockfile, authored
github-actions[bot], pushed fast-forward; the next run finds it current (`nothing`). The push side
REFUSES an artifact with an extra file, a symlink, a path outside the input's set (traversal
included), a delete, a merge claim; a lockfile the head does not track; a non-pull_request event,
a fork, a person's branch, an empty prefix; a relock needed on top of our own relock (no loop).
A lock tool that writes anything else is `strayed` and red; one that fails is `failed` and red.
MUST NOT FIRE: a clean artifact for exactly the input's lockfile passes every check.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from test_heal_regen_must_fire import commit, g

from ci_kit import pr_regen, pr_relock

PY = '[project]\nname = "p"\nversion = "{v}"\nrequires-python = ">=3.11"\ndependencies = []\n'
REF = "renovate/p-0.2"


def _gate(**kw: str) -> str:
    a = {"event": "pull_request", "head_repo": "o/r", "repo": "o/r", "head_ref": REF, "prefix": "renovate/"} | kw
    return pr_relock.refusal(a["event"], a["head_repo"], a["repo"], a["head_ref"], a["prefix"])


# ── the pure policy ─────────────────────────────────────────────────────────────────────────────
def test_the_gate_admits_only_a_same_repo_bot_branch_on_pull_request() -> None:
    assert _gate() == ""
    assert "pull_request caller" in _gate(event="pull_request_target")
    assert "pull_request caller" in _gate(event="push")
    assert "fork" in _gate(head_repo="evil/r") and "fork" in _gate(head_repo="")
    assert "dependency-bot branch" in _gate(head_ref="feature/x")
    assert "empty" in _gate(prefix="") and "empty" in _gate(prefix="  ")
    assert "whitespace" in _gate(prefix=" renovate/") and "whitespace" in _gate(prefix="renovate/\t")
    assert _gate(head_ref="Renovate/p-0.2") == ""            # case-insensitive, as the job's startsWith()


def test_the_lockfiles_input_is_plain_lockfile_paths_or_red() -> None:
    assert pr_relock.parse_lockfiles("uv.lock") == ["uv.lock"]
    assert pr_relock.parse_lockfiles("./uv.lock\nweb/package-lock.json uv.lock") == ["uv.lock", "web/package-lock.json"]
    for bad in ("", "  ", "../uv.lock", "a/../uv.lock", "/uv.lock", "a//uv.lock", "a/./uv.lock", "C:/uv.lock",
                "a\\uv.lock", "*/uv.lock", "pyproject.toml", "uv.lock.bak"):
        with pytest.raises(ValueError):
            pr_relock.parse_lockfiles(bad)


# ── the artifact check (push side) ──────────────────────────────────────────────────────────────
def _art(tmp: Path, files: dict[str, str] | None = None, **manifest) -> Path:
    art = tmp / "art"
    files = {"uv.lock": "write"} if files is None else files
    for k, v in files.items():
        if v == "write":
            p = art / "files" / k
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("lock\n")
    art.mkdir(exist_ok=True)
    m = {"tier": "regen", "base": "b", "head": "h", "merged": False, "conflicted": [], "files": files} | manifest
    (art / "manifest.json").write_text(json.dumps(m))
    return art


def test_a_clean_relock_artifact_passes_must_not_fire(tmp_path) -> None:
    assert pr_relock.artifact_faults(_art(tmp_path), ["uv.lock"]) == []


@pytest.mark.parametrize("files,why", [
    ({"../uv.lock": "write"}, "not a lockfile this relock may write"),
    ({"sub/uv.lock": "write"}, "not a lockfile this relock may write"),
    ({"uv.lock": "write", "src/app.py": "write"}, "not a lockfile this relock may write"),
    ({"uv.lock": "delete"}, "only rewrites"),
    ({}, "names no file"),
])
def test_a_manifest_outside_the_input_is_refused_must_fire(tmp_path, files, why) -> None:
    got = pr_relock.artifact_faults(_art(tmp_path, files), ["uv.lock"])
    assert any(why in f for f in got), got


def test_a_merge_claim_an_extra_file_or_a_missing_file_is_refused_must_fire(tmp_path) -> None:
    assert any("merge" in f for f in pr_relock.artifact_faults(_art(tmp_path / "a", merged=True), ["uv.lock"]))
    assert any("merge" in f for f in pr_relock.artifact_faults(_art(tmp_path / "b", conflicted=["uv.lock"]), ["uv.lock"]))
    art = _art(tmp_path / "c")
    (art / "files" / ".hidden").write_text("x")
    (art / "payload.sh").write_text("x")
    got = pr_relock.artifact_faults(art, ["uv.lock"])
    assert "files/.hidden: an extra file" in got and "payload.sh: an extra file" in got
    art = _art(tmp_path / "d")
    (art / "files" / "uv.lock").unlink()
    assert "files/uv.lock: missing from the artifact" in pr_relock.artifact_faults(art, ["uv.lock"])
    art = tmp_path / "e"
    art.mkdir()
    assert pr_relock.artifact_faults(art, ["uv.lock"]) == ["the artifact holds no regular manifest.json"]


def test_a_symlinked_lockfile_is_refused_must_fire(tmp_path) -> None:
    art = _art(tmp_path)
    target = art / "files" / "uv.lock"
    target.unlink()
    try:
        os.symlink(tmp_path / "elsewhere", target)
    except (OSError, NotImplementedError):
        pytest.skip("this platform will not create a symlink for an unprivileged user")
    assert "files/uv.lock: a symlink" in pr_relock.artifact_faults(art, ["uv.lock"])


# ── end to end on real git (the lock tool replaced by a stand-in, then real uv) ────────────────────
@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """(bare origin, work clone, base sha, bot head sha): main holds pyproject + uv.lock, the bot
    branch bumps the version in pyproject.toml and leaves uv.lock alone."""
    bare, work = tmp_path / "origin.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True, capture_output=True)
    g(work, "checkout", "-qb", "main")
    base = commit(work, {"pyproject.toml": PY.format(v="0.1.0"), "uv.lock": "version = 0.1.0\n"}, "init")
    g(work, "push", "-q", "origin", "main")
    g(work, "checkout", "-qb", REF)
    head = commit(work, {"pyproject.toml": PY.format(v="0.2.0")}, "chore(deps): bump p")
    g(work, "push", "-q", "origin", REF)
    return bare, work, base, head


def _clone(bare: Path, tmp: Path, name: str, ref: str) -> Path:
    d = tmp / name
    subprocess.run(["git", "clone", "-q", str(bare), str(d)], check=True, capture_output=True)
    g(d, "checkout", "-qB", REF, f"origin/{REF}") if ref == REF else g(d, "checkout", "-q", ref)
    return d


def _lock(d: Path, base: str, out: Path, run: str = "") -> str:
    """The lock job: prepare, the lock tool (or `run` standing in for it), collect."""
    p = pr_relock.prepare(d, ["uv.lock"], base)
    cmds = [{**c, "run": run} for c in p.commands] if run else p.commands
    return pr_regen.collect(d, p, out, pr_regen.run(d, cmds))


STAND_IN = "sed -n 's/^version = \"\\(.*\\)\"$/version = \\1/p' pyproject.toml > uv.lock"


def _push(bare: Path, tmp: Path, name: str, art: Path, head: str, base: str, monkeypatch) -> tuple[str, str, str]:
    monkeypatch.setenv("CI_KIT_PUSH_URL", str(bare))
    monkeypatch.setenv("CI_KIT_PUSH_TOKEN", "unused-for-a-local-remote")
    d = _clone(bare, tmp, name, REF)
    return pr_relock.relock_push(d, art, paths=["uv.lock"], head=head, base=base, ref=REF, repo="o/r")


def test_a_stale_lock_is_one_lockfile_commit_then_quiet_must_fire(repo, tmp_path, monkeypatch) -> None:
    bare, _, base, head = repo
    art = tmp_path / "art"
    assert _lock(_clone(bare, tmp_path, "lock", head), base, art, STAND_IN) == "regen"
    result, why, sha = _push(bare, tmp_path, "push", art, head, base, monkeypatch)
    assert result == "pushed", why
    assert g(bare, "rev-parse", REF) == sha
    assert g(bare, "rev-list", "--parents", "-n", "1", sha).split()[1:] == [head]           # fast-forward, one commit
    assert g(bare, "diff-tree", "--no-commit-id", "--name-only", "-r", sha) == "uv.lock"
    assert g(bare, "log", "-1", "--format=%ae|%s", sha) == f"{pr_regen.BOT[1]}|{pr_relock.SUBJECT} uv.lock"
    assert g(bare, "show", f"{sha}:uv.lock") == "version = 0.2.0"
    assert _lock(_clone(bare, tmp_path, "again", sha), base, tmp_path / "art2", STAND_IN) == "nothing"


def test_a_relock_needed_on_our_own_relock_is_refused_never_a_loop_must_fire(repo, tmp_path, monkeypatch) -> None:
    bare, _, base, head = repo
    _lock(_clone(bare, tmp_path, "lock", head), base, tmp_path / "art", STAND_IN)
    sha = _push(bare, tmp_path, "push", tmp_path / "art", head, base, monkeypatch)[2]
    flaky = "date +%N >> uv.lock"                                     # a non-deterministic lock tool
    assert _lock(_clone(bare, tmp_path, "lock2", sha), base, tmp_path / "art2", flaky) == "regen"
    result, why, _ = _push(bare, tmp_path, "push2", tmp_path / "art2", sha, base, monkeypatch)
    assert result == "refused" and "not deterministic" in why
    assert g(bare, "rev-parse", REF) == sha


def test_a_lock_tool_that_writes_more_is_strayed_and_a_failure_is_failed_must_fire(repo, tmp_path) -> None:
    bare, _, base, head = repo
    assert _lock(_clone(bare, tmp_path, "a", head), base, tmp_path / "a-art", f"{STAND_IN}; echo x > evil.py") == "strayed"
    assert _lock(_clone(bare, tmp_path, "b", head), base, tmp_path / "b-art", "exit 3") == "failed"


def test_a_moved_head_is_never_pushed_over_must_fire(repo, tmp_path, monkeypatch) -> None:
    bare, work, base, head = repo
    art = tmp_path / "art"
    _lock(_clone(bare, tmp_path, "lock", head), base, art, STAND_IN)
    commit(work, {"README": "x"}, "the bot pushed again")
    g(work, "push", "-q", "origin", REF)
    result, why, _ = _push(bare, tmp_path, "push", art, head, base, monkeypatch)
    assert result == "moved", why


def test_an_untracked_lockfile_is_never_written_must_fire(repo, tmp_path) -> None:
    bare, _, base, head = repo
    art = _art(tmp_path, {"sub/uv.lock": "write"}, base=base, head=head)    # an artifact that IS clean for the path
    assert pr_relock.artifact_faults(art, ["sub/uv.lock"]) == []
    got = pr_relock.relock_push(_clone(bare, tmp_path, "push", REF), art, paths=["sub/uv.lock"], head=head, base=base,
                                ref=REF, repo="o/r")
    assert got[0] == "refused" and "not tracked at the head" in got[1], got


def test_a_relock_commit_touching_more_than_the_lockfile_is_refused_must_fire(repo, tmp_path, monkeypatch) -> None:
    bare, _, base, head = repo
    art = tmp_path / "art"
    _lock(_clone(bare, tmp_path, "lock", head), base, art, STAND_IN)
    real = pr_relock.apply

    def widened(root, *a, **kw):             # an applier that (wrongly) commits a second file
        out = real(root, *a, **kw)
        (root / "extra.txt").write_text("x")
        g(root, "add", "extra.txt")
        g(root, "commit", "-q", "--amend", "--no-edit")
        return out
    monkeypatch.setattr(pr_relock, "apply", widened)
    result, why, _ = _push(bare, tmp_path, "push", art, head, base, monkeypatch)
    assert result == "refused" and "touches" in why, why
    assert g(bare, "rev-parse", REF) == head


def test_a_loop_is_caught_in_the_lock_job_before_any_token_must_fire(repo, tmp_path, monkeypatch, capsys) -> None:
    bare, _, base, head = repo
    _lock(_clone(bare, tmp_path, "lock", head), base, tmp_path / "art", STAND_IN)
    sha = _push(bare, tmp_path, "push", tmp_path / "art", head, base, monkeypatch)[2]
    d = _clone(bare, tmp_path, "lock2", sha)
    p = pr_relock.prepare(d, ["uv.lock"], base)
    pr_regen.run(d, [{**c, "run": "date +%N >> uv.lock"} for c in p.commands])
    state = tmp_path / "s.json"
    state.write_text(json.dumps(p.__dict__))
    monkeypatch.chdir(d)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    capsys.readouterr()
    assert pr_relock.main(["collect", "--state-file", str(state), "--out", str(tmp_path / "art2")]) == 1
    out = capsys.readouterr().out
    assert "state=loop" in out and "not deterministic" in out


def test_the_cli_refuses_a_wrong_event_and_a_typo_is_red_must_fire(repo, tmp_path, monkeypatch, capsys) -> None:
    bare, _, base, head = repo
    art = tmp_path / "art"
    _lock(_clone(bare, tmp_path, "lock", head), base, art, STAND_IN)          # a VALID artifact
    d = _clone(bare, tmp_path, "cli", REF)
    monkeypatch.chdir(d)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setenv("CI_KIT_PUSH_URL", str(bare))
    monkeypatch.setenv("CI_KIT_PUSH_TOKEN", "unused-for-a-local-remote")
    common = ["--head-repo", "o/r", "--repo", "o/r", "--ref", REF, "--branch-prefix", "renovate/"]
    state = str(tmp_path / "s.json")
    capsys.readouterr()
    assert pr_relock.main(["push", "--event", "pull_request_target", *common, "--lockfiles", "uv.lock",
                           "--artifact", str(art), "--head", head, "--base", base]) == 1
    assert "relock push refused: relock needs a pull_request caller" in capsys.readouterr().out
    assert g(bare, "rev-parse", REF) == head                                  # nothing pushed
    assert pr_relock.main(["prepare", "--event", "pull_request", *common, "--lockfiles", "sub/uv.lock",
                           "--base", base, "--state-file", state]) == 2
    assert "not tracked at the head: sub/uv.lock" in capsys.readouterr().out
    assert pr_relock.main(["prepare", "--event", "push", *common, "--lockfiles", "uv.lock",
                           "--base", base, "--state-file", state]) == 2
    assert "pull_request caller" in capsys.readouterr().out
    assert pr_relock.main(["prepare", "--event", "pull_request", *common, "--lockfiles", "uv.lock",
                           "--base", base, "--state-file", state]) == 0


@pytest.mark.skipif(not shutil.which("uv"), reason="uv not installed")
def test_real_uv_relocks_a_bumped_manifest_must_fire(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("UV_OFFLINE", "1")
    bare, work = tmp_path / "origin.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True, capture_output=True)
    g(work, "checkout", "-qb", "main")
    (work / "pyproject.toml").write_text(PY.format(v="0.1.0"))
    subprocess.run(["uv", "lock"], cwd=work, check=True, capture_output=True)
    base = commit(work, {}, "init")
    g(work, "checkout", "-qb", REF)
    head = commit(work, {"pyproject.toml": PY.format(v="0.2.0")}, "bump")
    d = tmp_path / "lock"
    subprocess.run(["git", "clone", "-q", str(work), str(d)], check=True, capture_output=True)
    g(d, "checkout", "-q", head)
    assert subprocess.run(["uv", "lock", "--check"], cwd=d, capture_output=True).returncode != 0
    assert _lock(d, base, tmp_path / "art") == "regen"
    assert 'version = "0.2.0"' in (tmp_path / "art" / "files" / "uv.lock").read_text()
    assert pr_relock.artifact_faults(tmp_path / "art", ["uv.lock"]) == []
