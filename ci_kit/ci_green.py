"""ci-green: fast-forward a branch (default `ci-green`) to a commit CI just PASSED on main.

WHY. A box that builds from its checkout and follows `origin/main` ships whatever merged, green
or not: a merge whose push-to-main CI then fails is deployed within the hour (edge-host
deploy-reconcile, quant + ui, found 2026-11-09). A registry-image deploy can gate on "an image was
built from a green run"; a build-on-the-box deploy has no such artefact. This is that artefact as
a git ref: `ci-green` only ever holds commits CI passed on the default branch, so the box follows
it with a plain fetch and needs no GitHub credential of its own.

WHAT IT DOES, given the sha CI passed (`--sha`, the workflow_run's head_sha):

  sha not on default      nothing: the commit is not in the remote default      -> 1
                           branch's history (the workflow guard read a NAME; this
                           reads the history)
  branch absent            create it at the sha                                  -> 0 created
  branch == sha            nothing                                               -> 0 current
  branch is an ancestor    push sha to it, NON-FORCE (the remote refuses a       -> 0 advanced
                           non-fast-forward even if this check were wrong)
  sha is an ancestor       nothing: an older run finished after a newer one      -> 0 older
                           (CI on main cancels in progress, but completion order
                           is not push order)
  diverged                 nothing: the branch holds a commit main does not, so   -> 1
                           someone pushed it by hand or main was rewritten. Never
                           forced over: a person decides which history is green.

SAFETY. The branch may never be the remote's default branch (or `main`/`master`): this tool
moves one ref, and it is not the one people merge to. `--dry-run` prints the plan, pushes nothing.

EXIT. 0 the branch holds the sha or something newer - 1 diverged, not on the default branch, or
the push was refused -
2 could not look: not a git checkout, the sha is not a commit here, the fetch failed, a refused
branch name.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

PROG = "ci-green"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_FORBIDDEN = frozenset({"main", "master", "HEAD"})


class Blind(Exception):
    """Could not look: the run ends with exit 2."""


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    r = subprocess.run(["git", *args], capture_output=True, text=True, timeout=120)
    if check and r.returncode != 0:
        raise Blind(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip() or f'rc {r.returncode}'}")
    return r


def _default_branch(remote: str) -> str:
    """The remote's HEAD branch, or '' when the remote does not say."""
    r = _git("ls-remote", "--symref", remote, "HEAD", check=False)
    m = re.search(r"^ref: refs/heads/(\S+)\s+HEAD$", r.stdout, re.MULTILINE)
    return m.group(1) if m else ""


def _remote_sha(remote: str, branch: str) -> str:
    """The branch's sha on the remote, '' when it does not exist. A failed ls-remote is blind."""
    r = _git("ls-remote", "--exit-code", remote, f"refs/heads/{branch}", check=False)
    if r.returncode == 2:   # --exit-code: no matching ref
        return ""
    if r.returncode != 0:
        raise Blind(f"git ls-remote {remote}: {(r.stderr or r.stdout).strip()}")
    return r.stdout.split()[0]


def _is_ancestor(a: str, b: str) -> bool:
    r = _git("merge-base", "--is-ancestor", a, b, check=False)
    if r.returncode not in (0, 1):
        raise Blind(f"git merge-base --is-ancestor {a[:12]} {b[:12]}: {r.stderr.strip()}")
    return r.returncode == 0


def advance(sha: str, branch: str, remote: str, *, dry_run: bool) -> tuple[int, str]:
    """Decide and (unless dry_run) act. Returns (exit code, one-line verdict)."""
    if not _SHA.match(sha):
        raise Blind(f"--sha {sha!r} is not a full 40-hex commit id")
    default = _default_branch(remote)
    if branch in _FORBIDDEN or branch == default or branch.startswith("refs/"):
        raise Blind(f"refusing to move {branch!r}: it is (or may be) the branch people merge to")
    _git("rev-parse", "--is-inside-work-tree")
    if _git("cat-file", "-t", sha, check=False).stdout.strip() != "commit":
        raise Blind(f"{sha[:12]} is not a commit in this checkout (fetch it first)")
    # the workflow's guard says the run was on the default branch; prove the COMMIT is (a second
    # layer: a run's head_branch is a name, the remote's history is the fact)
    if not default:
        raise Blind(f"{remote} does not name its default branch; cannot prove {sha[:12]} merged")
    _git("fetch", "-q", remote, f"refs/heads/{default}")
    if not _is_ancestor(sha, "FETCH_HEAD"):
        return 1, (f"NOT ON {default}: {sha[:12]} is not in {remote}/{default}'s history -- refusing to"
                   f" publish a commit that never merged as green")
    cur = _remote_sha(remote, branch)
    if cur == sha:
        return 0, f"current: {branch} is already {sha[:12]}"
    if cur:
        _git("fetch", "-q", remote, f"refs/heads/{branch}")
        if _is_ancestor(sha, cur):
            return 0, f"older: {sha[:12]} is already inside {branch} ({cur[:12]}); nothing to do"
        if not _is_ancestor(cur, sha):
            return 1, (f"DIVERGED: {branch} ({cur[:12]}) is not an ancestor of {sha[:12]} -- it holds a "
                       "commit main does not. Not forced; a person decides which history passed CI.")
    verb = "advanced" if cur else "created"
    plan = f"{verb}: {branch} {cur[:12] or '<none>'} -> {sha[:12]}"
    if dry_run:
        return 0, f"(dry run) would be {plan}"
    r = _git("push", "-q", remote, f"{sha}:refs/heads/{branch}", check=False)
    if r.returncode != 0:
        return 1, f"push REFUSED ({plan}): {(r.stderr or r.stdout).strip()}"
    after = _remote_sha(remote, branch)
    if after != sha:
        return 1, f"pushed but {branch} reads {after[:12] or '<none>'}, not {sha[:12]}"
    return 0, plan


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog=PROG, description=__doc__.splitlines()[0])
    ap.add_argument("--sha", required=True, help="the commit CI passed (workflow_run head_sha)")
    ap.add_argument("--branch", default="ci-green", help="the branch to fast-forward (default ci-green)")
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        rc, line = advance(a.sha, a.branch, a.remote, dry_run=a.dry_run)
    except Blind as e:
        print(f"{PROG}: BLIND -- {e}", file=sys.stderr)
        return 2
    print(f"{PROG}: {line}", file=sys.stderr if rc else sys.stdout)
    return rc


if __name__ == "__main__":
    sys.exit(main())
