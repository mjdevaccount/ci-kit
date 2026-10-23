"""Ratchet: uncast NUMERIC columns in SQL SELECT projections -- the Decimal law (`ci-decimal-reads`).

asyncpg decodes Postgres NUMERIC as `decimal.Decimal`. A Decimal that leaves a repository poisons
pure float math (TypeError mid-calculation) or serializes as a JSON *string* (".toFixed is not a
function" in a browser). The law: every numeric column leaving a repository gets `::float8`. This
gate makes the law mechanical.

THE GROUND TRUTH is a text file naming every NUMERIC column, one `schema.table.column` per line
(`#` comments allowed), generated from the live information_schema:

    SELECT table_schema||'.'||table_name||'.'||column_name FROM information_schema.columns
     WHERE data_type = 'numeric' AND table_schema NOT IN ('pg_catalog','information_schema')
     ORDER BY 1;

The gate matches by BARE column name. A cols file that is missing or names no column exits 2:
an empty ground truth lets every projection through and would report OK with a rule that cannot
fire. Keeping the file fresh is the caller's job; a caller with numbered migrations should check
its own high-water mark (`before_scan` hook) -- a stale allow-list does not fail loudly, it
quietly stops covering things.

The scanner PARSES every SQL value (`ci_kit.sql.sqlast`) and flags any SELECT or RETURNING item --
subqueries and CTEs included -- that references a known numeric column with no cast
(`::float8` / `CAST(.. AS int)` / ...) anywhere in the item. `count(...)` items pass (bigint), as
do EXTRACT items. WHERE / INSERT / UPDATE references are not reads and are ignored by
construction. A SQL value that will not parse EXITS 2.

Heuristic, so a RATCHET: known-safe items are grandfathered in the baseline (`--write-baseline`);
the waiver token anywhere in the SQL value, or on the line above it, waives the value (use it when
the consumer genuinely wants Decimal, or for a same-named non-numeric column). A NEW
un-baselined item fails CI.
"""

from __future__ import annotations

import argparse
import dataclasses
import pathlib
import re
import sys
from collections.abc import Callable, Sequence

from sqlglot import exp

from ci_kit import astread, discover, site_lint
from ci_kit.ratchet import RatchetSet
from ci_kit.sql import _cli, sqlast

#: a cast to any of these anywhere in the item passes it. `numeric` is kept on purpose
#: (`round(x::numeric, 2)` still decodes as Decimal, but that is an explicit choice at the site).
CAST_OK = frozenset({exp.DataType.Type.FLOAT, exp.DataType.Type.DOUBLE, exp.DataType.Type.INT,
                     exp.DataType.Type.BIGINT, exp.DataType.Type.SMALLINT, exp.DataType.Type.TEXT,
                     exp.DataType.Type.DECIMAL})

HEADER = (
    "# Uncast-NUMERIC-read baseline — see {doc}.\n"
    "# Grandfathered projection items referencing a NUMERIC column without ::float8.\n"
    "# Each is either consumer-verified Decimal-safe or a false positive. A NEW key\n"
    "# fails CI. Regenerate: {cmd} --write-baseline\n"
)


@dataclasses.dataclass(frozen=True)
class Config:
    root: pathlib.Path = dataclasses.field(default_factory=pathlib.Path.cwd)
    scan_dirs: tuple[str, ...] = ()
    exclude: tuple[pathlib.Path, ...] = ()
    cols: pathlib.Path = pathlib.Path("numeric_columns.txt")
    baseline: pathlib.Path = pathlib.Path("decimal_reads_baseline.txt")
    waiver: str = "decimal-ok"
    gate: str = "ci-decimal-reads"
    cmd: str = "ci-decimal-reads"
    doc: str = "ci_kit/sql/decimal_reads.py"
    header: str = HEADER

    def _abs(self, p: pathlib.Path) -> pathlib.Path:
        return p if p.is_absolute() else self.root / p

    def ratchet(self) -> RatchetSet:
        return RatchetSet(self._abs(self.baseline), header=self.header.format(doc=self.doc, cmd=self.cmd))

    def rel(self, p: pathlib.Path) -> str:
        return p.relative_to(self.root).as_posix()


def load_cols(cfg: Config) -> set[str]:
    """The ground truth, as bare column names. Missing or EMPTY exits 2: missing would otherwise
    be a traceback (exit 1, the findings code) and empty lets every projection through."""
    path = cfg._abs(cfg.cols)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[{cfg.gate}] cannot read {path.name}: {exc} -- could not look", file=sys.stderr)
        raise SystemExit(2) from exc
    cols: set[str] = set()
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            cols.add(ln.rsplit(".", 1)[-1])
    if not cols:
        print(f"[{cfg.gate}] {path.name} names no NUMERIC column -- the rule cannot fire, so "
              "this is exit 2, not clean", file=sys.stderr)
        raise SystemExit(2)
    return cols


def projections(stmt: exp.Expression) -> list[exp.Expression]:
    """Every SELECT list item (subqueries and CTEs included) and every RETURNING item."""
    items: list[exp.Expression] = []
    for sel in stmt.find_all(exp.Select):
        items += sel.expressions
    for ret in stmt.find_all(exp.Returning):
        items += ret.expressions
    return items


def _own(item: exp.Expression) -> list[exp.Expression]:
    """The item's nodes, minus any nested SELECT: a subquery's items are projections of their
    own and are judged there, once."""
    return list(item.walk(prune=lambda n: n is not item and isinstance(n, (exp.Select, exp.Subquery))))


def uncast_numeric(item: exp.Expression, cols: set[str]) -> bool:
    bare = item.unalias()
    if isinstance(bare, exp.Filter):          # COUNT(*) FILTER (WHERE ...) is still a count
        bare = bare.this
    if isinstance(item, exp.Star) or isinstance(bare, exp.Count):
        return False
    nodes = _own(item)
    if not any(isinstance(n, exp.Column) and n.name.lower() in cols for n in nodes):
        return False
    if any(isinstance(n, exp.Extract) for n in nodes):
        return False
    return not any(isinstance(n, exp.Cast) and n.to.this in CAST_OK for n in nodes)


def item_key(relpath: str, item: exp.Expression) -> str:
    text = item.sql(dialect="postgres", comments=False)
    text = re.sub(r"%\(" + sqlast.BIND_PREFIX + r"(\w+)\)s", r":\1", text)
    return f"{relpath}|{' '.join(text.split())[:90].rstrip()}"


def scan_text(relpath: str, text: str, cols: set[str], cfg: Config) -> list[tuple[str, str]]:
    """[(key, item)] of projection items referencing a numeric col without a cast.

    A waiver anywhere in the SQL value, or on the line above it, waives the value. A module or
    SQL value that will not parse EXITS 2."""
    out: dict[str, str] = {}
    for site in sqlast.module_sites(relpath, text, gate=cfg.gate):
        if site.waived(cfg.waiver) or cfg.waiver in site.comments.get(site.line - 1, ""):
            continue
        for stmt in site.statements:
            for item in projections(stmt):
                if uncast_numeric(item, cols):
                    out.setdefault(item_key(relpath, item), item.sql(dialect="postgres"))
    return list(out.items())


def iter_files(cfg: Config) -> list[pathlib.Path]:
    """Tracked .py under `cfg.scan_dirs` (`git ls-files`, never a walk); a dir yielding none exits 2."""
    skip = {p.resolve() for p in cfg.exclude}
    out: list[pathlib.Path] = []
    for d in cfg.scan_dirs:
        rels = discover.tracked((d,), root=cfg.root)
        discover.require_scanned(len(rels), gate=f"{cfg.gate}[{d}]", dirs=(d,), floor=1)
        out.extend(p for rel in rels if (p := cfg.root / rel).resolve() not in skip)
    return out


def scan(cols: set[str], cfg: Config) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for p in iter_files(cfg):
        rel = cfg.rel(p)
        out.extend(scan_text(rel, astread.read_text(p, gate=cfg.gate, origin=rel), cols, cfg))
    return out


def selftest(cfg: Config) -> list[str]:
    """Both directions, on sources built here, against a two-column ground truth."""
    cols = {"pnl_usd", "hold_hrs"}
    nl = chr(10)
    w = cfg.waiver

    def fired(src: str) -> bool:
        return bool(scan_text("f.py", src, cols, cfg))

    fails: list[str] = []
    cases = [
        (True, "a bare numeric column", 'q("SELECT t.hold_hrs FROM t")'),
        (False, "a ::float8 cast", 'q("SELECT t.hold_hrs::float8 AS h FROM t")'),
        (False, "a CAST(.. AS float8) — the regex saw only `::`", 'q("SELECT CAST(hold_hrs AS float8) FROM t")'),
        (False, "count(...) is bigint", 'q("SELECT count(hold_hrs) AS n FROM t")'),
        (False, "a numeric column in WHERE is not a read", 'q("SELECT id FROM t WHERE pnl_usd > 0")'),
        (True, "a RETURNING item", 'q("UPDATE t SET a = 1 RETURNING pnl_usd")'),
        (True, "an uncast read inside a CTE", 'q("WITH w AS (SELECT pnl_usd FROM t) SELECT 1 FROM w")'),
        (False, "a numeric name in a string literal is data", 'q("SELECT ' + "'pnl_usd'" + ' AS s FROM t")'),
        (False, "a commented-out projection is not one", "q('''SELECT id -- , pnl_usd" + nl + " FROM t''')"),
        (False, "a docstring is not SQL", '"""SELECT pnl_usd FROM t is what we used to run."""'),
        (False, "a waiver on the line above", "# " + w + nl + 'q("SELECT pnl_usd FROM t")'),
        (False, "a waiver in the SQL", 'q("SELECT pnl_usd FROM t -- ' + w + '")'),
    ]
    for should_fire, name, src in cases:
        if fired(src) != should_fire:
            fails.append(f"{name}: expected {'a finding' if should_fire else 'silence'}")
    blind = _cli.blind_probe(lambda: fired("q(" + repr("".join(("SEL", "ECT FROM WHERE"))) + ")"),
                             missed="an unparseable SQL value did not exit 2",
                             wrong="an unparseable SQL value exited {code}, not 2")
    return fails + ([blind] if blind else [])


def main(argv: Sequence[str] | None = None, cfg: Config | None = None, *,
         description: str | None = None,
         extra_args: Callable[[argparse.ArgumentParser], None] | None = None,
         before_scan: Callable[[argparse.Namespace], int | None] | None = None) -> int:
    """The CLI. A wrapping repo passes its `cfg`, and may add flags (`extra_args`) and a check that
    runs before the scan and may end the run with its own exit code (`before_scan`) -- the place
    for a ground-truth freshness check that the kit cannot know how to ask. The run itself is
    `site_lint.check`'s; `before_scan` is the caller's and runs ahead of it (never on --selftest)."""
    ap = argparse.ArgumentParser(description=description or __doc__)
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--list", action="store_true", help="dump raw findings, ignore baseline")
    ap.add_argument("--selftest", action="store_true", help="prove the rule can fire")
    if cfg is None:
        ap.add_argument("--root", type=pathlib.Path, default=pathlib.Path.cwd())
        ap.add_argument("--scan-dir", action="append", default=[], metavar="DIR")
        ap.add_argument("--cols", type=pathlib.Path, default=Config.cols,
                        help="the NUMERIC ground truth, one schema.table.column per line")
        ap.add_argument("--baseline", type=pathlib.Path, default=Config.baseline)
    if extra_args is not None:
        extra_args(ap)
    args = ap.parse_args(argv)
    if cfg is None:
        cfg = Config(root=args.root.resolve(), scan_dirs=tuple(args.scan_dir), cols=args.cols,
                     baseline=args.baseline)
        if not cfg.scan_dirs and not args.selftest:
            print(f"{cfg.gate}: no --scan-dir given -- nothing to read (exit 2)", file=sys.stderr)
            return 2
    if before_scan is not None and not args.selftest:
        rc = before_scan(args)
        if rc is not None:
            return rc
    c = cfg
    seen: dict[str, int] = {}

    def keys() -> set[str]:
        return {k for k, _ in scan(load_cols(c), c)}

    def list_raw() -> int:
        findings = scan(load_cols(c), c)
        for k, _ in findings:
            print(k)
        print(f"[{c.gate}] {len(findings)} raw finding(s) across "
              f"{len({k.split('|')[0] for k, _ in findings})} file(s)")
        return 0

    def write() -> int:
        now = keys()
        c.ratchet().write(now)
        print(f"[{c.gate}] wrote {len(now)} baseline keys")
        return 0

    def new_keys() -> list[str]:
        now = keys()
        seen["keys"] = len(now)
        return sorted(now - c.ratchet().load())

    def report(new: Sequence[str]) -> None:
        if new:
            print(f"[{c.gate}] NEW uncast NUMERIC reads (THE DECIMAL LAW):\n", file=sys.stderr)
            for k in new:
                print(f"  {k}", file=sys.stderr)
            print(f"\n[{c.gate}] {len(new)} new. Cast with ::float8, waive with "
                  f"-- {c.waiver}, or --write-baseline after review.", file=sys.stderr)
            return
        print(f"[{c.gate}] OK — no un-baselined uncast NUMERIC reads "
              f"({seen['keys']} grandfathered).")

    # --list is read before --write-baseline, as it always was.
    return site_lint.check(
        _cli.flags(args, "selftest", "list", "write_baseline"), gate=c.gate, scan=new_keys,
        selftest=lambda: selftest(c), selftest_report=_cli.selftest_report(f"[{c.gate}] selftest"),
        modes={"--list": list_raw, "--write-baseline": write}, report=report)


if __name__ == "__main__":
    raise SystemExit(main())
