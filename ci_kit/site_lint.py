"""The per-file SITE-COUNT lint, defined once: scan, fixture selftest, CLI.

A site lint is a predicate `findings_in(source, origin) -> [line]` plus mechanics every such gate
shares: a per-file count ratchet (`RatchetCount`), `{bad,good}.py` fixtures with `# BAD`
markers, and exit 0 clean / 1 grown / 2 could not look. A second site lint written as a copy of the
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
from collections.abc import Callable, Iterable, Sequence
from typing import NoReturn

from ci_kit import discover
from ci_kit.ratchet import RatchetCount

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


def run(argv: Sequence[str], *, gate: str, scan: Callable[[], dict[str, int]],
        scanned: Callable[[], int], scan_dirs: Sequence[str], ratchet: RatchetCount,
        selftest: Callable[[], list[str]], gained: str, advice: Sequence[str],
        floor: int = 100) -> int:
    """The CLI: `--selftest`, `--write`, or check. Exit 0 clean - 1 findings - 2 could not look.

    `floor` is the fewest files the scan must have read (see `discover.require_scanned`); 100 is
    what the first two site lints over a `service/` tree shipped with.
    """
    if "--selftest" in argv:
        fails = selftest()
        for f in fails:
            print(f"  SELFTEST FAIL: {f}")
        print(f"{gate}: selftest {'FAILED' if fails else 'OK'}")
        return 1 if fails else 0
    try:
        counts = scan()
    except Exception as e:                              # noqa: BLE001 - exit 2 IS the report
        print(f"{gate}: DID NOT FINISH LOOKING - {type(e).__name__}: {e}")
        return 2
    discover.require_scanned(scanned(), gate=gate, dirs=scan_dirs, floor=floor)
    if "--write" in argv:
        n = ratchet.write(counts)
        print(f"{gate}: baseline written, {n} file(s), {sum(counts.values())} site(s)")
        return 0
    grown = ratchet.regressions(counts)
    if grown:
        print(f"{gate}: {len(grown)} file(s) gained {gained}:")
        for rel, now, base in grown:
            print(f"  {rel}: {now} site(s), baseline {base}")
        for line in advice:
            print(f"  {line}")
        return 1
    slack = ratchet.shrunk(counts)
    tail = f", {len(slack)} file(s) shrank - tighten with --write" if slack else ""
    print(f"{gate}: OK - {scanned()} scanned, {sum(counts.values())} grandfathered site(s) "
          f"in {len(counts)} file(s), none new{tail}")
    return 0
