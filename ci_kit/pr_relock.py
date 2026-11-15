"""Relock a dependency bot's PR: the heal ladder's lockfile tier on the PR head, and its one pusher.

WHY. A dependency bot that bumps a pin with a regex (a release-wheel URL, a PEP 508 `@vX.Y.Z` git
pin) rewrites the manifest and leaves the lockfile alone, and a hosted bot runs no post-upgrade
task, so the PR goes red on `uv lock --check` / `uv sync --locked` and waits for someone to run
`uv lock` by hand. `.github/workflows/relock.yml` is that run.

ONE IMPLEMENTATION. The heal ladder already regenerates a lockfile with no secret in scope
(ci_kit/pr_regen.py: snapshot, run the built-in LOCKFILES command in the lockfile's own directory,
collect only what changed, `strayed` for any write outside the allowed paths) and pushes it from a
fresh runner holding the token (ci_kit/pr_apply.py: head re-check, scope, `github-actions[bot]`
commit, fast-forward only). This module adds only what a relock needs and a heal does not:
  * NO MERGE of the base: a relock pins what the PR names; bringing main in is the updater's call;
  * the lockfile set is the CALLER's `lockfiles` input (each a tracked file whose basename is a
    pr_regen.LOCKFILES kind), not a conflict or the PR's diff;
  * the event gate (a `pull_request` from THIS repo on the bot's branch prefix), checked in both jobs;
  * the push side's STRICT artifact check: exactly manifest.json + files/<p> per written p, every p
    in the push job's OWN copy of the input, regular files only (no symlink), no delete, no merge
    or conflict claim -- a build backend ran beside the job that wrote it;
  * NO LOOP: a relock needed on top of our own relock commit means the tool is not deterministic
    here, and the push is refused rather than made again.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from ci_kit.pr_apply import apply, push, pushed_over, safe_path
from ci_kit.pr_regen import BOT, LOCKFILES, RED, Prepared, collect, commands, git, lockfile_entry, output, snapshot

SUBJECT = "chore(lock): relock"


def parse_lockfiles(text: str) -> list[str]:
    """The `lockfiles` input (space/newline separated) -> repo-relative paths. Raises ValueError: a
    typo must turn the run red, never relock nothing (or something else). Pure."""
    out: list[str] = []
    for raw in (text or "").split():
        p = raw[2:] if raw.startswith("./") else raw
        parts = p.split("/")
        if not safe_path(p) or "\\" in p or "" in parts or "." in parts or any(c in p for c in "*?[]"):
            raise ValueError(f"lockfiles: {raw!r} is not a plain repository path")
        if parts[-1] not in LOCKFILES:
            raise ValueError(f"lockfiles: {raw!r} is not a lockfile kind the kit relocks ({', '.join(LOCKFILES)})")
        if p not in out:
            out.append(p)
    if not out:
        raise ValueError("lockfiles: empty -- name at least one (the default is uv.lock)")
    return out


def refusal(event: str, head_repo: str, repo: str, head_ref: str, prefix: str) -> str:
    """Why this run may not relock ('' = it may). Pure: the gate both jobs apply."""
    if event != "pull_request":
        return f"relock needs a pull_request caller (got '{event}'): only that event names the PR branch to relock"
    if not prefix.strip():
        return "branch-prefix is empty: every branch, a person's too, would be relocked"
    if head_repo != repo:
        return f"the PR comes from {head_repo or 'a deleted fork'}, not {repo}: a fork is never pushed to"
    if not head_ref.startswith(prefix):
        return f"{head_ref} is not a dependency-bot branch (prefix {prefix}): its author relocks it"
    return ""


def untracked(root: Path, paths: list[str]) -> list[str]:
    return [p for p in paths if git(root, "ls-files", "--error-unmatch", "--", p, check=False).returncode]


def prepare(root: Path, paths: list[str], base: str) -> Prepared:
    """The state pr_regen's `run` and `collect` read: each lockfile's built-in command, a local
    snapshot to diff against, no merge."""
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    p = Prepared(RED, base, head, commands=commands([lockfile_entry(x) for x in paths]), allowed=list(paths))
    p.snapshot = snapshot(root)
    p.state = "regen"
    return p


def artifact_faults(art: Path, allowed: list[str]) -> list[str]:
    """Why this artifact is not exactly a relock of `allowed` ([] = it is)."""
    mf = art / "manifest.json"
    if mf.is_symlink() or not mf.is_file():
        return ["the artifact holds no regular manifest.json"]
    try:
        m = json.loads(mf.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        return [f"manifest.json is unreadable: {e}"]
    files = m.get("files") if isinstance(m, dict) else None
    if not isinstance(files, dict) or not files:
        return ["the manifest names no file"]
    out = []
    if m.get("tier") != "regen" or m.get("merged") or m.get("conflicted"):
        out.append("the manifest claims a merge, a conflict or a tier other than regen -- a relock is none of them")
    for k, v in files.items():
        if k not in allowed:
            out.append(f"{k!r}: not a lockfile this relock may write ({', '.join(allowed)})")
        elif v != "write":
            out.append(f"{k}: marked {v!r} -- a relock only rewrites a lockfile")
    want = {"manifest.json"} | {f"files/{k}" for k in files if k in allowed}
    seen = set()
    for top, dirs, names in os.walk(art, followlinks=False):
        for n in [*dirs, *names]:
            p = Path(top) / n
            rel = p.relative_to(art).as_posix()
            if p.is_symlink():
                out.append(f"{rel}: a symlink")
            elif n in names and not p.is_file():
                out.append(f"{rel}: not a regular file")
            elif n in names and rel not in want:
                out.append(f"{rel}: an extra file")
            elif n in names:
                seen.add(rel)
    out += [f"{w}: missing from the artifact" for w in sorted(want - seen)]
    return out


def own_relock(root: Path) -> bool:
    """HEAD is a relock commit this workflow made (author and subject)."""
    email, _, subject = git(root, "log", "-1", "--format=%ae%x00%s").stdout.strip().partition("\x00")
    return email == BOT[1] and subject.startswith(SUBJECT)


def relock_push(root: Path, art: Path, *, paths: list[str], head: str, base: str, ref: str, repo: str,
                dry_run: bool = False) -> tuple[str, str, str]:
    """(result, why, sha): result pushed | moved | nothing | refused | failed."""
    bad = artifact_faults(art, paths)
    if bad:
        return "refused", "; ".join(bad), ""
    gone = untracked(root, paths)
    if gone:
        return "refused", f"not tracked at the head, so never written: {', '.join(gone)}", ""
    now = git(root, "rev-parse", "HEAD").stdout.strip()
    if now != head:
        return "moved", f"{ref} moved to {now[:12]} while {head[:12]} was relocked -- the new head runs its own relock", ""
    if own_relock(root):
        return "refused", ("a relock is needed on top of our own relock commit: the lock tool is not deterministic "
                           "here -- refusing to loop"), ""
    names = sorted(json.loads((art / "manifest.json").read_text(encoding="utf-8"))["files"])
    msg = f"{SUBJECT} {', '.join(names)}\n\nThe dependency bot moved the manifest on {ref} without its lockfile."
    result, why = apply(root, art, head=head, base=base, ref=ref, regen_globs=[], max_lines=0, message=msg)
    if result != "ready":
        return result, why, ""
    touched = sorted(git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").stdout.split())
    if not touched or set(touched) - set(paths):
        return "refused", f"the relock commit touches {touched}, not only {paths}", ""
    sha = git(root, "rev-parse", "HEAD").stdout.strip()
    ok, said = (True, "dry run") if dry_run else push(root, ref, repo)
    return ("pushed", ", ".join(touched), sha) if ok else (pushed_over(said), f"push refused: {said}", "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ci_kit.pr_relock", description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    gate = argparse.ArgumentParser(add_help=False)
    for a in ("--event", "--head-repo", "--repo", "--ref", "--branch-prefix", "--lockfiles"):
        gate.add_argument(a, required=True)
    a = sub.add_parser("prepare", parents=[gate], help="the lock job, before any PR code: snapshot + commands")
    a.add_argument("--base", required=True)
    a.add_argument("--state-file", required=True)
    c = sub.add_parser("collect", help="the lock job, after the relock: the artifact and the state")
    c.add_argument("--state-file", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--failed", default="")
    p = sub.add_parser("push", parents=[gate], help="the push job (no PR code): check, commit, push fast-forward")
    for x in ("--artifact", "--head", "--base"):
        p.add_argument(x, required=True)
    p.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    root = Path.cwd()
    if args.cmd == "collect":
        st = collect(root, Prepared(**json.loads(Path(args.state_file).read_text(encoding="utf-8"))), Path(args.out),
                     args.failed)
        output(state=st)
        if st == "nothing":
            print("::notice title=relock::the lockfile(s) already match the manifest -- nothing to push")
        elif st != "regen":
            print(f"::error::relock {st}: {args.failed or 'the lock tool wrote outside the lockfile(s), or left a marker'}")
        return 0 if st in ("nothing", "regen") else 1
    try:
        no = refusal(args.event, args.head_repo, args.repo, args.ref, args.branch_prefix)
        paths = parse_lockfiles(args.lockfiles)
        if args.cmd == "prepare":
            gone = untracked(root, paths)
            if no or gone:
                raise ValueError(no or f"lockfiles not tracked at the head: {', '.join(gone)}")
            st = prepare(root, paths, args.base)
            Path(args.state_file).write_text(json.dumps(st.__dict__), encoding="utf-8")
            tools = {x["tool"] for x in st.commands}
            output(state=st.state, **{t: str(t in tools).lower() for t in ("uv", "node", "dotnet")})
            return 0
        result, why, sha = ("refused", no, "") if no else relock_push(
            root, Path(args.artifact), paths=paths, head=args.head, base=args.base, ref=args.ref, repo=args.repo,
            dry_run=args.dry_run)
    except (subprocess.CalledProcessError, RuntimeError, ValueError, OSError, KeyError) as e:
        print(f"::error::relock {args.cmd} did not finish: {e} {(getattr(e, 'stderr', '') or '').strip()[-200:]}")
        output(state="failed", result="failed")
        return 2
    if result in ("refused", "failed"):
        print(f"::error::relock push {result}: {why}")
    else:
        print(f"::notice title=relock::{result}: {why}")
    output(result=result, sha=sha, why=why.replace("\n", " ")[:600])
    return 0 if result in ("pushed", "moved", "nothing") else 1


if __name__ == "__main__":
    sys.exit(main())
