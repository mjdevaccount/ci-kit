"""ci-sql-placement: SQL written OUTSIDE the repository layer may not GROW. Shrink-only per file.

MOVED FROM trade-data (v1.50.0, `scripts/lint_sql_placement.py`). "Repositories own ALL SQL" was a
layering rule nothing checked: import-linter sees imports, not statements, and the inventory that
found the second writers used a same-line `verb schema.table` scan that missed multi-line SQL and
f-string table names. This is that scan done with a parser; its baseline is the scoreboard.

WHAT COUNTS, per file (a write wrapped in `text(` is two sites):
  * a WRITE STATEMENT -- an Insert / Update / Delete / TruncateTable node in a SQL value PARSED by
    sqlglot (postgres) through `sqlast.module_sites`, the one SQL reader every kit gate uses
    (f-strings, `+` chains and named constants folded; comments and docstrings never read).
    `TRUNCATE a; DELETE FROM b` is two, as is `WITH d AS (DELETE ..) INSERT ..`.
  * a `text(` CALL -- sqlalchemy's, bare (`from sqlalchemy[.sql[.expression]] import text [as t]`)
    or as a module attribute (`sa.text(`, `sqlalchemy.sql.text(`, `sql.text(`). Reads count here:
    a SELECT outside a repository is still SQL outside a repository.
A module spelling none of the write verbs and not `sqlalchemy` is not parsed (it cannot hold a
countable site), so a python syntax error there is the linter's to report, not this gate's.

SCOPE: every `--dir DIR[:FLOOR]`, minus paths under an `--owner` (where SQL belongs) or an
`--exempt` (e.g. the gates' own selftest shapes). Both are PATH PREFIXES; `*` matches one path
segment (`app/content/*/repositories/`), and a prefix without a trailing `/` matches a file-name
prefix too (`scripts/lint_`). At least one `--owner` is required. NOT SEEN: `.sql` files, SQL from
a value another module passes in, `MERGE` / `COPY`.

  ci-sql-placement --dir DIR[:FLOOR] [--dir ...] --owner PREFIX [--owner ...] [--exempt PREFIX ...]
                   [--baseline ci/sql_placement_baseline.txt] [--write-baseline] [--root DIR] [--selftest]

Exit 0 clean, 1 a new or grown file, 2 could not look (no --owner, a dir under its floor, a SQL
value or a module that will not parse).
"""
from __future__ import annotations

import ast
import re
import sys
from collections.abc import Sequence

from sqlglot import exp

from ci_kit import astread, discover, site_lint
from ci_kit.ratchet import RatchetCount, kept_header
from ci_kit.sql import sqlast

GATE = "sql-placement"
HEADER = (
    "# sql-placement baseline: per-file count of SQL sites OUTSIDE the repository layer: parsed\n"
    "# INSERT/UPDATE/DELETE/TRUNCATE statements + sqlalchemy text( calls (`ci-sql-placement`).\n"
    "# SHRINK-ONLY: a grown count or an unlisted file fails CI. Re-freeze: --write-baseline.\n"
)
_WRITES = (exp.Insert, exp.Update, exp.Delete, exp.TruncateTable)
_TEXT_MODULES = ("sqlalchemy", "sqlalchemy.sql", "sqlalchemy.sql.expression")
#: the cut before the parse: INSERT / DELETE / TRUNCATE anywhere, UPDATE not as `.update(` / `x_update`
_WRITE_WORD = re.compile(r"(?i)\b(insert|delete|truncate)\b|(?<![.\w])update\b(?!\s*\()")


def prefix_matcher(prefixes: Sequence[str]) -> re.Pattern[str] | None:
    """One regex for path prefixes where `*` is one segment; None when there are none."""
    alts = ["".join("[^/]*" if c == "*" else re.escape(c) for c in site_lint.norm_dir(p) + ("/" if p.endswith("/") else ""))
            for p in prefixes]
    return re.compile("^(?:" + "|".join(alts) + ")") if alts else None


def _text_paths(bound: str, module: str) -> set[str]:
    return {bound + m[len(module):] for m in _TEXT_MODULES if m == module or m.startswith(module + ".")}


def text_calls(tree: ast.Module) -> list[int]:
    """Line numbers of every sqlalchemy `text(` call, aliases and submodule paths followed."""
    mods: set[str] = set()
    bare: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".", 1)[0] == "sqlalchemy":   # `import a.b` binds `a`
                    mods |= _text_paths(a.asname, a.name) if a.asname else _text_paths("sqlalchemy", "sqlalchemy")
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for a in node.names:
                if a.name == "text" and node.module in _TEXT_MODULES:
                    bare.add(a.asname or a.name)
                elif f"{node.module}.{a.name}" in _TEXT_MODULES:
                    mods |= _text_paths(a.asname or a.name, f"{node.module}.{a.name}")
    return sorted(node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call) and (
        (isinstance(node.func, ast.Name) and node.func.id in bare)
        or (isinstance(node.func, ast.Attribute) and node.func.attr == "text" and ast.unparse(node.func.value) in mods)))


def sql_sites(source: str, rel: str) -> list[int]:
    """Every counted site in one module: parsed write statements + `text(` calls."""
    want_writes, want_text = bool(_WRITE_WORD.search(source)), "sqlalchemy" in source
    lines = [site.line for site in sqlast.module_sites(rel, source, gate=GATE)
             for stmt in site.statements for _ in stmt.find_all(*_WRITES)] if want_writes else []
    if want_text:
        lines += text_calls(astread.parse(source, rel, gate=GATE))
    return sorted(lines)


_SHAPES = (
    "from sqlalchemy import text as T\nimport sqlalchemy as sa\n"
    "A = 'INSERT INTO s.x (a) VALUES (:a) ON CONFLICT (a) DO UPDATE SET a = :a'\n"
    "B = f'''\n    UPDATE {SCHEMA}.{tbl}\n       SET a = 1\n'''\n"
    "C = 'WITH d AS (DELETE FROM s.x RETURNING a) INSERT INTO s.y SELECT a FROM d'\n"
    "D = 'TRUNCATE ' + ', '.join(TABLES) + ' RESTART IDENTITY CASCADE'\n"
    "E = T('SELECT 1')\nF = sa.text('SELECT 2')\n"
    "from sqlalchemy import sql\nG = sql.text('SELECT 3')\nH = sa.sql.text('SELECT 4')\n"
    "J = 'TRUNCATE s.a; DELETE FROM s.b WHERE x = 1'\n"
)  # insert, multi-line f-string update, CTE delete+insert (2), truncate chain, four text(, TRUNCATE;DELETE (2) = 11
_NOT_SQL = (
    '"""INSERT INTO prose: a docstring is never executed. DELETE FROM x."""\n'
    "# DELETE FROM s.x -- a comment\nS = 'SELECT a FROM s.x -- delete from nothing'\n"
    "r = 'truncate disabled'\nmsg = 'Truncate to the cap instead'\n"
    "def text(s): return s\nq = text('x')\n"
)


def selftest() -> list[str]:
    import contextlib
    import io
    fails: list[str] = []
    if (n := len(sql_sites(_SHAPES, "<shapes>"))) != 11:
        fails.append(f"the SQL shapes counted {n}, not 11")
    if got := sql_sites(_NOT_SQL, "<notsql>"):
        fails.append(f"prose, a comment, a SELECT, 'truncate disabled' or a local text() counted: {got}")
    m = prefix_matcher(["app/repositories/", "app/content/*/repositories/", "scripts/lint_"])
    hits = [p for p in ("app/repositories/r.py", "app/content/mail/repositories/r.py", "scripts/lint_x.py",
                        "app/repositories_old/r.py", "app/content/repositories/r.py", "scripts/x_lint_.py")
            if m and m.match(p)]
    if hits != ["app/repositories/r.py", "app/content/mail/repositories/r.py", "scripts/lint_x.py"]:
        fails.append(f"prefix matching: {hits}")
    try:
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            sql_sites("Q = 'INSERT INTO s.x (a VALUES'\n", "<broken>")
        fails.append("an unparseable SQL value did not exit")
    except SystemExit as e:
        if e.code != 2:
            fails.append(f"an unparseable SQL value exited {e.code}, not 2")
    return fails


def main(argv: Sequence[str] | None = None) -> int:
    ap = site_lint.gate_parser("ci-sql-placement", __doc__, baseline="ci/sql_placement_baseline.txt")
    ap.add_argument("--owner", action="append", default=[], metavar="PREFIX")
    ap.add_argument("--exempt", action="append", default=[], metavar="PREFIX")
    a, root, floors = site_lint.parse_gate(ap, argv, gate=GATE, selftest=selftest)
    if not a.owner:
        print(f"{GATE}: no --owner -- 'outside the repositories' means nothing without them", file=sys.stderr)
        return 2
    baseline = site_lint.under(root, a.baseline)
    out_of_scope = prefix_matcher([*a.owner, *a.exempt])
    scanned = [0]

    def scan() -> dict[str, int]:
        files = discover.tracked_floored(floors, gate=GATE, root=root,
                                         keep=lambda rel: not (out_of_scope and out_of_scope.match(rel)))
        scanned[0] = len(files)
        counts = {rel: len(sql_sites(astread.read_text(root / rel, gate=GATE, origin=rel), rel)) for rel in files}
        return {rel: n for rel, n in counts.items() if n}

    def describe(rel: str, now: int, base: int) -> str:
        if base == 0:
            return f"NEW: {rel} holds {now} SQL site(s) outside the repositories and is not in the baseline."
        return f"GREW: {rel} now {now} SQL site(s) outside the repositories, baseline {base}."

    def ok(counts: dict[str, int], slack: list[tuple[str, int, int]]) -> str:
        note = f"; {len(slack)} file(s) shrank -- tighten with --write-baseline" if slack else ""
        return (f"{GATE}: OK ({sum(counts.values())} SQL site(s) outside the repositories in {len(counts)} "
                f"file(s), none grown; {scanned[0]} file(s) read{note})")

    return site_lint.run(
        ["--write"] if a.write else [], gate=GATE, scan=scan, scanned=lambda: scanned[0],
        scan_dirs=list(floors), ratchet=RatchetCount(baseline, kept_header(baseline, HEADER)),
        selftest=selftest, floor=1, gained="SQL site(s) outside the repositories", describe=describe, ok=ok,
        advice=(f"SQL lives under {', '.join(a.owner)}: put the statement behind a repository method.",))


if __name__ == "__main__":
    sys.exit(main())
