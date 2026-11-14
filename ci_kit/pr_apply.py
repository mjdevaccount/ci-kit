"""The heal ladder's ONE pusher: redo the merge, take ONLY what a tier changed, check its scope, push non-force.

It runs on a FRESH runner holding the push token (the caller's PR_UPDATE_TOKEN: a GITHUB_TOKEN push
fires no CI) and never executes PR code: git, and this module from ci-kit at the plan job's sha
(PYTHONSAFEPATH keeps a PR-added `ci_kit/` off the path). The tier jobs that DID run PR code (the
generators, Claude) hand over an artifact: a manifest plus the files they changed. Nothing else
from those jobs is trusted -- not their merge, not their verdict, not their file list:

  * the head must still be the head the ladder was dispatched for, else nothing is pushed (the
    author moved on; a new head is a new budget);
  * the merge is redone here from the same base sha, and git's conflicted set must be EXACTLY the
    manifest's -- every conflicted file must come back resolved in the artifact, with no marker;
  * SCOPE (`scope`, pure), per tier:
      regen   every file matches the caller's regen map (main's copy, passed as an input), is a
              lockfile of a built-in kind, or changes ONLY sha256 literals (a release-binary pin
              rehash, ci_kit/pin_rehash.rehashed_only: only a BUMPED pin's hashes, recomputed here);
      claude  every file is one the PR already changes, a conflicted file, or a mapped generated
              file -- the fix may not WIDEN the PR; nothing under `.github/` (this token can push
              workflows); no TEST file unless it was conflicted (a test that must change to pass is
              a red flag: refused, the report pages instead); at most `max_lines` changed lines
              outside the conflict resolutions;
  * the commit is a merge commit (when main was merged) or one fix commit, and the push is a plain
    fast-forward `git push`: never `--force`, never a rebase. A head that moved in the meantime is
    refused by GitHub, not overwritten.

Outputs `result` = pushed | moved | refused | nothing | failed, `sha`, `why`.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import subprocess
import sys
from pathlib import Path

from ci_kit.pin_rehash import declares, rehashed_only
from ci_kit.pr_regen import LOCKFILES, MARKER, git, glob_match, lines, merge, output, parse_map, unmerged

# a test file in any of the estate's stacks (python, js/ts, .NET)
TEST_GLOBS = ("tests/**", "test/**", "**/tests/**", "**/test/**", "**/__tests__/**", "**/test_*.py", "**/*_test.py",
              "**/*.test.*", "**/*.spec.*", "**/*Tests/**", "**/*.Tests/**", "**/conftest.py")
FORBIDDEN = (".github/**", ".git/**", ".gitattributes", ".gitmodules", "**/.claude/settings*.json")


def is_test(path: str, globs: tuple[str, ...] = TEST_GLOBS) -> bool:
    return any(glob_match(g, path) for g in globs)


def safe_path(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    return bool(path) and not path.startswith("/") and ".." not in parts and ":" not in parts[0]


def scope(tier: str, files: dict[str, str], conflicted: list[str], pr_files: list[str], regen_globs: list[str],
          changed_lines: int, max_lines: int, test_globs: tuple[str, ...] = TEST_GLOBS,
          hash_only: frozenset[str] = frozenset()) -> list[str]:
    """Why this change may NOT be pushed ([] = it may). Pure: the policy the tests pin."""
    bad: list[str] = []
    missing = [c for c in conflicted if c not in files]
    if missing:
        bad.append(f"conflicted file(s) left unresolved: {', '.join(missing[:8])}")
    for path in sorted(files):
        if not safe_path(path):
            bad.append(f"{path}: not a plain repository path")
            continue
        generated = any(glob_match(g, path) for g in regen_globs) or path.rsplit("/", 1)[-1] in LOCKFILES
        if tier == "regen":
            if not generated and path not in hash_only:
                bad.append(f"{path}: not in the regen map, a lockfile or a sha256-only pin change -- tier 2 writes "
                           "nothing else")
            continue
        if any(glob_match(g, path) for g in FORBIDDEN):
            bad.append(f"{path}: tier 3 never edits CI, git or agent configuration")
        elif path in conflicted:
            continue
        elif is_test(path, test_globs):
            bad.append(f"{path}: a TEST changed to make the PR pass -- a red flag, left for a session")
        elif path not in pr_files and not generated:
            bad.append(f"{path}: outside the PR's own diff -- the fix would widen the PR")
    if tier != "regen" and changed_lines > max_lines:
        bad.append(f"{changed_lines} changed line(s) outside the conflict resolutions (limit {max_lines})")
    return bad


def count_lines(old: str, new: str) -> int:
    return sum(1 for d in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
               if d[:1] in "+-" and not d.startswith(("+++", "---")))


def apply(root: Path, art: Path, *, head: str, base: str, ref: str, regen_globs: list[str], max_lines: int,
          test_globs: tuple[str, ...] = TEST_GLOBS) -> tuple[str, str]:
    """Redo the merge, check scope, write, commit. (result, why); result 'ready' = committed, push next.
    `base` is the PLAN's sha (a job without PR code): the artifact's own base and merge flag are only
    checked against it, never trusted -- a manifest written beside PR code could name any commit."""
    m = json.loads((art / "manifest.json").read_text(encoding="utf-8"))
    tier = str(m.get("tier") or "")
    files: dict[str, str] = {str(k): str(v) for k, v in (m.get("files") or {}).items()}
    now = git(root, "rev-parse", "HEAD").stdout.strip()
    if now != head or m.get("head") != head:
        return "moved", f"the PR head is {now[:12]}, the heal was for {head[:12]} (manifest {str(m.get('head'))[:12]})"
    if tier not in ("regen", "claude"):
        return "refused", f"unknown tier {tier!r} in the manifest"
    if m.get("base") != base:
        return "refused", f"the artifact names base {str(m.get('base'))[:12]}, the plan fixed {base[:12]}"
    odd = sorted(k for k, v in files.items() if v not in ("write", "delete"))
    if odd:
        return "refused", f"the artifact marks {odd[:6]} as neither write nor delete"
    if m.get("merged") and git(root, "merge-base", "--is-ancestor", base, head, check=False).returncode == 0:
        return "refused", "the artifact claims a merge, but the head already contains the base"
    fork = git(root, "merge-base", base, head).stdout.strip()
    mine = sorted(lines(git(root, "diff", "--name-only", "--no-renames", fork, head).stdout))
    merged, conflicted = merge(root, base) if m.get("merged") else (False, [])
    if sorted(conflicted) != sorted(m.get("conflicted") or []):
        return "refused", (f"git's conflicted set {conflicted[:6]} is not the healed one {(m.get('conflicted') or [])[:6]}: "
                           "the merge differs from the one the tier resolved")
    changed, hashes = 0, set()
    for path, how in files.items():
        if how == "write" and path not in conflicted and safe_path(path):
            old = git(root, "show", f":{path}", check=False).stdout
            new = (art / "files" / path).read_text(encoding="utf-8", errors="replace")
            changed += count_lines(old, new)
            # only a Pin file the PR itself changes, and only the hashes of a pin whose VERSION it moved
            if path in mine and declares(old):
                before = git(root, "show", f"{fork}:{path}", check=False).stdout
                hashes |= {path} if rehashed_only(old, new, before) else set()
    bad = scope(tier, files, conflicted, mine, regen_globs, changed, max_lines, test_globs, frozenset(hashes))
    if bad:
        return "refused", "; ".join(bad)
    for path, how in files.items():
        dst = root / path
        if how == "delete":
            git(root, "rm", "-q", "--ignore-unmatch", "--", path)
            continue
        text = (art / "files" / path).read_bytes()
        if MARKER.search(text.decode("utf-8", errors="replace")):
            return "refused", f"{path}: still holds a conflict marker"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(text)
        git(root, "add", "--", path)
    left = unmerged(root)
    if left:
        return "refused", f"still unmerged after the heal: {', '.join(left[:8])}"
    if not merged and not git(root, "diff", "--cached", "--name-only").stdout.strip():
        return "nothing", "the tier's change is already on the head"
    what = ", ".join(sorted(files)[:6]) + ("..." if len(files) > 6 else "")
    msg = (f"Merge {base[:12]} into {ref} (heal: {tier})" if merged else f"heal({tier}): {m.get('summary') or 'regenerate'}")
    body = f"{msg}\n\nheal ladder tier {tier}: {what or 'merge only'}\n{m.get('summary') or ''}".strip()
    git(root, "commit", "-q", "--no-verify", "-m", body)
    return "ready", what


def push(root: Path, ref: str, repo: str) -> tuple[bool, str]:
    """A plain fast-forward push with the token from the environment (a credential helper reads
    it; it is never on the command line). A remote that moved refuses it: nothing is forced."""
    helper = "!f() { echo username=x-access-token; echo \"password=$CI_KIT_PUSH_TOKEN\"; }; f"
    url = os.environ.get("CI_KIT_PUSH_URL") or f"https://github.com/{repo}.git"
    r = git(root, "-c", "credential.helper=", "-c", f"credential.helper={helper}", "push", url, f"HEAD:refs/heads/{ref}",
            check=False)
    return r.returncode == 0, (r.stderr or r.stdout).strip()[-300:]


def pushed_over(said: str) -> str:
    """A refused push -> 'moved' ONLY when the branch moved under it (non-fast-forward / fetch first);
    any other refusal -- a ruleset, a protected-branch hook, auth -- is 'failed', which pages. Pure."""
    return "moved" if ("non-fast-forward" in said or "(fetch first)" in said) else "failed"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ci_kit.pr_apply", description=__doc__.split("\n", 1)[0])
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--base", required=True, help="the plan job's base sha (never the artifact's)")
    ap.add_argument("--ref", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--max-lines", type=int, default=300)
    ap.add_argument("--dry-run", action="store_true", help="commit locally, push nothing")
    a = ap.parse_args(argv)
    root = Path.cwd()
    try:
        regen = [g for e in parse_map(os.environ.get("CI_KIT_REGEN_MAP", "")) for g in (*e.paths, *e.touches)]
        result, why = apply(root, Path(a.artifact), head=a.head, base=a.base, ref=a.ref, regen_globs=regen,
                            max_lines=a.max_lines)
        sha = ""
        if result == "ready":
            sha = git(root, "rev-parse", "HEAD").stdout.strip()
            ok, said = (True, "dry run") if a.dry_run else push(root, a.ref, a.repo)
            result, why = ("pushed", why) if ok else (pushed_over(said), f"push refused: {said}")
    except (subprocess.CalledProcessError, RuntimeError, ValueError, OSError, KeyError) as e:
        result, why, sha = "failed", f"{e} {(getattr(e, 'stderr', '') or '').strip()[-200:]}", ""
    if result in ("refused", "failed"):
        print(f"::error::heal push {result}: {why}")
    output(result=result, sha=sha, why=why.replace("\n", " ")[:600])
    return 0 if result in ("pushed", "moved", "nothing") else 1


if __name__ == "__main__":
    sys.exit(main())
