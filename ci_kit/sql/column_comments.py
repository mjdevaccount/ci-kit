"""Ratchet: a migration column comment that STATES a domain must be backed by a CHECK (`ci-column-comments`).

A column comment such as `-- max adverse excursion` declares a domain (a magnitude, so >= 0) in
English. Nothing makes it true: a later reader treats the column as signed and every number built
on it is wrong while the comment is right the whole time. This gate reads the migrations directory
and fails when a NEW migration declares a domain in a column comment that no CHECK in the same file
enforces.

A RATCHET, not an audit: files numbered at or below `legacy_max` are grandfathered, and a key
`<file>:<column>` in the json baseline waives one column, so the gate constrains what gets ADDED
rather than demanding a rewrite of history. Deliberately conservative -- it fires only on comments
that state a domain unambiguously (a magnitude word, a 0..1 range, an explicit enum, a comparison
operator), because a linter that cries wolf on prose gets suppressed and then protects nothing.

LINE-BASED, NOT PARSED: a column definition with a trailing `-- comment` inside a CREATE TABLE
body, read without a SQL parser (comments are not in sqlglot's tree). Migrations are found with
`git ls-files` (`<migrations>/*.sql`, one level); none at all exits 2 -- nothing was read.
"""

from __future__ import annotations

import argparse
import dataclasses
import pathlib
import re
import sys
from collections.abc import Sequence

from ci_kit import discover, site_lint
from ci_kit.ratchet import RatchetSet
from ci_kit.sql import _cli

# A column definition line carrying a trailing `-- comment`. Anchored on the comma or line end so a
# multi-line CREATE TABLE body parses without a SQL parser.
_COL_LINE = re.compile(
    r"^\s*(?P<col>[a-z_][a-z0-9_]*)\s+(?P<type>[a-z0-9_ ()\[\]]+?)\s*,?\s*--\s*(?P<comment>.+?)\s*$",
    re.I,
)

# Phrases that assert a domain. Each maps to the CHECK shape that would satisfy it.
_MAGNITUDE = re.compile(r"\b(magnitude|max adverse|max favorable|maximum adverse|maximum favorable"
                        r"|drawdown|giveback|excursion)\b", re.I)
_UNIT_RANGE = re.compile(r"\b0\s*\.\.\s*1\b|\b0-1\b|\bfraction\b")
_ENUM = re.compile(r"'[^']+'\s*(\||/|,)\s*'[^']+'")           # 'save' | 'miss'
_COMPARISON = re.compile(r"(>=|<=|>|<)\s*-?\d")               # ">= 0", "<= 1"

DOMAIN_TESTS = (("magnitude", _MAGNITUDE), ("unit-range", _UNIT_RANGE),
                ("enum", _ENUM), ("comparison", _COMPARISON))

_VERSION = re.compile(r"^(\d+)")


@dataclasses.dataclass(frozen=True)
class Config:
    root: pathlib.Path = dataclasses.field(default_factory=pathlib.Path.cwd)
    migrations: str = "migrations"
    baseline: pathlib.Path = pathlib.Path("migrations/.column_comment_baseline.json")
    #: files numbered at or below this shipped before the rule existed. Raising it grandfathers;
    #: lowering it tightens. Never edit an applied migration to satisfy the gate.
    legacy_max: int = 0
    #: how the gate names itself in its one-line verdicts
    label: str = "column-comment ratchet"
    cmd: str = "ci-column-comments"

    def ratchet(self) -> RatchetSet:
        p = self.baseline if self.baseline.is_absolute() else self.root / self.baseline
        return RatchetSet(p)


def sql_files(cfg: Config) -> list[pathlib.Path]:
    """Tracked `<migrations>/*.sql` (one level, `git ls-files`); [] when the directory is absent."""
    if not (cfg.root / cfg.migrations).is_dir():
        return []
    prefix = cfg.migrations.strip("/") + "/"
    rels = discover.tracked((cfg.migrations,), suffix=".sql", root=cfg.root)
    return sorted(cfg.root / r for r in rels if "/" not in r[len(prefix):])


def version(path: pathlib.Path) -> int:
    m = _VERSION.match(path.name)
    return int(m.group(1)) if m else 0


def declared_domains(sql: str) -> dict[str, str]:
    """-> {column: which domain phrase it declares}. Only lines inside a CREATE TABLE body."""
    out: dict[str, str] = {}
    in_table = False
    for raw in sql.splitlines():
        low = raw.strip().lower()
        if low.startswith("create table"):
            in_table = True
            continue
        if in_table and low.startswith((");", ")")):
            in_table = False
            continue
        if not in_table:
            continue
        m = _COL_LINE.match(raw)
        if not m:
            continue
        comment = m.group("comment")
        for label, rx in DOMAIN_TESTS:
            if rx.search(comment):
                out[m.group("col").lower()] = label
                break
    return out


def constrained_columns(sql: str) -> set[str]:
    """Columns named inside any CHECK (...) in the file -- the enforcement side."""
    cols: set[str] = set()
    for body in re.findall(r"CHECK\s*\((.*?)\)\s*(?:NOT\s+VALID)?", sql, re.I | re.S):
        cols.update(t.lower() for t in re.findall(r"[a-z_][a-z0-9_]*", body, re.I))
    return cols


def offenders(cfg: Config, files: Sequence[pathlib.Path] | None = None) -> list[str]:
    baseline = cfg.ratchet().load()
    out: list[str] = []
    for path in sql_files(cfg) if files is None else files:
        if version(path) <= cfg.legacy_max:
            continue
        sql = path.read_text(encoding="utf-8")
        checked = constrained_columns(sql)
        for col, label in declared_domains(sql).items():
            key = f"{path.name}:{col}"
            if col in checked or key in baseline:
                continue
            out.append(f"{key} — the comment declares a {label} domain but no CHECK enforces it")
    return out


def selftest(cfg: Config) -> list[str]:
    """Both directions on a table built here: the declared domains fire, the described ones do not."""
    nl = chr(10)
    table = nl.join((
        "CREATE TABLE s.example (",
        "    id          bigint PRIMARY KEY,",
        "    mae_pct     double precision,   -- max adverse excursion",
        "    hit_rate    double precision,   -- 0..1 share of hits",
        "    verdict     text,               -- 'save' | 'miss'",
        "    hold_min    double precision,   -- capped hold, >= 0",
        "    note        text                -- free-text operator note",
        ");"))
    fails: list[str] = []
    want = {"mae_pct": "magnitude", "hit_rate": "unit-range", "verdict": "enum", "hold_min": "comparison"}
    got = declared_domains(table)
    if got != want:
        fails.append(f"declared domains: expected {want}, got {got}")
    enforced = table + nl + "ALTER TABLE s.example ADD CHECK (mae_pct >= 0) NOT VALID;"
    if "mae_pct" not in constrained_columns(enforced):
        fails.append("a CHECK naming the column was not read as enforcement")
    if declared_domains("ALTER TABLE s.x ADD COLUMN y float8; -- max adverse excursion"):
        fails.append("a comment outside a CREATE TABLE body was read as a declaration")
    return fails


def main(argv: Sequence[str] | None = None, cfg: Config | None = None, *,
         description: str | None = None) -> int:
    ap = argparse.ArgumentParser(description=description or __doc__)
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="prove the rule can fire")
    if cfg is None:
        ap.add_argument("--root", type=pathlib.Path, default=pathlib.Path.cwd())
        ap.add_argument("--migrations", default=Config.migrations, metavar="DIR")
        ap.add_argument("--baseline", type=pathlib.Path, default=None,
                        help="json baseline (default: <migrations>/.column_comment_baseline.json)")
        ap.add_argument("--legacy-max", type=int, default=0,
                        help="files numbered at or below this are grandfathered")
    args = ap.parse_args(argv)
    if cfg is None:
        baseline = args.baseline or pathlib.Path(args.migrations) / ".column_comment_baseline.json"
        cfg = Config(root=args.root.resolve(), migrations=args.migrations, baseline=baseline,
                     legacy_max=args.legacy_max)
    c = cfg

    def write() -> int:
        keys = [o.split(" — ")[0] for o in offenders(c)]
        c.ratchet().write(keys)
        print(f"baseline written: {len(keys)} grandfathered")
        return 0

    def scan() -> list[str]:
        files = sql_files(c)
        if not files:
            print(f"{c.label}: no {c.migrations}/*.sql under {c.root} -- nothing was read, "
                  "so nothing was verified (exit 2, could not look)", file=sys.stderr)
            raise SystemExit(2)
        return offenders(c, files)

    def report(probs: Sequence[str]) -> None:
        if not probs:
            print(f"{c.label}: clean")
            return
        print("A column comment states a domain that nothing enforces.\n"
              "Add a CHECK (NOT VALID first if the table has history), or — if the domain genuinely\n"
              f"cannot be expressed — run: {c.cmd} --write-baseline\n")
        for p in probs:
            print(f"  {p}")

    return site_lint.check(
        _cli.flags(args, "selftest", "write_baseline"), gate=c.label, scan=scan,
        selftest=lambda: selftest(c), selftest_report=_cli.selftest_report(f"{c.label}: selftest"),
        modes={"--write-baseline": write}, report=report)


if __name__ == "__main__":
    raise SystemExit(main())
