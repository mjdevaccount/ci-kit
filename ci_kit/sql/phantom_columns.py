"""Phantom columns -- SQL that names a column its table does not have (`ci-phantom-columns`).

A select of a column that does not exist is syntactically fine, passes every linter, and passes
every unit test that fakes the repository method -- then raises `UndefinedColumnError` on every
real call. A phantom column is invisible to every check that does not hold the real schema, so
this gate takes the schema as INPUT: `--schema schema.json`, `{"schema.table": ["col", ...]}`,
built from a migrated database (e.g. an integration test querying information_schema.columns).

PRECISION OVER RECALL, deliberately. Only statements that reference EXACTLY ONE
schema-qualified table are analysed (no JOIN / CTE / UNION / subquery / comma-join), so every
column name is unambiguously that table's. Within those, only positions that must be a BASE
COLUMN are considered, each a node of sqlglot's parse tree (`ci_kit.sql.sqlast`):

    CAST(col AS type) / col::type        a cast
    any column in a SELECT / RETURNING   a select list cannot reference its own output aliases
    any column under WHERE               minus output aliases

ORDER BY / GROUP BY are not checked: SQL may order on an output alias, including an implicit
one. This gate is allowed to miss things; it is NOT allowed to cry wolf, because a noisy gate gets
waived and then it is a guard that cannot fire. A table absent from the schema (a view, a temp
table, another database) is not this gate's call and is skipped.

A SQL value that will not parse EXITS 2. So do a missing, malformed or EMPTY schema -- an empty
one makes every table "not our call", which would scan the whole tree and report 0 phantoms.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import sys
from collections.abc import Sequence

from sqlglot import exp

from ci_kit import astread, discover
from ci_kit.sql import sqlast

#: Postgres system columns -- real, queryable, and absent from information_schema.columns.
NON_COLUMNS = frozenset({"ctid", "xmin", "xmax", "cmin", "cmax", "tableoid", "oid"})

#: more than one relation in scope means a bare column is no longer unambiguously OUR table's
_AMBIGUOUS = (exp.Join, exp.With, exp.Union, exp.Subquery, exp.Unnest, exp.Lateral)


@dataclasses.dataclass(frozen=True)
class Config:
    root: pathlib.Path = dataclasses.field(default_factory=pathlib.Path.cwd)
    gate: str = "ci-phantom-columns"
    #: the name the selftest line speaks as: "<label>: selftest OK"
    label: str = "ci-phantom-columns"
    #: a tracked path containing any of these segments is not scanned (test fixtures fake SQL)
    skip_segments: tuple[str, ...] = ("tests",)


def single_table(stmt: exp.Expression) -> exp.Table | None:
    """The statement's one schema-qualified table, or None when it has any other shape."""
    if stmt.find(*_AMBIGUOUS) or len(list(stmt.find_all(exp.Select))) > 1:
        return None
    tables = list(stmt.find_all(exp.Table))
    if len(tables) != 1 or not tables[0].db:
        return None
    return tables[0]


def columns_used(stmt: exp.Expression, table: exp.Table) -> set[str]:
    """Names in a position that must be a base column of `table`."""
    own = {"", table.name, table.alias_or_name}
    aliases = {a.alias for a in stmt.find_all(exp.Alias)}
    base: set[str] = set()
    derived: set[str] = set()
    for col in stmt.find_all(exp.Column):
        if col.table not in own or col.name in ("", sqlast.INTERP):
            continue
        parent = col.parent
        if isinstance(parent, exp.Cast) and parent.this is col:
            base.add(col.name.lower())
        elif col.find_ancestor(exp.Order, exp.Group, exp.Having) is not None:
            continue
        elif col.find_ancestor(exp.Where) is not None:
            derived.add(col.name.lower())
        elif isinstance(col.find_ancestor(exp.Select, exp.Returning, exp.Update), (
                exp.Select, exp.Returning)):
            base.add(col.name.lower())    # a select list cannot reference its own output aliases
    return base | (derived - {a.lower() for a in aliases})


def phantoms(src: str, relpath: str, schema: dict[str, list[str]],
             cfg: Config) -> list[tuple[int, str, list[str]]]:
    """(python line, table, missing columns) per statement naming a column its table lacks."""
    out = []
    for site in sqlast.module_sites(relpath, src, gate=cfg.gate):
        for stmt in site.statements:
            table = single_table(stmt)
            if table is None:
                continue
            key = f"{table.db}.{table.name}".lower()
            cols = schema.get(key)
            if cols is None:            # view, temp table, or another database -- not our call
                continue
            bad = sorted(c for c in columns_used(stmt, table)
                         if c not in set(cols) and c not in NON_COLUMNS)
            if bad:
                out.append((site.line, key, bad))
    return out


def scan(scan_dir: pathlib.Path, schema: dict[str, list[str]],
         cfg: Config) -> list[tuple[str, int, str, list[str]]]:
    """Tracked .py under `scan_dir` (`git ls-files`, never a walk). Nothing scanned EXITS 2."""
    rel_root = scan_dir.resolve().relative_to(cfg.root.resolve()).as_posix()
    files = [r for r in discover.tracked((rel_root,), root=cfg.root)
             if not any(f"/{s}/" in f"/{r}" for s in cfg.skip_segments)]
    discover.require_scanned(len(files), gate=cfg.gate, dirs=(rel_root,), floor=1)
    out = []
    for rel in files:
        path = cfg.root / rel
        for lineno, table, bad in phantoms(astread.read_text(path, gate=cfg.gate), rel, schema, cfg):
            out.append((str(path), lineno, table, bad))
    return out


def load_schema(path: str, cfg: Config) -> dict[str, list[str]] | int:
    """The schema dict, or 2 (printed why) when it cannot be read or names no table."""
    try:
        schema = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"{cfg.gate}: could not read the schema {path}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(schema, dict) or not schema:
        print(f"{cfg.gate}: the schema {path} names no tables -- every statement would be "
              "skipped as 'not our call', so this is exit 2, not clean", file=sys.stderr)
        return 2
    return schema


def run(schema_path: str, scan_dir: pathlib.Path, cfg: Config) -> int:
    schema = load_schema(schema_path, cfg)
    if isinstance(schema, int):
        return schema
    hits = scan(scan_dir, schema, cfg)
    for path, lineno, table, bad in hits:
        print(f"{path}:{lineno}  {table}  no such column: {', '.join(bad)}")
    print(f"\n{len(hits)} phantom-column statement(s)")
    return 1 if hits else 0


def selftest(cfg: Config) -> list[str]:
    """Both directions. A gate that only proves the green case is the bug it exists to catch."""
    f: list[str] = []
    schema = {"quant.operator_trades": ["trade_id", "entry_px", "exit_px", "setup", "is_paper"]}

    def found(sql: str) -> set[str]:
        return {c for _ln, _t, bad in phantoms("q = " + repr(sql), "<selftest>", schema, cfg)
                for c in bad}

    # RED: a cast to a column that does not exist, aliased to its own name (the case a naive
    # alias filter silently misses).
    bad = ("SELECT trade_id, entry_px::float8 AS entry_px,"
           " risk_usd::float8 AS risk_usd FROM quant.operator_trades WHERE setup = :s")
    if found(bad) != {"risk_usd"}:
        f.append(f"the #1615 cast was not the one finding: {found(bad)}")
    if found("SELECT CAST(risk_usd AS float8) FROM quant.operator_trades") != {"risk_usd"}:
        f.append("the CAST() spelling of the same bug was missed")
    if found("SELECT t.trade_id FROM quant.operator_trades t WHERE t.risk_usd > 0") != {"risk_usd"}:
        f.append("an alias-qualified predicate column was missed")

    # GREEN: binds, computed output aliases ordered on, and real columns must all stay quiet.
    good = ("SELECT trade_id, exit_px::float8 AS exit_px, count(*)::int AS runs"
            " FROM quant.operator_trades WHERE setup = :setup AND is_paper"
            " ORDER BY runs DESC")
    if found(good):
        f.append(f"false positives on a clean statement: {found(good)}")
    # ...and the shapes a regex read as columns: a comment, a string literal, a JOIN.
    quiet = ("SELECT trade_id, 'risk_usd::float8' AS s FROM quant.operator_trades"
             " -- risk_usd::float8 AS risk_usd")
    if found(quiet):
        f.append(f"a comment or string literal was read as a column: {found(quiet)}")
    if found("SELECT o.risk_usd::float8 FROM quant.operator_trades t JOIN quant.x o ON o.id = t.trade_id"):
        f.append("a two-table statement was analysed; its columns are ambiguous")
    # A cast inside a CTE: the CTE's output columns are not the table's, so the statement is
    # skipped as ambiguous.
    if found("WITH w AS (SELECT risk_usd::float8 AS r FROM quant.operator_trades) SELECT r FROM w"):
        f.append("a CTE statement was analysed; its columns are ambiguous")
    try:
        phantoms("q = " + repr("".join(("SEL", "ECT FROM WHERE"))), "<selftest>", schema, cfg)
        f.append("an unparseable SQL value did not exit 2")
    except SystemExit as exc:
        if exc.code != 2:
            f.append(f"an unparseable SQL value exited {exc.code}, not 2")
    return f


def run_selftest(cfg: Config) -> int:
    fails = selftest(cfg)
    for x in fails:
        print(f"  SELFTEST FAIL: {x}")
    print(f"{cfg.label}: selftest {'FAILED' if fails else 'OK'}")
    return 1 if fails else 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--schema", help='{"schema.table": ["col", ...]} from a migrated database')
    ap.add_argument("--root", type=pathlib.Path, default=pathlib.Path.cwd(),
                    help="the repository root (default: cwd)")
    ap.add_argument("--scan-dir", default=None, metavar="DIR",
                    help="the directory to read, relative to --root (required unless --selftest)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    cfg = Config(root=args.root.resolve())
    if args.selftest:
        return run_selftest(cfg)
    if not args.schema or not args.scan_dir:
        ap.error("--schema and --scan-dir are required (or use --selftest)")
    return run(args.schema, cfg.root / args.scan_dir, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
