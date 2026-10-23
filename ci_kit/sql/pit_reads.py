"""Ratchet: SQL wall-clock reads -- now() / current_date / ... -- the look-ahead footgun (`ci-pit-reads`).

A read keyed on an explicit timestamp is honest. A read keyed on the DATABASE's own clock --
now(), CURRENT_TIMESTAMP, current_date, localtimestamp, clock_timestamp() -- is deceptive under a
replay clock: it doesn't error and doesn't return None, it returns a plausible number from the
WRONG day. A NEW SQL wall-clock read fails CI so a human decides whether it needs an as-of bound.

A RATCHET, not a ban: existing sites are grandfathered as `path|token|count`. CI fails when a
file's count for a token EXCEEDS its baseline (or a new file appears); a count that shrinks
passes. A legit new site (a live-only telemetry age) takes the waiver on the clock's line,
python `#` or SQL `--` comment.

PARSED, NOT MATCHED. The reads are sqlglot nodes (`sqlast`), so a docstring, a SQL comment and a
string literal that MENTION a clock are not counted -- a regex counted all three (a comment
explaining the ratchet once tripped it) -- and a SQL value sqlglot cannot parse exits 2. Python
wall clocks (datetime.now / time.time) are not SQL and never parse as a read.
"""

from __future__ import annotations

import argparse
import dataclasses
import pathlib
import sys
from collections import Counter
from collections.abc import Sequence

from sqlglot import exp

from ci_kit import astread, discover, site_lint
from ci_kit.ratchet import RatchetCount
from ci_kit.sql import _cli, sqlast

#: the SQL wall clocks as sqlglot's postgres dialect parses them. `now()` and
#: `current_timestamp` are ONE node type, told apart by the source text the node spans.
_NODES: dict[type[exp.Expression], str] = {
    exp.CurrentDate: "current_date",
    exp.Localtimestamp: "localtimestamp",
}
_ANON = {"clock_timestamp": "clock_timestamp()"}

HEADER = (
    "# SQL wall-clock read baseline — see {doc}.\n"
    "# Grandfathered now()/current_date/... sites per file|token|count. A count that\n"
    "# GROWS (or a new file) fails CI so a human decides whether the read needs an asof\n"
    "# bound. Regenerate: {cmd} --write-baseline\n"
)


@dataclasses.dataclass(frozen=True)
class Config:
    root: pathlib.Path = dataclasses.field(default_factory=pathlib.Path.cwd)
    scan_dirs: tuple[str, ...] = ()
    exclude: tuple[pathlib.Path, ...] = ()
    baseline: pathlib.Path = pathlib.Path("pit_reads_baseline.txt")
    waiver: str = "pit-ok"
    gate: str = "ci-pit-reads"
    cmd: str = "ci-pit-reads"
    doc: str = "ci_kit/sql/pit_reads.py"
    header: str = HEADER

    def ratchet(self) -> RatchetCount:
        path = self.baseline if self.baseline.is_absolute() else self.root / self.baseline
        return RatchetCount(path, header=self.header.format(doc=self.doc, cmd=self.cmd))

    def rel(self, p: pathlib.Path) -> str:
        return p.relative_to(self.root).as_posix()


def iter_files(cfg: Config) -> list[pathlib.Path]:
    """Tracked .py under `cfg.scan_dirs` (`git ls-files`, never a walk); a dir yielding none exits 2."""
    rels: list[str] = []
    for d in cfg.scan_dirs:
        got = discover.tracked((d,), root=cfg.root)
        discover.require_scanned(len(got), gate=f"{cfg.gate}[{d}]", dirs=(d,), floor=1)
        rels.extend(got)
    skip = {p.resolve() for p in cfg.exclude}
    return [p for r in rels if (p := cfg.root / r).resolve() not in skip]


def _token(site: sqlast.Site, node: exp.Expression) -> str | None:
    if isinstance(node, exp.CurrentTimestamp):
        start = node.meta.get("start")
        spelled = site.text[start:start + 3].lower() if start is not None else ""
        return "now()" if spelled == "now" else "current_timestamp"
    if isinstance(node, exp.Anonymous):
        return _ANON.get(str(node.this).lower())
    return _NODES.get(type(node))


def scan_text(relpath: str, text: str, cfg: Config) -> Counter[str]:
    """Count SQL wall-clock reads in one module. Keys: 'relpath|token'.

    The waiver covers the clock's line. A module or SQL value that will not parse exits 2."""
    counts: Counter[str] = Counter()
    for site in sqlast.module_sites(relpath, text, gate=cfg.gate):
        for stmt in site.statements:
            for node in stmt.walk():
                token = _token(site, node)
                if token and not site.waived(cfg.waiver, node):
                    counts[f"{relpath}|{token}"] += 1
    return counts


def scan_files(files: Sequence[pathlib.Path], cfg: Config) -> Counter[str]:
    total: Counter[str] = Counter()
    for p in files:
        rel = cfg.rel(p)
        total.update(scan_text(rel, astread.read_text(p, gate=cfg.gate, origin=rel), cfg))
    return total


def scan(cfg: Config) -> Counter[str]:
    return scan_files(iter_files(cfg), cfg)


def problems(counts: Counter[str], baseline: dict[str, int], cfg: Config) -> list[str]:
    out: list[str] = []
    for key, n in sorted(counts.items()):
        allowed = baseline.get(key, 0)
        if n > allowed:
            relpath, token = key.split("|", 1)
            out.append(
                f"[PIT] {relpath}: {n} x SQL {token} (baseline {allowed}).\n"
                f"        A wall-clock read returns a plausible number from the WRONG day under a\n"
                f"        replay clock. Bind an asof param (defaulted to now() in PYTHON) instead.\n"
                f"        If genuinely live-only: add '# {cfg.waiver}' / '-- {cfg.waiver}' on the line, or\n"
                f"        run: {cfg.cmd} --write-baseline")
    return out


def check(cfg: Config) -> list[str]:
    return problems(scan(cfg), cfg.ratchet().load(), cfg)


def selftest(cfg: Config) -> list[str]:
    """Both directions, on sources built here."""
    fails: list[str] = []
    key = "f.py|now()"
    nl = chr(10)
    w = cfg.waiver

    def scan_(src: str) -> Counter[str]:
        return scan_text("f.py", src, cfg)

    if scan_('SQL = "SELECT * FROM t WHERE ts < now()"').get(key) != 1:
        fails.append("a bare SQL now() was not counted")
    every = scan_('q("SELECT now(), current_timestamp, current_date, '
                  'localtimestamp, clock_timestamp() FROM t")')
    if len(every) != 5 or set(every.values()) != {1}:
        fails.append(f"the five wall clocks did not count once each: {dict(every)}")

    if scan_('SQL = "SELECT now()"  # ' + w).get(key):
        fails.append(f"a {w} waiver did not suppress the count")
    sql_waived = scan_("q('''" + nl + "SELECT now()  -- " + w + nl + "     , now() FROM t''')")
    if sql_waived.get(key) != 1:
        fails.append(f"a SQL-comment {w} must waive ITS line only")

    if scan_("x = dt.now()"):
        fails.append("a python .now() (a method, not the SQL builtin) was counted")

    # THE SHAPES A REGEX COUNTED: prose in a docstring, a clock inside a SQL string literal, a
    # clock in a SQL comment.
    mentions = ('"""Adding this would be two more SQL now() calls."""' + nl
                + "q(" + repr("SELECT 'now()' AS s FROM t -- was current_date") + ")")
    if scan_(mentions):
        fails.append("a docstring / string-literal / comment mention was counted as a read")
    # ...and one it had to count: a clock inside a CTE IS a read.
    if scan_('q("WITH w AS (SELECT now() AS t) SELECT t FROM w")').get(key) != 1:
        fails.append("a wall clock inside a CTE was not counted")
    # Review found: a value OPENING with a comment was skipped whole, and a waiver on one line
    # of a concatenated literal waived every clock in it.
    if scan_("q(" + repr("-- live" + nl + "SELECT now() FROM t") + ")").get(key) != 1:
        fails.append("a SQL value opening with a comment was not read")
    concat = 'q("SELECT now() "  # ' + w + nl + '  "     , now() FROM t")'
    if scan_(concat).get(key) != 1:
        fails.append(f"a {w} on one line of a concatenated literal must waive ITS line only")

    blind = _cli.blind_probe(lambda: scan_("q(" + repr("".join(("SEL", "ECT FROM WHERE now("))) + ")"),
                             missed="an unparseable SQL value did not exit 2",
                             wrong="an unparseable SQL value exited {code}, not 2")
    return fails + ([blind] if blind else [])


def main(argv: Sequence[str] | None = None, cfg: Config | None = None, *,
         description: str | None = None) -> int:
    """The CLI, on `site_lint.check`. A repo that wraps this passes its own `cfg` (and docstring)."""
    ap = argparse.ArgumentParser(description=description or __doc__)
    ap.add_argument("--write-baseline", action="store_true",
                    help="re-freeze the baseline to the current tree")
    ap.add_argument("--selftest", action="store_true", help="prove the rule can fire")
    if cfg is None:
        ap.add_argument("--root", type=pathlib.Path, default=pathlib.Path.cwd())
        ap.add_argument("--scan-dir", action="append", default=[], metavar="DIR")
        ap.add_argument("--baseline", type=pathlib.Path, default=Config.baseline)
    args = ap.parse_args(argv)
    if cfg is None:
        cfg = Config(root=args.root.resolve(), scan_dirs=tuple(args.scan_dir), baseline=args.baseline)
        if not cfg.scan_dirs and not args.selftest:
            print(f"{cfg.gate}: no --scan-dir given -- nothing to read (exit 2)", file=sys.stderr)
            return 2
    c = cfg

    def write() -> int:
        counts = scan(c)
        ratchet = c.ratchet()
        ratchet.write(counts)
        print(f"[{c.gate}] wrote {len(counts)} baseline keys -> {c.rel(ratchet.path)}")
        return 0

    def report(found: Sequence[str]) -> None:
        if found:
            print(f"[{c.gate}] SQL wall-clock ratchet FAILED:\n", file=sys.stderr)
            for p in found:
                print(f"  {p}", file=sys.stderr)
            print(f"\n[{c.gate}] {len(found)} problem(s). See {c.doc}.", file=sys.stderr)
        else:
            print(f"[{c.gate}] OK — no new SQL wall-clock reads beyond the baseline.")

    return site_lint.check(
        _cli.flags(args, "selftest", "write_baseline"), gate=c.gate, scan=lambda: check(c),
        selftest=lambda: selftest(c), selftest_report=_cli.selftest_report(f"[{c.gate}] selftest"),
        modes={"--write-baseline": write}, report=report)


if __name__ == "__main__":
    raise SystemExit(main())
