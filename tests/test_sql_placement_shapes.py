"""ci-sql-placement's shapes (moved from the source repo's unit tests with the gate, v1.50.0): a
parsed write statement or a sqlalchemy `text(` is a site; prose, comments, SELECT strings and
look-alikes are not. A parser regression shows as a named shape, not a count."""
from __future__ import annotations

import pytest

pytest.importorskip("sqlglot")

from ci_kit.sql import placement  # noqa: E402


def _n(src: str) -> int:
    return len(placement.sql_sites(src, "app/x.py"))


def test_a_multi_line_f_string_write_with_an_interpolated_table_is_one_site() -> None:
    # the shape a same-line `verb schema.table` scan MISSED
    src = ('SCHEMA = "s"\n\ndef purge(cur, t):\n    cur.execute(f"""\n        DELETE\n'
           '          FROM {SCHEMA}.{t}\n         WHERE day = %s\n    """, (1,))\n')
    assert _n(src) == 1


def test_a_truncate_chain_is_read_through_the_kit() -> None:
    src = 'T = ("s.a",)\nQ = "TRUNCATE " + ", ".join(T) + " RESTART IDENTITY CASCADE"\n'
    assert _n(src) == 1


def test_a_write_chained_after_a_truncate_counts_too() -> None:
    # a review in the source repo: the TRUNCATE branch once counted only the TruncateTable node
    assert _n("Q = 'TRUNCATE s.a; DELETE FROM s.b WHERE x = 1'\n") == 2


def test_a_cte_holding_a_delete_and_an_insert_is_two_writes() -> None:
    assert _n("Q = 'WITH d AS (DELETE FROM s.x RETURNING a) INSERT INTO s.y SELECT a FROM d'\n") == 2


def test_an_upsert_is_one_write_not_an_insert_plus_an_update() -> None:
    assert _n("Q = 'INSERT INTO s.x (a) VALUES (:a) ON CONFLICT (a) DO UPDATE SET a = :a'\n") == 1


@pytest.mark.parametrize(("imp", "call"), [
    ("from sqlalchemy import text", "text('SELECT 1')"),
    ("from sqlalchemy import text as T", "T('SELECT 1')"),
    ("from sqlalchemy.sql import text", "text('SELECT 1')"),
    ("import sqlalchemy as sa", "sa.text('SELECT 1')"),
    ("import sqlalchemy", "sqlalchemy.text('SELECT 1')"),
    ("import sqlalchemy", "sqlalchemy.sql.text('SELECT 1')"),
    ("import sqlalchemy.sql", "sqlalchemy.sql.expression.text('SELECT 1')"),
    ("from sqlalchemy import sql", "sql.text('SELECT 1')"),
    ("import sqlalchemy.sql as s", "s.text('SELECT 1')"),
], ids=["bare", "alias", "sql-submodule", "module-alias", "module",
        "module-sub-path", "dotted-import-deep-path", "from-import-submodule", "submodule-alias"])
def test_every_spelling_of_sqlalchemy_text_is_a_site(imp: str, call: str) -> None:
    assert _n(f"{imp}\nQ = {call}\n") == 1


def test_text_wrapping_a_write_counts_both() -> None:
    assert _n("from sqlalchemy import text\nQ = text('UPDATE s.x SET a = 1')\n") == 2


@pytest.mark.parametrize("src", [
    '"""DELETE FROM s.x and INSERT INTO y in a docstring are prose."""\nx = 1\n',
    "# DELETE FROM s.x -- a comment\nx = 1\n",
    "Q = 'SELECT a FROM s.x -- delete from nothing'\n",
    "r = 'truncate disabled'\nm = 'Truncate to the cap instead'\n",
    "def text(s):\n    return s\nq = text('x')\n",
    "d = {}\nd.update({'a': 1})\n",
], ids=["docstring", "comment", "select", "truncate-prose", "local-text", "dict-update"])
def test_look_alikes_are_not_sites(src: str) -> None:
    assert _n(src) == 0


def test_an_unparseable_sql_value_exits_2_never_passes() -> None:
    with pytest.raises(SystemExit) as e:
        placement.sql_sites("Q = 'INSERT INTO s.x (a VALUES'\n", "app/x.py")
    assert e.value.code == 2


@pytest.mark.parametrize(("rel", "scoped"), [
    ("app/repositories/x_repo.py", False),
    ("app/tear/repositories/y_repo.py", False),
    ("app/content/mail/repositories/z.py", False),
    ("scripts/lint_scoping.py", False),
    ("app/repositories_old/x.py", True),
    ("app/content/repositories/x.py", True),
    ("app/admin/x_router.py", True),
    ("scripts/backfill_x.py", True),
])
def test_owner_and_exempt_prefixes(rel: str, scoped: bool) -> None:
    m = placement.prefix_matcher(["app/repositories/", "app/tear/repositories/", "app/content/*/repositories/",
                                  "scripts/lint_"])
    assert m is not None and (not m.match(rel)) is scoped
