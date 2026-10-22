"""The asyncpg CAST-on-str footgun, made CI-catchable (`ci-sql-casts`).

asyncpg infers a bind param's wire type from the SQL CAST that wraps it, so binding a python
*str* into `CAST(:x AS timestamptz)` makes asyncpg encode the str as a timestamptz and blow up at
RUNTIME ("expected a datetime, got str") -- only on the real asyncpg path, never in a unit test
that does not touch a DB. Four shapes (A and C hard-fail; B and D are shrink-only ratchets):

  RULE A  `:x::type`        a bind adjacent to a `::` cast. SQLAlchemy reads `::` as bind syntax
                            and leaves `:x` unconverted -> PostgresSyntaxError. ALWAYS a bug.
  RULE B  `CAST(:x AS t)`   t a type a str cannot be binary-encoded as (timestamptz, timestamp,
                            date, time, interval, numeric). Legitimate when the caller binds a
                            typed object, so a RATCHET: known sites are baselined, a NEW one fails.
  RULE C  `now() - :age`    the SQL clock left of `+`/`-` with a bare bind on the right. asyncpg
                            sends the bind as `unknown`: "operator is not unique: date + unknown"
                            on the first live call. ALWAYS a bug.
  RULE D  `:x + interval`   the mirror of C: a bind left of `+`/`-` against `interval '..'` or
                            `make_interval(..)`. Safe only if ANOTHER context in the statement
                            types the bind (a comparison, BETWEEN, generate_series, COALESCE) --
                            open-ended, so a RATCHET. Unresolved, it is SQLSTATE 42883 on the
                            first live call (one took a watchdog's verdicts dark for ~36 hours).
                            Deliberately NOT matched (known misses, PREPARE against a migrated
                            database closes them): the mirrored `interval '1 day' + :x`, and a
                            parenthesised or column/subquery right operand.

HOW IT READS SQL. B, C and D are questions about sqlglot's postgres AST (`sqlast`): a `Cast` whose
operand is a bind, an `Add`/`Sub` whose left is the clock or a bind. A is a question about TOKENS:
`:x::date` and `CAST(:x AS date)` are the same node, so the `::` spelling is only visible there.
A regex over lines read a `-- CAST(:x AS date)` comment, a `':x::date'` string literal and a
docstring sentence as code, and missed a statement wrapped across lines. A SQL value sqlglot
cannot parse exits 2.

Fixes: bind a real typed object (dt.datetime / dt.date), or double-cast through text so asyncpg
sends text and Postgres casts server-side: `CAST(CAST(:x AS text) AS timestamptz)`.

WAIVERS: baseline the site (`--write-baseline --rule b|d`, after review) or put the waiver token
on the node's line, as a python `#` or SQL `--` comment. RULE D HAS ITS OWN TOKEN, and that is
load-bearing: the fix for a D finding is a CAST, itself a B flag, so one shared token made every
line FIXED for D invisible TO D -- starting with the line that caused the outage.
"""

from __future__ import annotations

import argparse
import dataclasses
import pathlib
import sys
from collections.abc import Sequence

from sqlglot import exp
from sqlglot.tokens import TokenType

from ci_kit import astread, discover
from ci_kit.ratchet import RatchetSet
from ci_kit.sql import sqlast

#: RULE B's target types: ones a python str can't be binary-encoded as. text/varchar/json/jsonb/
#: array/vector are EXCLUDED -- asyncpg encodes a str for those fine. Values are the key spelling,
#: which is the Postgres name (sqlglot calls numeric DECIMAL).
FOOTGUN_TYPES = {
    exp.DataType.Type.TIMESTAMPTZ: "timestamptz", exp.DataType.Type.TIMESTAMP: "timestamp",
    exp.DataType.Type.TIMETZ: "timetz", exp.DataType.Type.TIME: "time",
    exp.DataType.Type.DATE: "date", exp.DataType.Type.INTERVAL: "interval",
    exp.DataType.Type.DECIMAL: "numeric",
}
#: RULE C's left operand: the SQL clock. `now()` and `current_timestamp` are one node.
CLOCKS = (exp.CurrentDate, exp.CurrentTimestamp, exp.Localtimestamp)

_TEMPORAL = "[temporal arithmetic on a bare bind]"

B_HEADER = (
    "# asyncpg CAST-on-str baseline — see {doc}.\n"
    "# Grandfathered CAST(:p AS <temporal/numeric type>) sites (Rule B). Each is a\n"
    "# bind whose target type a Python str can't be encoded as; these are known-safe\n"
    "# (typed-object bind or Python-side normalizer). A NEW key not listed here fails\n"
    "# CI so a human reviews it. Regenerate: {cmd} --write-baseline\n"
)
D_HEADER = (
    "# asyncpg bind-on-the-left interval arithmetic baseline (Rule D)."
    "  See {doc}.\n"
    "# Each key is a site where a bind is an operand of `+`/`-` against an interval, e.g.\n"
    "# `:until + interval '1 day'`. These are SAFE ONLY because something else in the same\n"
    "# statement gives the planner a type for that bind -- a bare comparison, a BETWEEN\n"
    "# operand, a generate_series argument, a COALESCE against a typed column. That set is\n"
    "# open-ended, which is why this is a ratchet and not a hard fail: a NEW key fails CI so\n"
    "# a human checks whether the bind is resolvable. The shape with nothing resolving it is\n"
    "# SQLSTATE 42883 on the first live call -- it took the watchdog VERDICT plane dark for\n"
    "# ~36 hours on 2026-09-17 (its PAGING plane stayed up and delivered 17 pages). Fix a\n"
    "# site by wrapping the bind: CAST(:x AS timestamptz),\n"
    "# which also drops it from this population. Regenerate:\n"
    "#   {cmd} --write-baseline\n"
)


@dataclasses.dataclass(frozen=True)
class Config:
    """Where to look and how to speak. Detection never reads anything but `waiver`/`d_waiver`."""

    root: pathlib.Path = dataclasses.field(default_factory=pathlib.Path.cwd)
    scan_dirs: tuple[str, ...] = ()
    #: COVERAGE IS ITSELF A RATCHET: `scan_dirs` may grow and may not shrink past this. A
    #: per-directory floor catches a declared directory that yields nothing (the ENVIRONMENT
    #: failing); it does not catch the DECLARATION shrinking, which once printed OK with 81% of a
    #: tree unread.
    required_dirs: tuple[str, ...] = ()
    #: resolved paths never scanned -- a gate whose own docstring carries the example patterns
    exclude: tuple[pathlib.Path, ...] = ()
    baseline: pathlib.Path = pathlib.Path("sql_casts_baseline.txt")
    d_baseline: pathlib.Path = pathlib.Path("sql_bind_arith_baseline.txt")
    waiver: str = "asyncpg-cast-ok"
    d_waiver: str = "asyncpg-bind-ok"
    gate: str = "ci-sql-casts"
    cmd: str = "ci-sql-casts"          # how a human re-runs it: "<cmd> --write-baseline"
    doc: str = "ci_kit/sql/casts.py"   # where a failure points for the fix + waiver docs
    b_header: str = B_HEADER
    d_header: str = D_HEADER

    def ratchets(self) -> tuple[RatchetSet, RatchetSet]:
        fmt = {"doc": self.doc, "cmd": self.cmd}
        return (RatchetSet(self._abs(self.baseline), header=self.b_header.format(**fmt)),
                RatchetSet(self._abs(self.d_baseline), header=self.d_header.format(**fmt)))

    def rel(self, p: pathlib.Path) -> str:
        return p.relative_to(self.root).as_posix()

    def _abs(self, p: pathlib.Path) -> pathlib.Path:
        return p if p.is_absolute() else self.root / p


def iter_files(cfg: Config) -> list[pathlib.Path]:
    """Tracked .py under `cfg.scan_dirs` (`git ls-files`, never a walk). EVERY DECLARED DIRECTORY
    MUST CONTRIBUTE, checked one at a time with a floor of ONE: the gate cannot know how many
    files a directory ought to hold, but one it was told to scan that yielded nothing means it
    did not finish looking."""
    missing = [d for d in cfg.required_dirs if d not in cfg.scan_dirs]
    if missing:
        print(f"{cfg.gate}: SCAN_DIRS no longer covers {missing} — coverage may grow and may not "
              f"shrink (see REQUIRED_DIRS). Exit 2: this gate did not look where it must.",
              file=sys.stderr)
        raise SystemExit(2)
    rels: list[str] = []
    for d in cfg.scan_dirs:
        got = discover.tracked((d,), root=cfg.root)
        discover.require_scanned(len(got), gate=f"{cfg.gate}[{d}]", dirs=(d,), floor=1)
        rels.extend(got)
    skip = {p.resolve() for p in cfg.exclude}
    return [p for r in rels if (p := cfg.root / r).resolve() not in skip]


def ruleb_key(relpath: str, name: str, type_: str) -> str:
    """Stable baseline key: file + canonical snippet (line-number independent)."""
    return f"{relpath}|CAST(:{name} AS {type_})"


def ruled_key(relpath: str, name: str, op: str, kind: str) -> str:
    """Stable Rule-D key: file + bind + operator + the right operand's KIND.

    File and bind NAME alone was too coarse: the most generic names (`:t`, `:ts`, `:now`) would
    let one baselined key permit EVERY future `:t +/- interval` in that file. The kind is
    `interval` or `make_interval`, so retuning a window is not a new site; a new operator is."""
    return f"{relpath}|:{name} {op} {kind}"


def rule_a(site: sqlast.Site) -> list[int]:
    """SQL lines where a bind is spelled `:x::type`. TOKENS, not the tree: the AST cannot tell
    `:x::date` from `CAST(:x AS date)`, and only the first is broken. A `'...::...'` string and
    a `-- :x::date` comment are not tokens of this kind at all."""
    toks = site.tokens
    return [b.line for a, b, c in zip(toks, toks[1:], toks[2:], strict=False)
            if a.token_type == TokenType.COLON and b.start == a.end + 1
            and b.text.startswith(sqlast.BIND_PREFIX)
            and c.token_type == TokenType.DCOLON and c.start == b.end + 1]


def site_findings(site: sqlast.Site, cfg: Config) -> tuple[list[str], set[str], set[str]]:
    a_violations: list[str] = []
    b_keys: set[str] = set()
    d_keys: set[str] = set()
    rel = site.relpath
    a_lines = set(rule_a(site))
    for ln in sorted(a_lines):
        a_violations.append(f"{rel}:{site.py_line(ln)}: {site.text.split(chr(10))[ln - 1].strip()}")
    for stmt in site.statements:
        for cast in stmt.find_all(exp.Cast):
            to = cast.args.get("to")
            spelled = FOOTGUN_TYPES.get(to.this) if isinstance(to, exp.DataType) else None
            if not (sqlast.is_bind(cast.this) and spelled):
                continue
            if sqlast.line_of(cast.this) in a_lines:
                continue                          # the `::` spelling: Rule A owns it
            if not site.waived(cfg.waiver, cast):
                b_keys.add(ruleb_key(rel, sqlast.bind_name(cast.this), spelled))
        for arith in stmt.find_all(exp.Add, exp.Sub):
            op = "+" if isinstance(arith, exp.Add) else "-"
            left, right = arith.this, arith.expression
            if isinstance(left, CLOCKS) and sqlast.is_bind(right):
                a_violations.append(
                    f"{rel}:{site.py_line(sqlast.line_of(arith))}: {_TEMPORAL} "
                    f"{arith.sql(dialect='postgres')}")
            if sqlast.is_bind(left) and isinstance(right, (exp.Interval, exp.MakeInterval)):
                if site.waived(cfg.d_waiver, left) or site.waived(cfg.d_waiver, arith):
                    continue
                kind = "make_interval" if isinstance(right, exp.MakeInterval) else "interval"
                d_keys.add(ruled_key(rel, sqlast.bind_name(left), op, kind))
    return a_violations, b_keys, d_keys


def scan_text(relpath: str, text: str, cfg: Config) -> tuple[list[str], set[str], set[str]]:
    """One python module's (rule_a_c_violations, rule_b_keys, rule_d_keys).

    A module that will not parse, or a SQL value in it that sqlglot cannot, EXITS 2."""
    tree = astread.parse(text, relpath, gate=cfg.gate)
    try:
        sites = sqlast.sites(relpath, text, tree)
    except sqlast.Unparsed as exc:
        astread.blind(relpath, exc, gate=cfg.gate)
    a_violations: list[str] = []
    b_keys: set[str] = set()
    d_keys: set[str] = set()
    for site in sites:
        a, b, d = site_findings(site, cfg)
        a_violations.extend(a)
        b_keys |= b
        d_keys |= d
    return a_violations, b_keys, d_keys


def scan_files(files: Sequence[pathlib.Path], cfg: Config) -> tuple[list[str], set[str], set[str]]:
    """`scan_text` over `files`, merged. AN UNREADABLE FILE EXITS 2, NOT 1 (`astread.read_text`):
    indexing from git can name a file the worktree lacks, and 1 is the FINDINGS code."""
    a_violations: list[str] = []
    b_keys: set[str] = set()
    d_keys: set[str] = set()
    for p in files:
        rel = cfg.rel(p)
        a, b, d = scan_text(rel, astread.read_text(p, gate=cfg.gate, origin=rel), cfg)
        a_violations.extend(a)
        b_keys |= b
        d_keys |= d
    return a_violations, b_keys, d_keys


def scan(cfg: Config) -> tuple[list[str], set[str], set[str]]:
    return scan_files(iter_files(cfg), cfg)


def problems(a_violations: list[str], b_keys: set[str], d_keys: set[str], baseline: set[str],
             d_baseline: set[str], cfg: Config) -> list[str]:
    """The failure lines for one scan against its two baselines; empty == pass."""
    out: list[str] = []
    for v in a_violations:
        if _TEMPORAL in v:
            out.append(
                f"[RULE C] temporal arithmetic on a bare bind (asyncpg sends it as unknown; "
                f"Postgres: 'operator is not unique: date + unknown' on the first live call -- "
                f"use CAST(:p AS integer) / CAST(:p AS interval)): {v}")
        else:
            out.append(
                f"[RULE A] bind-param adjacent to '::' cast (always broken — use "
                f"CAST(:p AS type)): {v}")

    for k in sorted(b_keys - baseline):
        relpath, snippet = k.split("|", 1)
        out.append(
            f"[RULE B] new CAST-on-footgun-type in {relpath}: {snippet}\n"
            f"          If a str can be bound here it will DataError on asyncpg at runtime.\n"
            f"          Fix: bind a typed object, or CAST(CAST(:x AS text) AS <type>).\n"
            f"          If genuinely safe: add '# {cfg.waiver}' (or '-- {cfg.waiver}')\n"
            f"          on the line, or run: {cfg.cmd} --write-baseline")

    for k in sorted(d_keys - d_baseline):
        relpath, snippet = k.split("|", 1)
        out.append(
            f"[RULE D] new bind-on-the-left interval arithmetic in {relpath}: {snippet}\n"
            f"          asyncpg sends an un-annotated bind as `unknown`. If NOTHING ELSE in this\n"
            f"          statement types that bind -- a bare comparison against a typed column, a\n"
            f"          BETWEEN operand, a generate_series argument, a COALESCE -- Postgres reads\n"
            f"          the arithmetic as interval-minus-interval and the comparison becomes\n"
            f"          `timestamptz > interval`: SQLSTATE 42883 on the FIRST LIVE CALL, invisible\n"
            f"          to every unit test. That is the 36-hour watchdog outage of 2026-09-17.\n"
            f"          Fix: CAST(:x AS timestamptz) / CAST(:x AS date) around the bind.\n"
            f"          If another context genuinely resolves it: add '-- {cfg.d_waiver}' on the\n"
            f"          line (NOT '-- {cfg.waiver}', which is Rule B's and no longer silences\n"
            f"          this rule), or run: {cfg.cmd} --write-baseline")
    return out


def check(cfg: Config) -> tuple[list[str], set[str], set[str]]:
    """Run the gate: (problems, rule_b_keys, rule_d_keys); empty problems == pass.

    IT TAKES NO PRE-COMPUTED SCAN: a `scanned=` parameter once let a caller pass an empty scan and
    get a clean verdict over zero files, skipping the coverage ratchet and the floor."""
    a, b, d = scan(cfg)
    rb, rd = cfg.ratchets()
    return problems(a, b, d, rb.load(), rd.load(), cfg), b, d


def _q(sql: str) -> str:
    """A one-line python module holding `sql` as a query -- what scan_text reads."""
    return "q(" + repr(sql) + ")"


def selftest(cfg: Config) -> list[str]:
    """Prove the rules can FIRE, in the job that runs the gate: every planted case, the shapes a
    regex got wrong in both directions, the blind path, and the coverage ratchet.

    RETURNS THE FAILURE STRINGS, NOT AN EXIT CODE: an `int` makes "nothing failed" and "returned
    the wrong type" the same falsy value."""
    nl = chr(10)
    w, dw = cfg.waiver, cfg.d_waiver
    out = "SELECT 1 FROM t WHERE created_at > :anchor - make_interval(secs => :within)"
    fixed = "SELECT 1 FROM t WHERE created_at > CAST(:anchor AS timestamptz) - make_interval(secs => 1)"
    # (python source, rule, expect a finding, why)
    cases: list[tuple[str, str, bool, str]] = [
        (_q(out), "d", True, "the 2026-09-17 outage shape must be seen"),
        (_q(out) + "  # " + w, "d", True,
         "Rule B's waiver must NOT silence Rule D — the fix for D carries B's waiver forever"),
        (_q(out) + "  # " + dw, "d", False, "Rule D's own waiver must work"),
        (_q(out + " -- " + dw), "d", False, "...in a SQL comment as well"),
        (_q(fixed) + "  # " + w, "d", False, "a wrapped bind leaves the population"),
        (_q("SELECT now() - :days * interval '1 day'"), "c", False,
         "the multiplier form resolves itself"),
        (_q("SELECT 1 FROM t WHERE ts < interval '1 day' + :until"), "d", False,
         "the MIRRORED form is a KNOWN MISS, not coverage (see the module docstring)"),
        (_q("SELECT 1 FROM t" + nl + "    -- never write :anchor - make_interval(secs => :x) here"),
         "d", False, "a SQL comment is a MENTION, not a use"),
        (_q("SELECT :x::date"), "a", True, "Rule A: a bind spelled `:x::type`"),
        (_q("SELECT col::date::text FROM t"), "a", False, "a column cast chain is not a bind"),
        (_q("SELECT now() - :age"), "c", True, "Rule C: the SQL clock minus a bare bind"),
        (_q("SELECT CAST(:ts AS timestamptz)"), "b", True, "Rule B: a bind cast to a footgun type"),
        (_q("SELECT CAST(:ts AS timestamptz)") + "  # " + w, "b", False, "Rule B's waiver"),
        (_q("SELECT CAST(CAST(:ts AS text) AS timestamptz)"), "b", False,
         "the double cast through text is the fix, not a finding"),
        # ── the shapes the line regex got WRONG, both directions ──────────────────────────
        (_q("WITH w AS (SELECT CAST(:since AS date) AS d) SELECT d FROM w"), "b", True,
         "a cast inside a CTE is a use"),
        (_q("SELECT 'a::b', ':x::date' AS s, 'CAST(:y AS date)' AS t"), "ab", False,
         "a string literal containing `::` or a CAST is data, not a cast"),
        (_q("SELECT 1 FROM t -- CAST(:y AS date), :z::date" + nl + "WHERE a = 1"), "ab", False,
         "a commented-out cast is not a cast"),
        (_q("SELECT 1" + nl + "  FROM t WHERE created_at > :anchor" + nl
            + "    - make_interval(secs => :within)"), "d", True,
         "a statement WRAPPED between bind and operator is still one statement"),
        ('"""A docstring: CAST(:ts AS timestamptz) and :x::date."""', "ab", False,
         "a docstring sentence is prose"),
    ]
    fails: list[str] = []
    for src, rules, expect, why in cases:
        a, b, d = scan_text("selftest.py", src, cfg)
        got = {"a": any("[temporal" not in v for v in a), "b": bool(b),
               "c": any("[temporal" in v for v in a), "d": bool(d)}
        if any(got[r] for r in rules) != expect:
            fails.append(f"  expected {'a finding' if expect else 'silence'}: {why}\n    {src}")
    checks = len(cases)

    # A SQL VALUE sqlglot CANNOT PARSE MUST EXIT 2 — never a clean pass, never exit 1.
    checks += 1
    try:
        scan_text("selftest.py", _q("".join(("SEL", "ECT FROM WHERE CAST(:x AS"))), cfg)
        fails.append("  an unparseable SQL value did NOT exit 2 — the gate would pass it unread")
    except SystemExit as exc:
        if exc.code != 2:
            fails.append(f"  an unparseable SQL value exited {exc.code}, not 2")

    # THE COVERAGE RATCHET, EXERCISED FOR REAL. The stderr line it prints is the mechanism WORKING.
    if cfg.required_dirs:
        checks += 1
        dropped = cfg.required_dirs[0]
        try:
            iter_files(dataclasses.replace(cfg, scan_dirs=tuple(x for x in cfg.scan_dirs if x != dropped)))
        except SystemExit as exc:
            if exc.code != 2:
                fails.append(f"  dropping {dropped}/ from SCAN_DIRS exited {exc.code}, not 2")
        else:
            fails.append(f"  dropping {dropped}/ from SCAN_DIRS did NOT exit 2 -- the gate would print OK "
                         f"with that directory unread")

    for f in fails:
        print(f, file=sys.stderr)
    print(f"[{cfg.gate}] selftest: {checks - len(fails)}/{checks} case(s) passed")
    return fails


def _parser(description: str | None, standalone: bool) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=description or __doc__)
    ap.add_argument("--write-baseline", action="store_true",
                    help="re-freeze the Rule-B and Rule-D baselines to the current tree")
    ap.add_argument("--rule", choices=("b", "d", "both"), default="both",
                    help="which ratchet --write-baseline may re-freeze (default: both)")
    ap.add_argument("--selftest", action="store_true",
                    help="prove the rules can fire (run in the same job as the gate)")
    if standalone:
        ap.add_argument("--root", type=pathlib.Path, default=pathlib.Path.cwd(),
                        help="checkout root (default: the current directory)")
        ap.add_argument("--scan-dir", action="append", default=[], metavar="DIR",
                        help="a directory to scan (repeatable; each must yield a tracked .py)")
        ap.add_argument("--baseline", type=pathlib.Path, default=Config.baseline,
                        help="Rule-B baseline, relative to --root")
        ap.add_argument("--d-baseline", type=pathlib.Path, default=Config.d_baseline,
                        help="Rule-D baseline, relative to --root")
    return ap


def main(argv: Sequence[str] | None = None, cfg: Config | None = None, *,
         description: str | None = None) -> int:
    """The CLI. A repo that wraps this passes its own `cfg` (and docstring); standalone, the
    scan dirs come from `--scan-dir` and every one of them is required."""
    args = _parser(description, cfg is None).parse_args(argv)
    if cfg is None and args.selftest:
        return 1 if selftest(Config(root=args.root.resolve())) else 0
    if cfg is None:
        if not args.scan_dir:
            print("ci-sql-casts: no --scan-dir given -- nothing to read (exit 2)", file=sys.stderr)
            return 2
        dirs = tuple(args.scan_dir)
        cfg = Config(root=args.root.resolve(), scan_dirs=dirs, required_dirs=dirs,
                     baseline=args.baseline, d_baseline=args.d_baseline)

    if args.selftest:
        return 1 if selftest(cfg) else 0

    if args.write_baseline:
        # RE-BLESSING NEEDS A STATED REASON (ci_kit.ratchet), so the per-rule diff is printed,
        # and `--rule` keeps reviewing one Rule-D site from blessing every pending B.
        _, b_keys, d_keys = scan(cfg)
        rb, rd = cfg.ratchets()
        for rule, keys, ratchet in (("b", b_keys, rb), ("d", d_keys, rd)):
            if args.rule not in (rule, "both"):
                continue
            was = ratchet.load()
            for k in sorted(keys - was):
                print(f"[{cfg.gate}]   + RULE {rule.upper()} {k}")
            for k in sorted(was - keys):
                print(f"[{cfg.gate}]   - RULE {rule.upper()} {k}")
            ratchet.write(keys)
            print(f"[{cfg.gate}] wrote {len(keys)} Rule-{rule.upper()} key(s) "
                  f"({len(keys - was)} added, {len(was - keys)} removed) -> {cfg.rel(ratchet.path)}")
        return 0

    found, b_now, d_now = check(cfg)
    if found:
        print(f"[{cfg.gate}] asyncpg CAST-on-str footgun check FAILED:\n", file=sys.stderr)
        for p in found:
            print(f"  {p}", file=sys.stderr)
        print(f"\n[{cfg.gate}] {len(found)} problem(s). See {cfg.doc} "
              f"for the fix + waiver docs.", file=sys.stderr)
        return 1
    # THE COUNTS COME FROM THE RUN, never from prose.
    print(f"[{cfg.gate}] OK — no Rule A/C violations; "
          f"{len(b_now)} Rule-B site(s) and {len(d_now)} Rule-D site(s), all baselined.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
