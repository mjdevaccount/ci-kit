"""ci-line-cap: a module over the line cap may not appear, and one already over may not grow.

MOVED FROM trade-data (v1.49.0, `scripts/lint_line_cap.py`). There a 400-line cap was stated as
HARD in three places and enforced nowhere: measured 2026-09-18, 47 of 859 modules were over it,
7,891 lines over in total, the worst at 1,086. A rule violated 47 times without one failing build
is a preference, and worse than no rule, because review keeps citing it with authority the check
never earned.

A RATCHET, NOT A BAN. A gate that fails on day one is bypassed on day one, so the baseline is the
current count of every module already over, and only GROWTH fails: a new module over the cap, or
a grandfathered one getting longer. Shrinking is never a failure; the OK line says how much slack
the baseline can give back (`--write-baseline` in the same PR).

PHYSICAL LINES, not statements and not lines-minus-comments: the cap exists so a module stays
readable in one sitting, and counting only code would make documentation a way to dodge it.

EVERY RULED DIR HAS ITS OWN FLOOR (`--dir DIR:FLOOR`). The total hides a directory the scan read
nothing from: `service/` alone clears any total whether `sources/` was read or not, and in
trade-data the six most failure-prone daemons were all over the cap and all invisible to it.

NO DIRECTORY IS SILENTLY OUT OF SCOPE. Tracked python under a top-level directory that is neither
ruled (`--dir`) nor excused with a reason (`--unruled DIR:WHY`; `.` names repo-root files) is
exit 2: trade-data's `flows/` grew from 230 to 616 lines because nothing made a NEW directory
declare itself. Omit every `--unruled` and this check is off.

  ci-line-cap --dir service:100 [--dir DIR[:FLOOR] ...] [--cap 400] [--unruled DIR:WHY ...]
              [--baseline ci/line_cap_baseline.txt] [--write-baseline] [--root DIR] [--selftest]
  (`@FILE` reads one argument per line; `--write` is an alias of `--write-baseline`.)

Exit 0 clean, 1 grown or new, 2 could not look (a ruled dir under its floor, an unruled one).
"""

from __future__ import annotations

import pathlib
import sys
from collections.abc import Mapping, Sequence

from ci_kit import discover, site_lint
from ci_kit.ratchet import RatchetCount, kept_header

GATE = "line-cap"
HEADER = (
    "# line-cap baseline: per-file PHYSICAL line counts of modules over the cap (`ci-line-cap`).\n"
    "# SHRINK-ONLY. A grown or new file fails CI; shrink = tighten this file in the same PR.\n"
    "# Re-freeze: ci-line-cap ... --write-baseline (a RAISED number needs a reason in the PR).\n"
)


def physical_lines(path: pathlib.Path) -> int:
    """Physical lines, the last one counted with or without a trailing newline."""
    with path.open(encoding="utf-8", errors="replace") as fh:
        return sum(1 for _ in fh)


def over_cap(lengths: Mapping[str, int], cap: int) -> dict[str, int]:
    """{path: lines} for every module OVER the cap. Under-cap files are absent, so a module that
    drops below it shows up as slack and its baseline entry can go."""
    return {rel: n for rel, n in lengths.items() if n > cap}


def unruled_dirs(paths: Sequence[str], ruled: Sequence[str], unruled: Mapping[str, str]) -> list[str]:
    """Top-level dirs of `paths` neither ruled nor excused (`.` = a repo-root file). A ruled `.`
    covers every directory."""
    if "." in ruled:
        return []
    tops = {p.split("/", 1)[0] if "/" in p else "." for p in paths}
    return sorted(t for t in tops if t not in ruled and t not in unruled)


def _unruled(values: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for v in values:
        d, sep, why = v.partition(":")
        if not sep or len(why.strip()) < 8:
            print(f"{GATE}: --unruled {v!r} needs DIR:WHY with a reason (>= 8 chars) -- an excuse "
                  f"nobody had to write is how a directory goes dark", file=sys.stderr)
            raise SystemExit(2)
        out[site_lint.norm_dir(d)] = why.strip()
    return out


def selftest() -> list[str]:
    import contextlib
    import io
    import tempfile
    fails: list[str] = []
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "probe.py"
        p.write_text("a\nb\nc", encoding="utf-8", newline="")
        if physical_lines(p) != 3:
            fails.append(f"a file with no trailing newline counted {physical_lines(p)}, not 3")
        base = RatchetCount(pathlib.Path(d) / "b.txt", HEADER)
        base.write({"service/big.py": 450})
        if base.regressions(over_cap({"service/big.py": 450, "service/ok.py": 400}, 400)):
            fails.append("a grandfathered module at its baseline, or one AT the cap, was a regression")
        if base.regressions({"service/big.py": 451}) != [("service/big.py", 451, 450)]:
            fails.append("a grandfathered module that GREW was not a regression")
        if base.regressions({"sources/new.py": 401}) != [("sources/new.py", 401, 0)]:
            fails.append("a NEW module over the cap was not a regression")
        if base.shrunk({"service/big.py": 420}) != [("service/big.py", 420, 450)]:
            fails.append("a shrunk module was not reported as slack")
    if unruled_dirs(["flows/h.py", "service/x.py", "tests/t.py", "setup.py"], ["service"],
                    {"tests": "why", ".": "why"}) != ["flows"]:
        fails.append("a python file under a new top-level dir was not reported as unruled")
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            discover.require_scanned_each({"service": 120, "sources": 0}, {"service": 100, "sources": 10}, gate=GATE)
        fails.append("a ruled dir that read nothing did not exit at all")
    except SystemExit as e:
        if e.code != 2:
            fails.append(f"a ruled dir that read nothing exited {e.code}, not 2")
    return fails


def main(argv: Sequence[str] | None = None) -> int:
    ap = site_lint.gate_parser("ci-line-cap", __doc__, baseline="ci/line_cap_baseline.txt")
    ap.add_argument("--cap", type=int, default=400)
    ap.add_argument("--unruled", action="append", default=[], metavar="DIR:WHY")
    a, root, floors = site_lint.parse_gate(ap, argv, gate=GATE, selftest=selftest)
    unruled = _unruled(a.unruled)
    baseline = site_lint.under(root, a.baseline)
    seen: dict[str, int] = {}

    def scan() -> dict[str, int]:
        files = discover.tracked(floors, root=root)
        by_dir = discover.count_by_dir(files, floors)
        discover.require_scanned_each(by_dir, floors, gate=GATE)
        if unruled:
            stray = unruled_dirs(discover.tracked(["."], root=root), list(floors), unruled)
            if stray:
                print(f"{GATE}: tracked python under {stray} is neither ruled (--dir) nor excused "
                      f"(--unruled DIR:WHY) -- rule it or excuse it with a reason", file=sys.stderr)
                raise SystemExit(2)
        seen.update(by_dir, total=len(files))
        return over_cap({rel: physical_lines(root / rel) for rel in files}, a.cap)

    def ok(cur: dict[str, int], slack: list[tuple[str, int, int]]) -> str:
        tail = (f", {sum(b - n for _, n, b in slack):,} line(s) of slack across {len(slack)} module(s) "
                f"-- tighten with --write-baseline in the same PR") if slack else ""
        dirs = ", ".join(f"{d}/={seen.get(d, 0)}" for d in floors)
        return (f"{GATE}: OK -- {seen['total']} module(s) [{dirs}], {len(cur)} over the {a.cap}-line cap "
                f"and none growing, {sum(v - a.cap for v in cur.values()):,} line(s) over in total{tail}")

    def describe(rel: str, now: int, base: int) -> str:
        if base == 0:
            return f"NEW over the cap: {rel} is {now} lines. Split it, or baseline it with a reason in the PR."
        return f"GREW: {rel} is {now} lines, baseline {base}. It was already over the cap; it may not get longer."

    return site_lint.run(
        ["--write"] if a.write else [], gate=GATE, scan=scan, scanned=lambda: seen.get("total", 0),
        scan_dirs=list(floors), ratchet=RatchetCount(baseline, kept_header(baseline, HEADER)),
        selftest=selftest, gained="lines over the cap", advice=(), floor=1, describe=describe, ok=ok,
        header=lambda n: f"{GATE}: {n} module(s) over the {a.cap}-line cap and growing ({seen['total']} scanned)",
        written=lambda c: (f"{GATE}: baseline written with {len(c)} module(s) over the {a.cap}-line cap, "
                           f"{sum(v - a.cap for v in c.values()):,} line(s) over in total"))


if __name__ == "__main__":
    sys.exit(main())
