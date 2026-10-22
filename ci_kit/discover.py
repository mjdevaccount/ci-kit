"""Which files a gate scans -- from `git ls-files`, never a filesystem walk.

WHY THIS EXISTS, measured 2026-09-18. Three scan loops excluded the agent's own
files with

    if "__pycache__" in f.parts or ".claude" in f.parts: continue

and `rglob` yields ABSOLUTE paths. So when the checkout itself lives at
`<repo>/.claude/worktrees/<name>/` -- exactly where `git worktree add` puts one -- **every file in
the repository matches that exclusion and the scan sees nothing.** Same commit, two checkouts,
opposite failures from one cause: one gate went BLIND AND GREEN (0 files, exit 0), another went
LOUD AND WRONG (15 findings telling the author to delete a correct inventory). A checker that
cannot reach a file must not claim the file is missing.

`git ls-files` fixes both by construction: it returns REPO-RELATIVE paths, so the checkout's own
location cannot poison them, and it scans exactly what CI scans ("index from `git ls-files`, never
a filesystem walk -- a walk sees your untracked files, CI does not").

AND A FLOOR. Zero files scanned is not a clean result, it is a gate that did not run:
`require_scanned` exits **2** ("could not look"), which is distinct from 1 (findings).

ROOT. The private copy derived its root from its own file location (`scripts/..`). A package
installed into a venv cannot, so `root` defaults to `git rev-parse --show-toplevel` of the current
directory -- the checkout the gate was run in -- and a directory that is not inside a git checkout
is exit 2, never an empty scan. Pass `root=` explicitly when the gate knows better.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
from collections.abc import Iterable


def _git(args: list[str], *, timeout: int) -> subprocess.CompletedProcess[bytes]:
    """Run git, or exit 2 when it cannot run at all (missing binary, hung) -- a traceback would
    exit 1, which reads as findings."""
    try:
        return subprocess.run(["git", *args], capture_output=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"discover: could not run git {args[2:3]}: {type(exc).__name__}: {exc} -- "
              f"a gate that cannot enumerate its subject must not report clean", file=sys.stderr)
        raise SystemExit(2) from exc


def repo_root(start: pathlib.Path | None = None) -> pathlib.Path:
    """The git toplevel containing `start` (default: cwd), or exit 2 -- never a guess."""
    here = start or pathlib.Path.cwd()
    r = _git(["-C", str(here), "rev-parse", "--show-toplevel"], timeout=60)
    top = r.stdout.decode("utf-8", errors="replace").strip()
    if r.returncode != 0 or not top:
        print(f"discover: {here} is not inside a git checkout -- a gate that cannot find its "
              f"repository must not report clean", file=sys.stderr)
        raise SystemExit(2)
    return pathlib.Path(top)


def tracked(scan_dirs: Iterable[str], *, suffix: str = ".py",
            root: pathlib.Path | None = None) -> list[str]:
    """Repo-relative POSIX paths of tracked files under `scan_dirs`, sorted.

    A `git ls-files` failure raises SystemExit(2) rather than returning an empty list, because an
    empty list here is indistinguishable from a clean tree.

    `__pycache__` is still skipped: it is never tracked, so this is belt-and-braces for a repo
    that has committed one by accident. `.claude` is NOT skipped by path -- a worktree's files are
    not tracked IN the parent checkout, which is the honest reason and does not depend on where
    the checkout sits.
    """
    scan_dirs = list(scan_dirs)
    base = root if root is not None else repo_root()
    dirs = [d for d in scan_dirs if (base / d).is_dir()]
    if not dirs:
        print(f"discover: none of {scan_dirs} exists under {base} -- cannot look",
              file=sys.stderr)
        raise SystemExit(2)
    r = _git(["-C", str(base), "ls-files", "-z", *dirs], timeout=120)
    if r.returncode != 0:
        detail = r.stderr.decode("utf-8", errors="replace").strip().splitlines()
        print(f"discover: git ls-files failed ({detail[-1] if detail else 'no output'}) -- "
              f"a gate that cannot enumerate its subject must not report clean", file=sys.stderr)
        raise SystemExit(2)
    out = []
    for raw in r.stdout.decode("utf-8", errors="replace").split("\0"):
        rel = raw.strip()
        if not rel or not rel.endswith(suffix) or "__pycache__" in rel.split("/"):
            continue
        out.append(rel)
    return sorted(out)


def require_scanned(n: int, *, gate: str, dirs: Iterable[str], floor: int = 1) -> None:
    """Exit 2 when a gate read fewer files than it must have. Call it BEFORE reporting.

    The floor is the caller's decision: a gate over a whole service should see hundreds, a narrow
    one might legitimately see three. None can legitimately see ZERO.
    """
    if n < floor:
        print(f"{gate}: scanned {n} file(s) under {list(dirs)} -- a gate that read nothing has "
              f"NOT verified anything, so this is exit 2 (could not look), never a clean pass",
              file=sys.stderr)
        raise SystemExit(2)
