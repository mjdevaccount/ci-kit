"""The per-file SITE-COUNT lint, defined once: scan, fixture selftest, CLI.

A site lint is a predicate `findings_in(source, origin) -> [line]` plus mechanics every such gate
shares: a per-file count ratchet (`RatchetCount`) or a set-of-keys one (`RatchetSet`),
`{bad,good}.py` fixtures with `# BAD` markers, and exit 0 clean / 1 grown / 2 could not look. A second site lint written as a copy of the
first is what a clone gate refuses, so the mechanics live here and
each lint keeps its predicate, its baseline header and its voice (the advice printed on a
regression).

READING. `count_sites` takes an optional `read(path, rel) -> str` and defaults to `read_source`
below, which keeps the two properties that mattered: BOM-tolerant (`utf-8-sig`, so a BOM'd file is
not mistaken for broken code) and exit 2 on a file it cannot decode -- never a silent skip.
"""

from __future__ import annotations

import collections
import pathlib
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, NoReturn

from ci_kit import discover
from ci_kit.ratchet import RatchetCount, RatchetSet

FindingsIn = Callable[[str, str], list[int]]
Reader = Callable[[pathlib.Path, str], str]

#: BOM-tolerant on purpose: `utf-8` turns a BOM'd file into a SyntaxError that looks like broken code.
ENCODING = "utf-8-sig"


def _blind(rel: str, exc: BaseException, *, gate: str) -> NoReturn:
    print(f"{gate}: DID NOT FINISH LOOKING -- {rel}: {type(exc).__name__}: {exc}", file=sys.stderr)
    raise SystemExit(2)


def read_source(path: pathlib.Path, rel: str, *, gate: str = "site-lint") -> str:
    """The file's text decoded `utf-8-sig`, or exit 2 naming it."""
    try:
        return path.read_text(encoding=ENCODING)
    except (OSError, UnicodeDecodeError) as exc:
        _blind(rel, exc, gate=gate)


def count_sites(root: pathlib.Path, scan_dirs: Sequence[str], exempt: Iterable[str],
                findings_in: FindingsIn, *, gate: str,
                read: Reader | None = None) -> tuple[dict[str, int], int]:
    """({tracked file: site count} for files with any, number of files scanned)."""
    skip = set(exempt)
    counts: dict[str, int] = collections.Counter()
    files = [f for f in discover.tracked(scan_dirs, root=root) if f not in skip]
    for rel in files:
        text = read(root / rel, rel) if read else read_source(root / rel, rel, gate=gate)
        n = len(findings_in(text, rel))
        if n:
            counts[rel] = n
    return dict(counts), len(files)


def fixture_selftest(root: pathlib.Path, fixtures: pathlib.Path, findings_in: FindingsIn,
                     *, noun: str) -> list[str]:
    """Both directions, on the checked-in fixtures: every `# BAD` line of bad.py is flagged and
    nothing else there is; good.py is silent."""
    fails: list[str] = []
    bad_p, good_p = fixtures / "bad.py", fixtures / "good.py"
    for p in (bad_p, good_p):
        if not p.exists():
            return [f"fixture missing: {p.relative_to(root).as_posix()}"]
    bad = bad_p.read_text(encoding="utf-8")
    want = [i for i, ln in enumerate(bad.splitlines(), 1) if ln.rstrip().endswith("# BAD")]
    got = findings_in(bad, "bad.py")
    if not want:
        fails.append("bad.py has no `# BAD` lines - the fixture proves nothing")
    for ln in sorted(set(want) - set(got)):
        fails.append(f"MISSED {noun} at bad.py:{ln}")
    for ln in sorted(set(got) - set(want)):
        fails.append(f"flagged an unmarked line bad.py:{ln}")
    for ln in findings_in(good_p.read_text(encoding="utf-8"), "good.py"):
        fails.append(f"FALSE POSITIVE at good.py:{ln}")
    return fails


def run(argv: Sequence[str], *, gate: str, scan: Callable[[], Mapping[str, int] | Iterable[str]],
        scanned: Callable[[], int], scan_dirs: Sequence[str], ratchet: RatchetCount | RatchetSet,
        selftest: Callable[[], list[str]], gained: str, advice: Sequence[str],
        floor: int = 100, selftest_fail: int = 1, write_flags: Sequence[str] = ("--write",),
        header: Callable[[int], str] | None = None, describe: Callable[..., str] | None = None,
        ok: Callable[[Any, list[Any]], str] | None = None,
        written: Callable[[Any], str] | None = None,
        hard: Callable[[Any], list[str]] | None = None) -> int:
    """The CLI: `--selftest`, `--write`, or check. Exit 0 clean - 1 findings - 2 could not look.

    `floor` is the fewest files the scan must have read (see `discover.require_scanned`); 100 is
    what the first two site lints over a `service/` tree shipped with.

    TWO RATCHET SHAPES, ONE CLI. With a `RatchetCount`, `scan()` returns {key: count} and a new
    key or a grown count fails. With a `RatchetSet`, `scan()` returns the current keys (any
    iterable -- a mapping counts by its keys) and a key the baseline does not list fails.
    Everything else is the same: the selftest flag, exit 2 on a crashed scan, the floor, the write,
    the shrink note. A check that must refuse beyond the ratchet (a second floor, a missing input)
    raises from `scan` -- `SystemExit(2)` passes through, any other exception is reported as 2.

    THE VOICE STAYS THE CALLER'S. Every hook is optional and has the default this module always
    printed:
      header(n)          the line above the regressions
      describe(...)      one regression: (key, now, base) for a count ratchet, (key) for a set
      ok(current, slack) the clean line; slack = `shrunk()` rows (count) or gone keys (set)
      written(current)   the line after a baseline write
    `hard(current)` returns findings NO baseline can grant (a waiver with no reason, a baselined key
    that vanished from the scan): each is printed and fails the run alongside the regressions.
    `selftest_fail` is the exit code of a failing `--selftest` (gates that shipped 2 keep 2);
    `write_flags` are the spellings that re-freeze the baseline (`--write`, `--write-baseline`).
    """
    if "--selftest" in argv:
        fails = selftest()
        for f in fails:
            print(f"  SELFTEST FAIL: {f}")
        print(f"{gate}: selftest {'FAILED' if fails else 'OK'}")
        return selftest_fail if fails else 0
    try:
        current = scan()
    except Exception as e:                              # noqa: BLE001 - exit 2 IS the report
        print(f"{gate}: DID NOT FINISH LOOKING - {type(e).__name__}: {e}")
        return 2
    discover.require_scanned(scanned(), gate=gate, dirs=scan_dirs, floor=floor)
    is_set = isinstance(ratchet, RatchetSet)
    if any(f in argv for f in write_flags):
        n = ratchet.write(set(current) if is_set else current)  # type: ignore[arg-type]
        print(written(current) if written else _written(gate, n, current, is_set=is_set))
        return 0
    if is_set:
        keys = set(current)
        grown: list[Any] = ratchet.regressions(keys)
        slack: list[Any] = sorted(ratchet.load() - keys)
    else:
        grown = ratchet.regressions(current)  # type: ignore[arg-type]
        slack = ratchet.shrunk(current)  # type: ignore[arg-type,union-attr]
    unbaselined = hard(current) if hard else []
    if grown or unbaselined:
        if grown:
            print(header(len(grown)) if header else (
                f"{gate}: {len(grown)} new {gained} the baseline does not list:" if is_set
                else f"{gate}: {len(grown)} file(s) gained {gained}:"))
        for row in grown:
            print(f"  {_describe(row, describe, is_set=is_set)}")
        for line in unbaselined:
            print(f"  {line}")
        for line in advice:
            print(f"  {line}")
        return 1
    print(ok(current, slack) if ok else _ok(gate, scanned(), current, slack, write_flags[0], is_set=is_set))
    return 0


def _written(gate: str, n: int, current: Any, *, is_set: bool) -> str:
    if is_set:
        return f"{gate}: baseline written, {n} key(s)"
    return f"{gate}: baseline written, {n} file(s), {sum(current.values())} site(s)"


def _describe(row: Any, describe: Callable[..., str] | None, *, is_set: bool) -> str:
    if is_set:
        return describe(row) if describe else str(row)
    rel, now, base = row
    return describe(rel, now, base) if describe else f"{rel}: {now} site(s), baseline {base}"


def _ok(gate: str, scanned: int, current: Any, slack: list[Any], write_flag: str, *, is_set: bool) -> str:
    if is_set:
        tail = f", {len(slack)} baseline key(s) gone - tighten with {write_flag}" if slack else ""
        return f"{gate}: OK - {scanned} scanned, {len(set(current))} grandfathered, none new{tail}"
    tail = f", {len(slack)} file(s) shrank - tighten with {write_flag}" if slack else ""
    return (f"{gate}: OK - {scanned} scanned, {sum(current.values())} grandfathered site(s) "
            f"in {len(current)} file(s), none new{tail}")
