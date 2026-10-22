#!/usr/bin/env python3
"""ci-manual-lint -- does every path an agent manual (CLAUDE.md) points at resolve TODAY?

WHY THIS IS NOT `ci-doc-lint`. The doc gate asks "did this path's count fall from positive to
zero", which is what makes it quiet enough to run over a whole docs tree -- and it says plainly
that a path which was NEVER real (a typo, an invented filename, a file that only lived in a plan)
is invisible to it forever. For most docs that is the right price. For the manual an agent loads
into EVERY session it is not: a wrong pointer there is followed, not read. So this gate asks the
noisier question over the one or two files where the answer is worth it, and keeps the noise down
by SHAPE rather than by allowlist.

It replaces four per-repo copies of `scripts/lint_claude_md.py` that had drifted to 0.46-0.53
similarity: one copy had a migration-range regex (`0\\d\\d`) that went dead the day migrations
passed 099 while reporting clean, one resolved against the filesystem (green locally on an
untracked file, red in CI), and every one of them had independently paid for the same two
backtick-pairing bugs. One reader, the per-repo rules as flags.

WHAT A POINTER IS (decided by shape, the same rule in every repo):
  * a code span (`x`, ``x``: a run of N backticks closed by N) outside fenced blocks, never
    paired across a blank line (CommonMark ends a span at the block);
  * decorations stripped: trailing punctuation, a leading `./`, and whatever follows a file
    extension after `:` `#` or `@` (`:12`, `:main`, `::test_x`, `#anchor`, `@v1`);
  * NOT a pointer: anything with whitespace, a glob/template/quote/formula character, a URL, a
    `:` left after the line pin (a docker tag, a `key:value`), a leading `/` `#` `-` `~` `@`
    `../` (route, anchor, flag, home path, scope, relative sibling), a bare extension (`.py`),
    a one-uppercase-letter segment (the `tests/X.py` placeholder), or a purely numeric segment
    (`0.5/365` is a ratio);
  * a pointer if it ends in a file extension, OR contains `/` and its first segment is a
    tracked top-level name or a declared sibling repo. `tier1/tier2` and `America/New_York`
    are prose by this rule without being listed anywhere.
  * DECLARED BLIND SPOT, the price of that rule: an extension-less pointer whose FIRST segment
    is itself gone or mistyped (`servcie/admin/`, a deleted `flows/nightly/`) reads as prose.
    Name the file, not just its directory, where it matters.

HOW IT RESOLVES:
  * against `git ls-files`, never the filesystem: identical locally and in CI by construction;
  * a bare name (`bus.py`, `enrich/`) is searched among every tracked basename and directory;
  * a qualified path must be a tracked path or directory from the root (`--suffix-match` also
    accepts it as the tail of one: `lib/api.ts` for `src/lib/api.ts`);
  * a path the committed .gitignore covers is deliberately absent and is never "broken";
  * `--sibling NAME`: a pointer into a sibling repo is checked when that checkout is present
    beside this one and is SILENT when it is not -- a checker that cannot reach a file must not
    claim it is missing (CI clones one repo; the workstation has all of them).

THE OPTIONAL CHECKS, each off unless asked for:
  --migrations DIR      a pinned `NNN-NNN` range in the manual whose high end is below the newest
                        `NNN_*.sql` in DIR is stale (error). Word-bounded: a port `3000-3004`
                        and a two-digit `30-40` are not migration ranges
  --line-budget N       the manual is over N lines (warning)
  --empty-package DIR   DIR is documented as an empty placeholder: any *.py other than
                        __init__.py is drift (error)
  --ref-dir DIR         pointers in DIR/*.md resolve, suffix-matched (warning)
  --stamp TEXT          every DIR/*.md of a --ref-dir contains TEXT, e.g. `LAST-VERIFIED:` (warning)
  --index FILE          every *.md beside FILE is named in it by stem (error): an index is only
                        useful if it is total
  --exempt-prefix P     a namespace of yours whose spans look like paths and are not (repeatable)

Usage:
    ci-manual-lint                                  check CLAUDE.md at the repo root
    ci-manual-lint --sibling trade-data --migrations migrations --line-budget 200
    ci-manual-lint @scripts/manual_lint.args        flags from a file, one per line

Exit codes: 0 clean (warnings do not fail) · 1 errors · 2 DID NOT FINISH LOOKING (no git, empty
index, a manual that is not there).
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ci_kit import discover

# A code span is a run of N backticks closed by a run of N. Assuming N=1 lets one ``double``
# span shift the pairing for the rest of the document (it cost one manual 72 of its 78 pointers).
_SPAN = re.compile(r"(`+)([^`].*?)\1", re.S)
# Each ``` fence contributes three backticks, so an unstripped fence shifts the pairing too (one
# gate was checking 5 pointers of 70, clean every run).
_FENCE = re.compile(r"```.*?```", re.S)
# CommonMark never lets a code span cross a block boundary, so neither may the pairing: one stray
# backtick (`Press the ` key.`) would otherwise pair with the next paragraph's opener and shift
# every span after it -- the ``double`` bug again, by another route.
_BLANK_LINE = re.compile(r"\n[ \t]*\n")
_FILE_EXT = (".md", ".py", ".yaml", ".yml", ".sql", ".json", ".toml", ".sh", ".ps1", ".ts",
             ".tsx", ".mjs", ".js", ".css", ".html")
_NOT_IN_A_PATH = set("*<>{}[]$|?!='\"`()\\+%,·−×≤≥")
_NEVER_A_PATH_PREFIX = ("/", "#", "-", "~", "@", "../")
# At least three digits, not exactly three: `(\d{3})_` would read 1000_x.sql as 100 and go dead
# at migration 999, the same way a `0\d\d` range regex went dead at 099.
_MIGRATION = re.compile(r"(\d{3,})_")
_RANGE = re.compile(r"\b(\d{3,})\s*[–—-]\s*(\d{3,})\b")
# What may follow a file extension and is not part of the path: a `:line` / `:symbol` /
# `::test_id` pin, a `#anchor`, an `@ref`.
_PIN_CHARS = ":#@"


class CouldNotLook(Exception):
    """Exit 2: the run cannot vouch for anything."""


@dataclass
class Repo:
    """What the checkout tracks, read once from `git ls-files`."""

    root: Path
    siblings: tuple[str, ...] = ()
    sibling_base: Path | None = None
    exempt: tuple[str, ...] = ()
    paths: set[str] = field(default_factory=set)       # every tracked path AND directory prefix
    names: set[str] = field(default_factory=set)       # every basename AND directory component
    rels: list[str] = field(default_factory=list)      # tracked files

    @classmethod
    def read(cls, root: Path, **kw) -> Repo:
        r = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True,
                           check=False, timeout=60)
        if r.returncode != 0:
            raise CouldNotLook(f"git ls-files failed in {root}: "
                               f"{r.stderr.decode('utf-8', 'replace').strip()}")
        rels = [x for x in r.stdout.decode("utf-8", "replace").split("\0") if x]
        if not rels:
            # An empty index (fresh init, mid-operation) would call every pointer broken.
            raise CouldNotLook(f"git ls-files is EMPTY in {root} -- nothing to resolve against")
        repo = cls(root=root, rels=rels, **kw)
        for rel in rels:
            parts = rel.split("/")
            repo.names.update(parts)
            for i in range(1, len(parts) + 1):
                repo.paths.add("/".join(parts[:i]))
        return repo

    def files_in(self, d: str, suffix: str) -> list[str]:
        """Tracked files DIRECTLY under `d` ('' = the root) ending in `suffix`, sorted."""
        d = d.strip("/")
        return sorted(r for r in self.rels if r.endswith(suffix)
                      and (r.rsplit("/", 1)[0] if "/" in r else "") == d)

    def ignored(self, tok: str) -> bool:
        try:
            return subprocess.run(["git", "-C", str(self.root), "check-ignore", "-q", tok],
                                  capture_output=True, timeout=15, check=False).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def sibling(self, tok: str) -> tuple[bool, str | None]:
        """(is a sibling pointer, error-or-None). Present sibling -> checked; absent -> silent.
        Tests the sibling REPO ROOT, never the token's grandparent: `mcp/README.md` is two
        segments deep, so its grandparent is the workspace root, which always exists."""
        head = tok.split("/", 1)[0]
        if head not in self.siblings or head == self.root.name:
            return False, None
        base = self.sibling_base or self.root.parent
        if (base / head).exists() and not (base / tok).exists():
            return True, f"`{tok}` names a sibling-repo file that does not exist at {base / tok}"
        return True, None

    def resolves(self, tok: str, *, suffix: bool) -> bool:
        bare = tok.rstrip("/")
        if "/" not in bare:
            return bare in self.names
        if bare in self.paths:
            return True
        if suffix:
            tail = "/" + bare
            return any(p.endswith(tail) for p in self.paths)
        return False


def spans(text: str) -> list[str]:
    """The contents of every inline code span outside fenced blocks, paired within one block."""
    return [m.group(2) for block in _BLANK_LINE.split(_FENCE.sub("", text))
            for m in _SPAN.finditer(block)]


def _strip_pin(tok: str) -> str:
    """`a.py:12`, `a.py:main`, `a.py::test_x`, `a.md#setup`, `ci.yml@v1` -> the path alone."""
    for ext in _FILE_EXT:
        i = tok.find(ext)
        while i != -1:
            j = i + len(ext)
            if j < len(tok) and tok[j] in _PIN_CHARS:
                return tok[:j]
            i = tok.find(ext, i + 1)
    return tok


def normalise(raw: str) -> str:
    tok = _strip_pin(raw.strip().rstrip(".,;:)"))
    return tok[2:] if tok.startswith("./") else tok


def is_pointer(tok: str, repo: Repo) -> bool:
    if not tok or any(c.isspace() for c in tok) or _NOT_IN_A_PATH & set(tok) or "://" in tok:
        return False
    if tok.startswith(_NEVER_A_PATH_PREFIX) or (repo.exempt and tok.startswith(repo.exempt)):
        return False
    if ":" in tok or re.fullmatch(r"\.\w+", tok):
        return False
    parts = [x for x in tok.split("/") if x]
    if not parts or any(re.fullmatch(r"[A-Z]", x.split(".", 1)[0]) for x in parts):
        return False
    if any(x.replace(".", "").isdigit() for x in parts):
        return False
    if tok.endswith(_FILE_EXT):
        return True
    return "/" in tok and (parts[0] in repo.paths or parts[0] in repo.siblings)


def check_refs(text: str, label: str, repo: Repo, *, suffix: bool) -> list[str]:
    errs: list[str] = []
    seen: set[str] = set()
    for raw in spans(text):
        tok = normalise(raw)
        if tok in seen or not is_pointer(tok, repo):
            continue
        seen.add(tok)
        is_sib, err = repo.sibling(tok)
        if is_sib:
            if err:
                errs.append(f"broken pointer: {err}")
            continue
        if repo.resolves(tok, suffix=suffix) or repo.ignored(tok):
            continue
        if "/" not in tok.rstrip("/"):
            errs.append(f"broken pointer: `{tok}` is named in {label} but no file or directory "
                        f"by that name exists anywhere in this repo")
        else:
            errs.append(f"broken pointer: `{tok}` referenced in {label} but not found on disk")
    return errs


def newest_migration(repo: Repo, mig: str) -> int | None:
    nums = [int(m.group(1)) for r in repo.files_in(mig, ".sql")
            if (m := _MIGRATION.match(r.rsplit("/", 1)[-1]))]
    return max(nums) if nums else None


def check_migration_range(text: str, newest: int | None) -> list[str]:
    if newest is None:
        return []
    return [f"stale migration range '{m.group(0)}' — newest on disk is {newest:03d}. "
            "De-pin it (e.g. '001…, newest wins') so it can't rot."
            for m in _RANGE.finditer(text) if int(m.group(2)) < newest]


def check_budget(text: str, label: str, budget: int | None) -> list[str]:
    n = len(text.splitlines())
    if budget is not None and n > budget:
        return [f"{label} is {n} lines (> {budget} best-practice cap) — trim or push detail to pointers"]
    return []


def check_empty_package(repo: Repo, pkg: str) -> list[str]:
    stray = [r.rsplit("/", 1)[-1] for r in repo.files_in(pkg, ".py")
             if not r.endswith("/__init__.py")]
    if stray:
        return [f"placeholder {pkg}/ has unexpected files {stray} — keep it empty or update the "
                "manual's layer map"]
    return []


def check_ref_dir(repo: Repo, ref: str, stamp: str | None) -> list[str]:
    warns: list[str] = []
    for rel in repo.files_in(ref, ".md"):
        text = (repo.root / rel).read_text(encoding="utf-8")
        if stamp and stamp not in text:
            warns.append(f"{rel} has no `{stamp}` stamp — add one so freshness is visible")
        seen: set[str] = set()
        for raw in spans(text):
            tok = normalise(raw)
            if tok in seen or not is_pointer(tok, repo):
                continue
            seen.add(tok)
            is_sib, err = repo.sibling(tok)
            if is_sib:
                if err:
                    warns.append(f"{rel}: broken pointer {err}")
            elif not repo.resolves(tok, suffix=True) and not repo.ignored(tok):
                warns.append(f"{rel}: broken pointer `{tok}` (not on disk)")
    return warns


def check_index(repo: Repo, index: str) -> list[str]:
    if index not in repo.rels:
        raise CouldNotLook(f"--index {index} is not a tracked file")
    idx = (repo.root / index).read_text(encoding="utf-8")
    parent = index.rsplit("/", 1)[0] if "/" in index else ""
    return [f"{d} not in {index} index" for d in repo.files_in(parent, ".md")
            if d != index and d.rsplit("/", 1)[-1][:-3] not in idx]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ci-manual-lint", fromfile_prefix_chars="@",
                                description="every path the agent manual names resolves today")
    p.add_argument("--root", type=Path, help="checkout to gate (default: git toplevel of cwd)")
    p.add_argument("--manual", action="append", help="manual file, repeatable (default CLAUDE.md)")
    p.add_argument("--sibling", action="append", default=[], help="a sibling repo name")
    p.add_argument("--sibling-base", type=Path, help="where siblings live (default: root's parent)")
    p.add_argument("--exempt-prefix", action="append", default=[], help="never a path (repeatable)")
    p.add_argument("--suffix-match", action="store_true", help="a manual pointer may be a path's tail")
    p.add_argument("--migrations", help="dir of NNN_*.sql whose newest a pinned range must reach")
    p.add_argument("--line-budget", type=int, help="warn when a manual exceeds N lines")
    p.add_argument("--empty-package", action="append", default=[], help="dir that must hold no code")
    p.add_argument("--ref-dir", action="append", default=[], help="dir of docs whose pointers resolve")
    p.add_argument("--stamp", help="text every --ref-dir doc must contain")
    p.add_argument("--index", action="append", default=[], help="an index that names every sibling .md")
    return p


def run(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    env = os.environ.get("CI_KIT_ROOT")
    root = (a.root or (Path(env) if env else discover.repo_root())).resolve()
    repo = Repo.read(root, siblings=tuple(a.sibling), sibling_base=a.sibling_base,
                     exempt=tuple(a.exempt_prefix))
    newest = None
    if a.migrations:
        newest = newest_migration(repo, a.migrations)
        if newest is None:
            raise CouldNotLook(f"--migrations {a.migrations} holds no tracked NNN_*.sql")
    errs: list[str] = []
    warns: list[str] = []
    labels = a.manual or ["CLAUDE.md"]
    for label in labels:
        path = root / label
        if not path.is_file():
            raise CouldNotLook(f"no {label} at {root} -- nothing to check")
        text = path.read_text(encoding="utf-8")
        errs += check_refs(text, label, repo, suffix=a.suffix_match)
        errs += check_migration_range(text, newest)
        warns += check_budget(text, label, a.line_budget)
    for pkg in a.empty_package:
        if not any(r.startswith(pkg.strip("/") + "/") for r in repo.rels):
            raise CouldNotLook(f"--empty-package {pkg} is not tracked (a placeholder keeps an "
                               f"__init__.py): a check of nothing would report clean")
        errs += check_empty_package(repo, pkg)
    for idx in a.index:
        errs += check_index(repo, idx)
    for ref in a.ref_dir:
        if not repo.files_in(ref, ".md"):
            raise CouldNotLook(f"--ref-dir {ref} holds no tracked *.md: a check of nothing "
                               f"would report clean")
        warns += check_ref_dir(repo, ref, a.stamp)

    for w in warns:
        print(f"  warn:  {w}")
    for e in errs:
        print(f"  ERROR: {e}")
    name = labels[0] if len(labels) == 1 else "manual"
    if errs:
        print(f"\n{name} lint: {len(errs)} error(s), {len(warns)} warning(s)")
        return 1
    print(f"{name} lint: clean ({len(warns)} warning(s))")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(argv)
    except CouldNotLook as e:
        print(f"manual lint: DID NOT FINISH LOOKING (exit 2): {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
