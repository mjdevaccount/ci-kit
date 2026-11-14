"""The heal ladder's tiers 1 and 2, and its ONE pusher, on throwaway REAL git repos -- both directions.

MUST FIRE: two PRs that each add a source both regenerate `gen.txt` and conflict there; tier 2 takes
main's side, reruns the generator on the merged tree and pushes ONE merge commit (two parents, never
forced) whose gen.txt holds both sides. A stale generated file on a red head is regenerated as one
fix commit. A lockfile-only conflict is relocked by the real tool (uv / npm / dotnet when present).
MUST NOT FIRE: a conflict in a hand-written file is `uncovered` (tier 3's); a generator that writes
outside the map is `strayed`; a shrink-only baseline that would GROW is refused; a head that moved
is never pushed over; a lockfile whose manifest also conflicts is not "lockfile-only".
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ci_kit import pr_apply, pr_regen

PY = sys.executable.replace("\\", "/")
GEN = "import pathlib\nsrc = sorted(p.read_text().strip() for p in pathlib.Path('src').glob('*.txt'))\n" \
      "pathlib.Path('gen.txt').write_text('\\n'.join(src) + '\\n')\n"
MAP = json.dumps([{"paths": ["gen.txt"], "run": f'"{PY}" gen.py', "on": ["conflict", "red"]}])


def g(d: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True,
                          capture_output=True, text=True).stdout.strip()


def commit(d: Path, files: dict[str, str], msg: str) -> str:
    for k, v in files.items():
        (d / k).parent.mkdir(parents=True, exist_ok=True)
        (d / k).write_text(v, encoding="utf-8", newline="\n")
    g(d, "add", "-A")
    g(d, "commit", "-qm", msg)
    return g(d, "rev-parse", "HEAD")


@pytest.fixture
def origin(tmp_path: Path) -> tuple[Path, Path]:
    """A bare origin and a work clone with main = gen.py + src/a.txt + its gen.txt."""
    bare, work = tmp_path / "origin.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True, capture_output=True)
    g(work, "checkout", "-qb", "main")
    commit(work, {"gen.py": GEN, "src/a.txt": "a", "gen.txt": "a\n", "hand.md": "x\n"}, "init")
    g(work, "push", "-q", "origin", "main")
    return bare, work


def branch_pair(work: Path, main_files: dict[str, str], pr_files: dict[str, str]) -> tuple[str, str]:
    """main advances with main_files, branch `pr` (from the old main) with pr_files. (base sha, head sha)."""
    g(work, "checkout", "-qb", "pr")
    head = commit(work, pr_files, "pr change")
    g(work, "push", "-q", "origin", "pr")
    g(work, "checkout", "-q", "main")
    base = commit(work, main_files, "main change")
    g(work, "push", "-q", "origin", "main")
    return base, head


def heal_clone(bare: Path, tmp: Path, name: str, head: str) -> Path:
    d = tmp / name
    subprocess.run(["git", "clone", "-q", str(bare), str(d)], check=True, capture_output=True)
    g(d, "checkout", "-q", head)
    return d


def run_regen(d: Path, mode: str, base: str, regen_map: str = MAP, out: Path | None = None) -> tuple[pr_regen.Prepared, str]:
    p = pr_regen.prepare(d, mode, base, pr_regen.parse_map(regen_map))
    if p.state not in ("regen", "merge"):
        return p, p.state
    failed = pr_regen.run(d, p.commands)
    return p, pr_regen.collect(d, p, out or d.parent / f"{d.name}-art", failed)


def push_clone(bare: Path, tmp: Path, head: str, art: Path, regen_map: str = MAP, **kw) -> tuple[str, str, Path]:
    d = heal_clone(bare, tmp, "push", head)
    g(d, "checkout", "-qB", "pr", head)
    globs = [x for e in pr_regen.parse_map(regen_map) for x in (*e.paths, *e.touches)]
    result, why = pr_apply.apply(d, art, head=head, base=_base(art), ref="pr", regen_globs=globs, max_lines=kw.get("max_lines", 300))
    if result == "ready":
        ok, said = _push_local(d, bare)
        result, why = ("pushed", why) if ok else ("moved", said)
    return result, why, d


def _base(art: Path) -> str:
    return json.loads((art / "manifest.json").read_text())["base"]


def _push_local(d: Path, bare: Path) -> tuple[bool, str]:
    r = subprocess.run(["git", "-C", str(d), "push", str(bare), "HEAD:refs/heads/pr"], capture_output=True, text=True)
    return r.returncode == 0, r.stderr


# ── tier 2: a conflict confined to a generated file ───────────────────────────────────────────
def test_a_generated_file_conflict_is_regenerated_and_pushed_as_one_merge_commit_must_fire(origin, tmp_path) -> None:
    bare, work = origin
    base, head = branch_pair(work, {"src/b.txt": "b", "gen.txt": "a\nb\n"}, {"src/c.txt": "c", "gen.txt": "a\nc\n"})
    d = heal_clone(bare, tmp_path, "regen", head)
    art = tmp_path / "art"
    p, state = run_regen(d, "conflict", base, out=art)
    assert p.conflicted == ["gen.txt"] and state == "regen"
    assert (art / "files" / "gen.txt").read_text() == "a\nb\nc\n"
    result, why, pushed = push_clone(bare, tmp_path, head, art)
    assert result == "pushed", why
    tip = g(bare, "rev-parse", "pr")
    parents = g(bare, "rev-list", "--parents", "-n", "1", tip).split()[1:]
    assert parents == [head, base]                                  # a merge commit: never a rebase
    assert g(bare, "show", f"{tip}:gen.txt") == "a\nb\nc"


def test_a_hand_file_conflict_is_uncovered_tier_3s_must_not_fire(origin, tmp_path) -> None:
    bare, work = origin
    base, head = branch_pair(work, {"hand.md": "main\n"}, {"hand.md": "pr\n"})
    p, state = run_regen(heal_clone(bare, tmp_path, "regen", head), "conflict", base)
    assert state == "uncovered" and p.uncovered == ["hand.md"]


def test_a_generator_writing_outside_the_map_is_strayed_must_not_fire(origin, tmp_path) -> None:
    bare, work = origin
    base, head = branch_pair(work, {"src/b.txt": "b", "gen.txt": "a\nb\n"}, {"src/c.txt": "c", "gen.txt": "a\nc\n"})
    rogue = json.dumps([{"paths": ["gen.txt"], "run": f'"{PY}" gen.py && echo y > hand.md'}])
    _, state = run_regen(heal_clone(bare, tmp_path, "regen", head), "conflict", base, rogue)
    assert state == "strayed"


def test_a_failing_generator_is_failed_never_pushed(origin, tmp_path) -> None:
    bare, work = origin
    base, head = branch_pair(work, {"src/b.txt": "b", "gen.txt": "a\nb\n"}, {"src/c.txt": "c", "gen.txt": "a\nc\n"})
    _, state = run_regen(heal_clone(bare, tmp_path, "regen", head), "conflict", base,
                         json.dumps([{"paths": ["gen.txt"], "run": "exit 3"}]))
    assert state == "failed"


def test_a_shrink_only_baseline_may_shrink_never_grow(origin, tmp_path) -> None:
    bare, work = origin
    base, head = branch_pair(work, {"base.txt": "k1\nk2\n"}, {"base.txt": "k1\nk3\n"})
    grow = json.dumps([{"paths": ["base.txt"], "run": "printf 'k1\\nk9\\n' > base.txt", "shrink": True}])
    assert run_regen(heal_clone(bare, tmp_path, "grow", head), "conflict", base, grow)[1] == "strayed"
    shrink = json.dumps([{"paths": ["base.txt"], "run": "printf 'k1\\n' > base.txt", "shrink": True}])
    assert run_regen(heal_clone(bare, tmp_path, "shrink", head), "conflict", base, shrink)[1] == "regen"
    assert pr_regen.shrink_faults("b", "k1\nk9\n", "k1\nk2\n", "k1\nk3\n") and \
        not pr_regen.shrink_faults("b", "k3\n", "k1\nk2\n", "k1\nk3\n")


# ── tier 2: a red confined to a stale generated file ──────────────────────────────────────────
def test_a_stale_generated_file_on_a_red_head_is_one_fix_commit_then_quiet(origin, tmp_path) -> None:
    bare, work = origin
    g(work, "checkout", "-qb", "pr")
    head = commit(work, {"src/c.txt": "c"}, "pr forgot to regenerate")        # gen.txt stale: the red
    g(work, "push", "-q", "origin", "pr")
    base = g(work, "rev-parse", "main")
    art = tmp_path / "art"
    p, state = run_regen(heal_clone(bare, tmp_path, "regen", head), "red", base, out=art)
    assert state == "regen" and not p.merged
    result, why, _ = push_clone(bare, tmp_path, head, art)
    assert result == "pushed", why
    tip = g(bare, "rev-parse", "pr")
    assert g(bare, "rev-list", "--parents", "-n", "1", tip).split()[1:] == [head]   # a fix commit
    again = run_regen(heal_clone(bare, tmp_path, "again", tip), "red", base)[1]
    assert again == "nothing"                                         # idempotent: no loop


def test_a_red_with_nothing_to_regenerate_is_nothing_never_a_bare_merge(origin, tmp_path) -> None:
    bare, work = origin
    base, head = branch_pair(work, {"hand.md": "main\n"}, {"src/c.txt": "c", "gen.txt": "a\nc\n"})
    assert run_regen(heal_clone(bare, tmp_path, "regen", head), "red", base)[1] == "nothing"


# ── the pusher ─────────────────────────────────────────────────────────────────────────────────
def test_a_moved_head_is_never_pushed_over_must_not_fire(origin, tmp_path) -> None:
    bare, work = origin
    base, head = branch_pair(work, {"src/b.txt": "b", "gen.txt": "a\nb\n"}, {"src/c.txt": "c", "gen.txt": "a\nc\n"})
    art = tmp_path / "art"
    run_regen(heal_clone(bare, tmp_path, "regen", head), "conflict", base, out=art)
    g(work, "checkout", "-q", "pr")
    moved = commit(work, {"src/d.txt": "d"}, "the author pushed")
    g(work, "push", "-q", "origin", "pr")
    d = heal_clone(bare, tmp_path, "push", moved)
    assert pr_apply.apply(d, art, head=head, base=base, ref="pr", regen_globs=["gen.txt"], max_lines=300)[0] == "moved"
    # and a remote that moved AFTER the check refuses the fast-forward push: nothing is forced
    d2 = heal_clone(bare, tmp_path, "push2", head)
    g(d2, "checkout", "-qB", "pr", head)
    assert pr_apply.apply(d2, art, head=head, base=base, ref="pr", regen_globs=["gen.txt"], max_lines=300)[0] == "ready"
    ok, said = _push_local(d2, bare)
    assert not ok and g(bare, "rev-parse", "pr") == moved


def test_the_pusher_redoes_the_merge_and_refuses_an_unmapped_file(origin, tmp_path) -> None:
    bare, work = origin
    base, head = branch_pair(work, {"src/b.txt": "b", "gen.txt": "a\nb\n"}, {"src/c.txt": "c", "gen.txt": "a\nc\n"})
    art = tmp_path / "art"
    run_regen(heal_clone(bare, tmp_path, "regen", head), "conflict", base, out=art)
    m = json.loads((art / "manifest.json").read_text())
    m["files"]["hand.md"] = "write"                                   # a tampered artifact
    (art / "files" / "hand.md").write_text("owned\n")
    (art / "manifest.json").write_text(json.dumps(m))
    result, why, _ = push_clone(bare, tmp_path, head, art)
    assert result == "refused" and "hand.md" in why


# ── tier 1: lockfiles ──────────────────────────────────────────────────────────────────────────
def test_lockfiles_map_to_their_own_relock_in_their_own_directory() -> None:
    cov, loose = pr_regen.cover(["uv.lock", "web/package-lock.json", "src/App/packages.lock.json", "pnpm-lock.yaml"], [])
    assert not loose
    assert cov["uv.lock"].run == "uv lock" and cov["uv.lock"].cwd == ""
    assert cov["web/package-lock.json"].run.startswith("npm install --package-lock-only") and \
        cov["web/package-lock.json"].cwd == "web"
    assert cov["src/App/packages.lock.json"].run == "dotnet restore --force-evaluate" and \
        cov["src/App/packages.lock.json"].tool == "dotnet"


def test_a_lockfile_whose_manifest_also_conflicts_is_not_lockfile_only_must_not_fire() -> None:
    cov, loose = pr_regen.cover(["uv.lock", "pyproject.toml"], [])
    assert sorted(loose) == ["pyproject.toml", "uv.lock"] and not cov
    cov, loose = pr_regen.cover(["src/App/packages.lock.json", "src/App/App.csproj"], [])
    assert len(loose) == 2
    cov, loose = pr_regen.cover(["a/uv.lock", "b/pyproject.toml"], [])   # another project's manifest
    assert "a/uv.lock" in cov


def _lock_conflict(origin, tmp_path, files: dict[str, str], lock: str) -> tuple[str, Path]:
    bare, work = origin
    g(work, "checkout", "-q", "main")
    commit(work, files, "project")
    g(work, "push", "-q", "origin", "main")
    base, head = branch_pair(work, {lock: files[lock] + "\n"}, {lock: files[lock] + "\n\n"})
    return run_regen(heal_clone(bare, tmp_path, "regen", head), "conflict", base, "")[1], tmp_path / "regen"


@pytest.mark.skipif(not shutil.which("uv"), reason="uv not installed")
def test_a_uv_lock_conflict_is_relocked_by_real_uv_must_fire(origin, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("UV_OFFLINE", "1")
    py = '[project]\nname = "p"\nversion = "0.1.0"\nrequires-python = ">=3.11"\ndependencies = []\n'
    d = tmp_path / "seed"
    d.mkdir()
    (d / "pyproject.toml").write_text(py)
    subprocess.run(["uv", "lock"], cwd=d, check=True, capture_output=True)
    state, clone = _lock_conflict(origin, tmp_path, {"pyproject.toml": py, "uv.lock": (d / "uv.lock").read_text()}, "uv.lock")
    assert state == "regen"
    subprocess.run(["uv", "lock", "--check"], cwd=clone, check=True, capture_output=True)


@pytest.mark.skipif(not shutil.which("npm"), reason="npm not installed")
def test_a_package_lock_conflict_is_relocked_by_real_npm_must_fire(origin, tmp_path) -> None:
    pkg = '{"name": "p", "version": "1.0.0"}\n'
    lock = json.dumps({"name": "p", "version": "1.0.0", "lockfileVersion": 3, "requires": True,
                       "packages": {"": {"name": "p", "version": "1.0.0"}}}, indent=2)
    state, clone = _lock_conflict(origin, tmp_path, {"package.json": pkg, "package-lock.json": lock}, "package-lock.json")
    assert state == "regen"
    assert json.loads((clone / "package-lock.json").read_text())["lockfileVersion"] >= 2


@pytest.mark.skipif(not shutil.which("dotnet"), reason="dotnet not installed")
def test_a_nuget_lock_conflict_is_relocked_by_real_dotnet_must_fire(origin, tmp_path) -> None:
    proj = ('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net10.0</TargetFramework>'
            '<RestorePackagesWithLockFile>true</RestorePackagesWithLockFile></PropertyGroup></Project>\n')
    d = tmp_path / "seed"
    d.mkdir()
    (d / "App.csproj").write_text(proj)
    r = subprocess.run(["dotnet", "restore", "--force-evaluate"], cwd=d, capture_output=True, text=True)
    if r.returncode or not (d / "packages.lock.json").exists():
        pytest.skip(f"dotnet restore unavailable here: {r.stdout[-200:]}")
    state, clone = _lock_conflict(origin, tmp_path, {"App.csproj": proj, ".gitignore": "obj/\nbin/\n",
                                                    "packages.lock.json": (d / "packages.lock.json").read_text()},
                                  "packages.lock.json")
    assert state == "regen"
    assert json.loads((clone / "packages.lock.json").read_text())["version"] >= 1


# ── the map ────────────────────────────────────────────────────────────────────────────────────
def test_a_malformed_map_is_an_error_never_an_empty_one() -> None:
    assert pr_regen.parse_map("") == []
    for bad in ('{"paths": "x"}', '[{"paths": ["x"]}]', '[{"run": "x"}]', '[{"paths": ["x"], "run": "y", "on": "always"}]',
                '[{"paths": ["x"], "run": "y", "typo": 1}]'):
        with pytest.raises(ValueError):
            pr_regen.parse_map(bad)


def test_globs() -> None:
    m = pr_regen.glob_match
    assert m("docs/**", "docs/a/b.md") and m("**/test_*.py", "test_x.py") and m("**/test_*.py", "a/b/test_x.py")
    assert not m("docs/*.md", "docs/a/b.md") and not m("gen.txt", "sub/gen.txt")


# ── review 2026-11-14: the pusher's own inputs, red mode on a covered conflict ──────────────────
def test_the_pusher_trusts_the_plans_base_never_the_artifacts_must_not_fire(origin, tmp_path) -> None:
    bare, work = origin
    base, head = branch_pair(work, {"src/b.txt": "b", "gen.txt": "a\nb\n"}, {"src/c.txt": "c", "gen.txt": "a\nc\n"})
    art = tmp_path / "art"
    run_regen(heal_clone(bare, tmp_path, "regen", head), "conflict", base, out=art)
    d = heal_clone(bare, tmp_path, "push", head)
    g(d, "checkout", "-qB", "pr", head)
    other = g(d, "rev-parse", "origin/main~1")
    assert pr_apply.apply(d, art, head=head, base=other, ref="pr", regen_globs=["gen.txt"], max_lines=300)[0] == "refused"
    m = json.loads((art / "manifest.json").read_text())
    m["files"]["gen.txt"] = "w"                                        # an unknown `how` skips the line count
    (art / "manifest.json").write_text(json.dumps(m))
    result, why = pr_apply.apply(d, art, head=head, base=base, ref="pr", regen_globs=["gen.txt"], max_lines=300)
    assert result == "refused" and "neither write nor delete" in why


def test_only_a_moved_branch_is_moved_any_other_refusal_pages() -> None:
    assert pr_apply.pushed_over("! [rejected] HEAD -> pr (non-fast-forward)") == "moved"
    assert pr_apply.pushed_over("! [rejected] HEAD -> pr (fetch first)") == "moved"
    assert pr_apply.pushed_over("! [remote rejected] HEAD -> pr (push declined due to repository rule violations)") \
        == "failed"
    assert pr_apply.pushed_over("! [remote rejected] HEAD -> pr (protected branch hook declined)") == "failed"


def test_red_mode_regenerates_a_covered_conflict_never_keeps_mains_side_alone(origin, tmp_path) -> None:
    bare, work = origin
    conflict_only = json.dumps([{"paths": ["gen.txt"], "run": f'"{PY}" gen.py', "on": ["conflict"]}])
    base, head = branch_pair(work, {"src/b.txt": "b", "gen.txt": "a\nb\n"}, {"src/c.txt": "c", "gen.txt": "a\nc\n"})
    art = tmp_path / "art"
    p, state = run_regen(heal_clone(bare, tmp_path, "regen", head), "red", base, conflict_only, out=art)
    assert p.commands and state == "regen"
    assert (art / "files" / "gen.txt").read_text() == "a\nb\nc\n"      # both sides, regenerated


def test_the_install_is_not_the_generators_change(origin, tmp_path) -> None:
    bare, work = origin
    base, head = branch_pair(work, {"src/b.txt": "b", "gen.txt": "a\nb\n"}, {"src/c.txt": "c", "gen.txt": "a\nc\n"})
    d = heal_clone(bare, tmp_path, "regen", head)
    state_file = tmp_path / "state.json"
    old = Path.cwd()
    os.chdir(d)
    try:
        os.environ["CI_KIT_REGEN_MAP"] = MAP
        assert pr_regen.main(["prepare", "--mode", "conflict", "--base", base, "--state-file", str(state_file)]) == 0
        (d / "hand.md").write_text("an install rewrote me\n")             # the caller's install, before `run`
        assert pr_regen.main(["run", "--state-file", str(state_file)]) == 0
        p = pr_regen.Prepared(**json.loads(state_file.read_text()))
        assert pr_regen.collect(d, p, tmp_path / "art") == "regen"       # not `strayed`
    finally:
        os.chdir(old)
        os.environ.pop("CI_KIT_REGEN_MAP", None)
