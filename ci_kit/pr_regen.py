"""Tiers 1 and 2 of the heal ladder: a conflict or red confined to GENERATED files is fixed by regenerating them.

WHY (operator, 2026-11-14: "we can likely handle a lot of cases with no claude at all"). Most
conflicts between two agent PRs are not a disagreement. Both added a route, so `openapi.json`
conflicts; both added a dependency, so `uv.lock` conflicts. Both are fixed by running the generator
again on the merged tree. Neither needs judgment, and neither should wait for a session or for Claude.

THE MAP (the caller's `regen` input, read from the caller's MAIN copy of its workflow, never from
the PR). It is a JSON list of entries:
  {"paths": ["docs/api/openapi.json"],          wholly generated: a conflict here is RESOLVED by
                                                taking main's side and running `run`
   "touches": ["CLAUDE.md"],                    also rewritten by `run` (a cog block in a hand file):
                                                allowed in the output, never resolved by taking a side
   "run": "uv run python scripts/openapi_snapshot.py --write",
   "on": ["conflict", "red"],                   conflict (default) and/or red: a judgment red at the
                                                head regenerates these and pushes only if they moved
   "shrink": false}                             a shrink-only key baseline: the regenerated file may
                                                hold no line that neither side's copy had (it can
                                                drop an entry, never grant a new one)
TIER 1 is the same machinery with built-in entries, LOCKFILES: uv.lock, package-lock.json /
npm-shrinkwrap.json, pnpm-lock.yaml and NuGet's packages.lock.json, each relocked in its own
directory. A lockfile is resolved only when its MANIFEST merged cleanly (a pyproject.toml or
package.json conflict is a judgment, so tier 3's).

THE SNAPSHOT. `prepare` merges the base sha into the PR head with --no-commit, resolves what the
map covers, stages EVERYTHING (an uncovered conflict keeps its markers), and commits that LOCALLY
as one snapshot commit, never pushed. Whatever a generator (or tier 3) changes afterwards is
`changed()` against that snapshot. The push job (ci_kit/pr_apply.py) redoes the merge itself from
the same base sha and takes from the artifact only those files, so nothing git merged cleanly
travels through an artifact.

States (`prepare`, then `collect`): merge (GitHub's merge refused, git's own merge is clean -- a
`merge=union` attribute, or merge-ort seeing more), regen (the generators produced the fix),
nothing (red mode: the generated files were already current), uncovered (a conflict the map does
not cover), strayed (a generator wrote outside the map), failed (a generator exited nonzero).
The last four hand the PR to tier 3.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ci_kit.pin_rehash import bumped, declares, only_hashes

CONFLICT, RED = "conflict", "red"
SNAPSHOT_MSG = "heal: local merge snapshot (never pushed)"
# every heal commit's author: the identity Renovate's `gitIgnoredAuthors` (renovate/default.json5)
# already ignores, so a Renovate branch the ladder fixed is still Renovate's to rebase and merge
BOT = ("github-actions[bot]", "41898282+github-actions[bot]@users.noreply.github.com")
# a conflict marker git left (diff3 or merge style); `=======` alone is a markdown heading too
MARKER = re.compile(r"^(<{7}|>{7}|\|{7})( |$)", re.M)


@dataclass(frozen=True)
class Entry:
    paths: tuple[str, ...]
    run: str
    on: frozenset[str] = frozenset({CONFLICT})
    touches: tuple[str, ...] = ()
    shrink: bool = False
    cwd: str = ""          # '' = the repository root
    tool: str = ""         # uv | node | dotnet | '' (the caller's own install covers it)

    def owns(self, path: str) -> bool:
        return any(glob_match(g, path) for g in self.paths)

    def may_write(self, path: str) -> bool:
        return self.owns(path) or any(glob_match(g, path) for g in self.touches)


# TIER 1: basename -> (command, toolchain, the manifests whose conflict makes it a judgment)
LOCKFILES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "uv.lock": ("uv lock", "uv", ("pyproject.toml",)),
    "package-lock.json": ("npm install --package-lock-only --ignore-scripts --no-audit --no-fund", "node",
                          ("package.json",)),
    "npm-shrinkwrap.json": ("npm install --package-lock-only --ignore-scripts --no-audit --no-fund", "node",
                            ("package.json",)),
    "pnpm-lock.yaml": ("corepack pnpm install --lockfile-only --ignore-scripts", "node", ("package.json",)),
    "packages.lock.json": ("dotnet restore --force-evaluate", "dotnet", ("*.csproj", "*.fsproj", "*.vbproj",
                                                                          "Directory.Packages.props")),
}


def glob_match(pattern: str, path: str) -> bool:
    """A gitignore-ish glob: `**` crosses directories, `*` and `?` do not. Pure."""
    rx, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            rx, i = rx + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            rx, i = rx + ".*", i + 2
        elif pattern[i] == "*":
            rx, i = rx + "[^/]*", i + 1
        elif pattern[i] == "?":
            rx, i = rx + "[^/]", i + 1
        else:
            rx, i = rx + re.escape(pattern[i]), i + 1
    return re.fullmatch(rx, path) is not None


def parse_map(text: str) -> list[Entry]:
    """The caller's `regen` input -> entries. '' = none. A malformed map raises ValueError: a typo
    must turn the heal run red, never silently cover nothing."""
    if not (text or "").strip():
        return []
    raw = json.loads(text)
    if not isinstance(raw, list):
        raise ValueError("regen map: a JSON list of {paths, run, on, touches, shrink}")
    out = []
    for i, e in enumerate(raw):
        if not isinstance(e, dict) or not isinstance(e.get("run"), str) or not e["run"].strip():
            raise ValueError(f"regen map entry {i}: needs a non-empty `run`")
        paths = e.get("paths") or []
        if isinstance(paths, str):
            paths = [paths]
        on = e.get("on") or [CONFLICT]
        on = [on] if isinstance(on, str) else on
        if not paths or set(on) - {CONFLICT, RED} or set(e) - {"paths", "run", "on", "touches", "shrink"}:
            raise ValueError(f"regen map entry {i}: needs `paths`; `on` is conflict/red; keys are "
                             "paths, run, on, touches, shrink")
        touches = e.get("touches") or []
        out.append(Entry(tuple(paths), e["run"], frozenset(on), tuple([touches] if isinstance(touches, str) else touches),
                         bool(e.get("shrink"))))
    return out


def lockfile_entry(path: str) -> Entry | None:
    name = path.rsplit("/", 1)[-1]
    if name not in LOCKFILES:
        return None
    run, tool, _ = LOCKFILES[name]
    d = path.rsplit("/", 1)[0] if "/" in path else ""
    return Entry((path,), run, frozenset({CONFLICT, RED}), cwd=d, tool=tool)


def manifest_of(lock: str, path: str) -> bool:
    """`path` is a manifest the lockfile `lock` is computed from (same directory)."""
    d = lock.rsplit("/", 1)[0] if "/" in lock else ""
    pd = path.rsplit("/", 1)[0] if "/" in path else ""
    name = path.rsplit("/", 1)[-1]
    return d == pd and any(glob_match(m, name) for m in LOCKFILES[lock.rsplit("/", 1)[-1]][2])


def cover(conflicted: list[str], entries: list[Entry]) -> tuple[dict[str, Entry], list[str]]:
    """(path -> the entry that regenerates it, the paths nothing covers). Pure.
    A lockfile whose manifest ALSO conflicts is not covered: the dependency change is a judgment."""
    covered: dict[str, Entry] = {}
    loose: list[str] = []
    for p in conflicted:
        e = next((x for x in entries if CONFLICT in x.on and x.owns(p)), None) or lockfile_entry(p)
        if e is not None and e.tool and any(manifest_of(p, q) for q in conflicted if q != p):
            e = None
        if e is None:
            loose.append(p)
        else:
            covered[p] = e
    return covered, loose


def red_entries(entries: list[Entry], pr_files: list[str], tracked: list[str]) -> list[Entry]:
    """What a judgment red regenerates: the map's `red` entries, and every lockfile whose manifest
    the PR's own diff touches (a stale lock is relocked, never one the PR has no business moving)."""
    out = [e for e in entries if RED in e.on]
    for lock in tracked:
        if lock.rsplit("/", 1)[-1] in LOCKFILES and any(manifest_of(lock, f) for f in pr_files):
            out.append(lockfile_entry(lock))  # type: ignore[arg-type]
    return out


def pin_entry(path: str, base: str) -> Entry:
    """The built-in red-mode generator for a file declaring a release_bin.Pin (ci_kit/pin_rehash.py).
    PYTHONSAFEPATH: the kit's own module (PYTHONPATH), never one the PR added."""
    run = f"PYTHONSAFEPATH=1 python3 -m ci_kit.pin_rehash --since {shlex.quote(base)} {shlex.quote(path)}"
    return Entry((path,), run, frozenset({RED}))


def pinned(root: Path, files: list[str], base: str) -> list[str]:
    """The PR's own Python files whose release-binary Pin VERSION differs from the base's copy (a
    Renovate bump). A Pin file the PR touched elsewhere keeps its frozen hashes."""
    out = []
    for f in files:
        fp = root / f
        if not (f.endswith(".py") and fp.is_file()):
            continue
        text = fp.read_text(encoding="utf-8", errors="replace")
        if not declares(text):
            continue
        r = git(root, "show", f"{base}:{f}", check=False)
        try:
            if bumped(text, r.stdout if r.returncode == 0 else ""):
                out.append(f)
        except (ValueError, SyntaxError):
            out.append(f)          # unreadable: pin_rehash itself fails loudly, never a quiet skip
    return out


def commands(entries: list[Entry]) -> list[dict]:
    """The distinct (cwd, run) pairs, in map order. Pure."""
    seen, out = set(), []
    for e in entries:
        if (e.cwd, e.run) not in seen:
            seen.add((e.cwd, e.run))
            out.append({"cwd": e.cwd, "run": e.run, "tool": e.tool})
    return out


def shrink_faults(path: str, new: str, ours: str, theirs: str) -> list[str]:
    """A shrink-only baseline may lose entries, never gain one neither side had. Pure."""
    have = set(ours.splitlines()) | set(theirs.splitlines())
    grown = [ln for ln in new.splitlines() if ln.strip() and ln not in have]
    return [f"{path}: regenerating it GREW the shrink-only baseline ({len(grown)} new line(s), first: {grown[0][:80]!r})"] \
        if grown else []


# ── git ───────────────────────────────────────────────────────────────────────────────────────
def git(root: Path, *args: str, check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_AUTHOR_NAME": BOT[0], "GIT_AUTHOR_EMAIL": BOT[1],
           "GIT_COMMITTER_NAME": BOT[0], "GIT_COMMITTER_EMAIL": BOT[1], "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", "-C", str(root), *args], check=check, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, input=input_text)


def lines(out: str) -> list[str]:
    return [x for x in out.splitlines() if x.strip()]


def unmerged(root: Path) -> list[str]:
    return sorted(set(lines(git(root, "diff", "--name-only", "--diff-filter=U").stdout)))


def merge(root: Path, base: str) -> tuple[bool, list[str]]:
    """Merge `base` into HEAD, uncommitted. (merged, conflicted): merged False = HEAD already
    contains base (nothing to merge)."""
    if git(root, "merge-base", "--is-ancestor", base, "HEAD", check=False).returncode == 0:
        return False, []
    r = git(root, "-c", "merge.conflictStyle=diff3", "merge", "--no-commit", "--no-ff", base, check=False)
    conflicted = unmerged(root)
    if r.returncode and not conflicted:
        raise RuntimeError(f"git merge {base[:12]} failed with no conflicted file: {(r.stderr or r.stdout).strip()[-300:]}")
    return True, conflicted


def side(root: Path, path: str, stage: int) -> str | None:
    """A conflicted file's content on one side (2 = ours, the PR; 3 = theirs, the base). None = absent."""
    r = git(root, "show", f":{stage}:{path}", check=False)
    return r.stdout if r.returncode == 0 else None


def snapshot(root: Path) -> str:
    """Stage everything (markers and all) and commit it LOCALLY: the tree a later change is read against."""
    git(root, "add", "-A", "--", ".")
    git(root, "commit", "-q", "--no-verify", "--allow-empty", "-m", SNAPSHOT_MSG)
    return git(root, "rev-parse", "HEAD").stdout.strip()


def changed(root: Path, since: str) -> dict[str, str]:
    """path -> write | delete: the working tree against the snapshot, untracked (not ignored) files included."""
    out: dict[str, str] = {}
    for ln in lines(git(root, "diff", "--name-status", "--no-renames", since).stdout):
        st, _, path = ln.partition("\t")
        out[path] = "delete" if st.startswith("D") else "write"
    for path in lines(git(root, "ls-files", "--others", "--exclude-standard").stdout):
        out[path] = "write"
    return out


def pr_files(root: Path, base: str, head: str) -> list[str]:
    """The PR's OWN diff: merge-base(base, head)..head."""
    mb = git(root, "merge-base", base, head).stdout.strip()
    return sorted(lines(git(root, "diff", "--name-only", "--no-renames", mb, head).stdout))


def save(root: Path, files: dict[str, str], out: Path, manifest: dict) -> None:
    """The artifact: manifest.json plus files/<path> for every written path."""
    (out / "files").mkdir(parents=True, exist_ok=True)
    for path, how in files.items():
        if how == "write":
            dst = out / "files" / path
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / path, dst)
    (out / "manifest.json").write_text(json.dumps({**manifest, "files": files}, indent=1), encoding="utf-8")


def output(**kv: str) -> None:
    for k, v in kv.items():
        print(f"{k}={v}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.writelines(f"{k}={v}\n" for k, v in kv.items())


# ── the two steps of the regen job ──────────────────────────────────────────────────────────────
@dataclass
class Prepared:
    mode: str
    base: str
    head: str
    merged: bool = False
    conflicted: list[str] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)
    commands: list[dict] = field(default_factory=list)
    allowed: list[str] = field(default_factory=list)      # globs a generator may write
    shrink: dict[str, list[str]] = field(default_factory=dict)  # path -> [ours, theirs]
    hash_only: list[str] = field(default_factory=list)    # hand files only a pin rehash may touch
    snapshot: str = ""
    state: str = ""


def prepare(root: Path, mode: str, base: str, entries: list[Entry]) -> Prepared:
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    p = Prepared(mode, base, head)
    p.merged, p.conflicted = merge(root, base)
    covered, p.uncovered = cover(p.conflicted, entries)
    if p.uncovered:
        p.state = "uncovered"
        return p
    for path, e in covered.items():
        theirs, ours = side(root, path, 3), side(root, path, 2)
        if theirs is None or ours is None:    # deleted on one side: not a generator's call
            p.uncovered.append(path)
            continue
        if e.shrink:
            p.shrink[path] = [ours, theirs]
        git(root, "checkout", "--theirs", "--", path)
    if p.uncovered:
        p.state = "uncovered"
        return p
    if mode == RED:
        tracked = lines(git(root, "ls-files").stdout)
        mine = pr_files(root, base, head)
        # a covered conflict took main's side above: its generator must run in red mode too
        used = list(dict.fromkeys([*red_entries(entries, mine, tracked), *covered.values()]))
        p.hash_only = pinned(root, mine, base)
        used += [pin_entry(f, base) for f in p.hash_only]
    else:
        used = list(dict.fromkeys(covered.values()))
    p.commands = commands(used)
    p.allowed = sorted({g for e in used for g in (*e.paths, *e.touches)} | set(covered))
    for e in used:
        if e.shrink:
            for path in [x for x in lines(git(root, "ls-files").stdout) if e.owns(x)]:
                p.shrink.setdefault(path, [git(root, "show", f"{head}:{path}", check=False).stdout,
                                           git(root, "show", f"{base}:{path}", check=False).stdout])
    p.snapshot = snapshot(root)
    p.state = "merge" if not p.commands else "regen"
    return p


def collect(root: Path, p: Prepared, out: Path, failed: str = "") -> str:
    """After the generators ran: the artifact, and the final state."""
    if failed:
        return "failed"
    files = changed(root, p.snapshot) if p.snapshot else {}
    stray = sorted(f for f in files if not any(glob_match(g, f) for g in p.allowed))
    if stray:
        print(f"::warning::a generator wrote outside the regen map: {', '.join(stray[:10])}")
        return "strayed"
    faults = []
    for path, how in files.items():
        if how == "write":
            text = (root / path).read_text(encoding="utf-8", errors="replace")
            if MARKER.search(text):
                faults.append(f"{path}: still holds a conflict marker")
            if path in p.shrink:
                faults += shrink_faults(path, text, *p.shrink[path])
    for path in p.shrink:     # a shrink-only file a generator did NOT rewrite is still checked
        if path not in files and path in p.conflicted:
            faults += shrink_faults(path, (root / path).read_text(encoding="utf-8", errors="replace"), *p.shrink[path])
    for path in p.hash_only:  # a pin file may move its sha256 literals and nothing else
        if files.get(path) == "write" and not only_hashes(git(root, "show", f"{p.snapshot}:{path}").stdout,
                                                           (root / path).read_text(encoding="utf-8")):
            faults.append(f"{path}: the pin rehash changed more than sha256 literals")
    if faults:
        print("::warning::" + "; ".join(faults))
        return "strayed"
    # a conflict resolution is in the artifact even when its generator reproduced main's side exactly
    for path in p.conflicted:
        files.setdefault(path, "write" if (root / path).exists() else "delete")
    if not files and (not p.merged or p.mode == RED):
        return "nothing"   # red mode: a bare merge of main is the queue's call, not a fix
    save(root, files, out, {"tier": "regen", "base": p.base, "head": p.head, "merged": p.merged,
                            "conflicted": p.conflicted, "commands": [c["run"] for c in p.commands]})
    return "regen" if files else "merge"


def bash() -> str:
    """bash for the generators. On Windows (a local run) Git's own bash: `bash` there resolves to
    System32's WSL launcher first, which is another machine's shell."""
    if sys.platform == "win32" and shutil.which("git"):
        exe = Path(shutil.which("git") or "").resolve()
        for top in exe.parents[1:3]:     # Git/cmd/git.exe or Git/mingw64/bin/git.exe
            c = top / "usr" / "bin" / "bash.exe"
            if c.exists():
                return str(c)
    return "bash"


def run(root: Path, cmds: list[dict]) -> str:
    """Run each generator in its directory, in order; the first that fails is named and stops the rest."""
    for c in cmds:
        print(f"::group::regen: ({c['cwd'] or '.'}) {c['run']}", flush=True)
        r = subprocess.run([bash(), "-c", c["run"]], cwd=root / (c["cwd"] or "."), check=False)
        print("::endgroup::", flush=True)
        if r.returncode:
            return f"{c['run']} (in {c['cwd'] or '.'}) exited {r.returncode}"
    return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ci_kit.pr_regen", description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("prepare", help="merge the base, resolve what the map covers, snapshot")
    a.add_argument("--mode", choices=(CONFLICT, RED), required=True)
    a.add_argument("--base", required=True, help="the base sha the heal merges (the plan job's)")
    a.add_argument("--state-file", required=True)
    r = sub.add_parser("run", help="run the prepared generators (PR code: the regen job only)")
    r.add_argument("--state-file", required=True)
    b = sub.add_parser("collect", help="after the generators: the artifact and the state")
    b.add_argument("--state-file", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--failed", default="", help="the generator that exited nonzero, if one did")
    args = ap.parse_args(argv)
    root = Path.cwd()
    try:
        if args.cmd == "prepare":
            p = prepare(root, args.mode, args.base, parse_map(os.environ.get("CI_KIT_REGEN_MAP", "")))
            Path(args.state_file).write_text(json.dumps(p.__dict__), encoding="utf-8")
            tools = {c["tool"] for c in p.commands}
            output(state=p.state, uncovered=",".join(p.uncovered)[:500], conflicted=",".join(p.conflicted)[:500],
                   node=str("node" in tools).lower(), dotnet=str("dotnet" in tools).lower())
            return 0
        p = Prepared(**json.loads(Path(args.state_file).read_text(encoding="utf-8")))
        if args.cmd == "run":
            # the caller's install ran since prepare: whatever IT changed is not the generators'
            p.snapshot = snapshot(root)
            Path(args.state_file).write_text(json.dumps(p.__dict__), encoding="utf-8")
            bad = run(root, p.commands)
            output(failed=bad.replace("\n", " ")[:300])
            return 1 if bad else 0
        output(state=collect(root, p, Path(args.out), args.failed))
        return 0
    except (subprocess.CalledProcessError, RuntimeError, ValueError, OSError) as e:
        err = (getattr(e, "stderr", "") or "").strip()[-300:]
        print(f"::error::pr_regen {args.cmd} did not finish: {e} {err}")
        output(state="failed")
        return 2


if __name__ == "__main__":
    sys.exit(main())
