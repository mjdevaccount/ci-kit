"""The shared SQL reader (ci_kit.sql.sqlast) every SQL gate stands on since B13.

A gate's selftest proves its RULE; these prove what the rule is handed: which python strings are
SQL, how holes and binds are filled, where a node sits, and that a value sqlglot cannot read is
a refusal (exit 2 through the gate) rather than an empty list.
"""

from __future__ import annotations

import ast

import pytest
from sqlglot import exp

from ci_kit.sql import sqlast as _sqlast

NL = chr(10)


def _sites(src: str) -> list[_sqlast.Site]:
    return _sqlast.sites("x.py", src, ast.parse(src))


def test_a_docstring_and_a_re_pattern_are_not_SQL() -> None:
    src = ('def f():' + NL + '    """SELECT 1 FROM t is prose here."""' + NL
           + '    re.compile("SELECT (.*) FROM")' + NL + '    return q("SELECT 2 FROM u")')
    assert [s.text for s in _sites(src)] == ["SELECT 2 FROM u"]


def test_prose_that_does_not_OPEN_a_statement_is_not_SQL() -> None:
    assert _sites('msg = "with no rows, UPDATE the baseline"') == []
    assert _sites('msg = "delete the file and select another"') == []


def test_an_UPDATE_with_a_comment_before_SET_is_SQL() -> None:
    """Found by B13's own coverage check: a head-only read missed five real UPDATEs."""
    src = "q('''UPDATE ops.jobs" + NL + "   -- the attempt's clock" + NL + "   SET a = now()''')"
    assert len(_sites(src)) == 1


def test_a_concatenation_is_ONE_statement_and_its_fragments_are_not_read_alone() -> None:
    src = 'q("SELECT a " "FROM t " + "WHERE b = :b")'
    got = _sites(src)
    assert len(got) == 1 and got[0].statements[0].find(exp.Where) is not None


def test_binds_are_prefixed_so_a_keyword_name_parses() -> None:
    site = _sqlast.one("SELECT 1 FROM t WHERE a LIKE :like AND b = :out")
    names = sorted(_sqlast.bind_name(p) for p in site.statements[0].find_all(exp.Placeholder))
    assert names == ["like", "out"]


def test_fstring_holes_fill_as_names_clauses_and_operators() -> None:
    assert len(_sites('q(f"SELECT {cols} FROM t WHERE a = 1 {and_clause}")')) == 1
    assert len(_sites('q(f"SELECT a FROM t WHERE b {op} :x")')) == 1


def test_a_named_fragment_constant_is_inlined_where_it_is_used() -> None:
    src = ('_WIN = "interval ' + "'1 day'" + '"' + NL
           + 'q(f"SELECT 1 FROM t WHERE ts > now() - {_WIN}")')
    got = _sites(src)
    assert len(got) == 1 and got[0].statements[0].find(exp.Interval) is not None


def test_a_format_template_is_retried_as_one() -> None:
    src = 'SQL = "SELECT a FROM {table} WHERE b = :b"' + NL + 'q(SQL.format(table="t"))'
    assert len(_sites(src)) == 1


def test_an_unparseable_value_RAISES_it_is_never_an_empty_list() -> None:
    with pytest.raises(_sqlast.Unparsed, match=r"x\.py:1"):
        _sites("q(" + repr("SELECT FROM WHERE") + ")")


def test_an_opaque_Command_parse_is_refused() -> None:
    """sqlglot falls back to an opaque `Command` for syntax it cannot model: a parse that read
    nothing, so it must not reach a gate as a statement."""
    with pytest.raises(_sqlast.Unparsed):
        _sqlast.one("SELECT 1 FROM t WHERE a = 1 FOR NO KEY UPDATE OF t SKIP LOCKED NOWAIT x y")


def test_module_sites_EXITS_2_on_either_kind_of_blindness() -> None:
    for src in ("def broken(:", "q(" + repr("SELECT FROM WHERE") + ")"):
        with pytest.raises(SystemExit) as got:
            _sqlast.module_sites("x.py", src, gate="t")
        assert got.value.code == 2


def test_every_node_carries_a_line_and_py_line_maps_it_back() -> None:
    src = "x = 1" + NL + "q('''" + NL + "SELECT a" + NL + "  FROM t WHERE ts > now()''')"
    site = _sites(src)[0]
    now = site.statements[0].find(exp.CurrentTimestamp)
    assert _sqlast.line_of(now) == 3 and site.py_line(3) == 4


def test_a_waiver_is_scoped_to_its_line_in_an_exact_literal() -> None:
    src = ("q('''" + NL + "SELECT now()  -- pit-ok" + NL + "     , current_date FROM t''')")
    site = _sites(src)[0]
    now = site.statements[0].find(exp.CurrentTimestamp)
    date = site.statements[0].find(exp.CurrentDate)
    assert site.waived("pit-ok", now) and not site.waived("pit-ok", date)


def test_a_python_comment_waives_its_own_line() -> None:
    site = _sites('q("SELECT now()")  # pit-ok')[0]
    assert site.waived("pit-ok", site.statements[0].find(exp.CurrentTimestamp))


@pytest.mark.parametrize("lead", ["-- live read" + NL, "/* live read */ ", "-- a" + NL + "/* b */" + NL])
def test_a_value_OPENING_with_a_comment_is_SQL(lead: str) -> None:
    """B13 review: `_leads_sql` read the comment as the head, and every gate skipped the value."""
    assert len(_sites("q(" + repr(lead + "SELECT now() FROM t") + ")")) == 1


def test_WITH_a_column_list_and_CREATE_TABLE_AS_are_SQL_and_prose_is_not() -> None:
    assert len(_sites("q(" + repr("WITH t(a, b) AS (SELECT 1, 2) SELECT a FROM t") + ")")) == 1
    assert len(_sites("q(" + repr("CREATE TEMP TABLE x AS SELECT a FROM t") + ")")) == 1
    assert _sites('msg = "create the table as described"') == []
    assert _sites('msg = "with a view to the future"') == []


def test_a_head_the_tokenizer_REFUSES_fails_closed() -> None:
    """Not-SQL would let every gate skip it; SQL sends it to the parse, which exits 2."""
    with pytest.raises(_sqlast.Unparsed):
        _sites("q(" + repr('SELECT "unterminated FROM t') + ")")


def test_a_waiver_on_one_line_of_a_CONCATENATED_literal_covers_that_line_only() -> None:
    """python folds `"a" "b"` over several lines into ONE constant; B13 review found a
    `# pit-ok` on its first line waiving every clock in it."""
    src = ('q("SELECT current_date "  # pit-ok' + NL + '  "     , now() FROM t "' + NL
           + '  "WHERE a = :a")')
    site = _sites(src)[0]
    date = site.statements[0].find(exp.CurrentDate)
    now = site.statements[0].find(exp.CurrentTimestamp)
    assert site.waived("pit-ok", date) and not site.waived("pit-ok", now)


def test_an_fstring_waiver_is_per_line_across_holes() -> None:
    src = ('q(f"SELECT {col}, now() "  # pit-ok' + NL + '  f"FROM {tbl} WHERE ts > now()")')
    site = _sites(src)[0]
    first, second = list(site.statements[0].find_all(exp.CurrentTimestamp))
    assert {site.waived("pit-ok", first), site.waived("pit-ok", second)} == {True, False}


def test_a_node_with_no_python_line_is_held_STRICT_never_widened() -> None:
    """A named fragment inlined into the value has no tokens where it is used."""
    src = ('_W = "now()"' + NL + 'q(f"SELECT current_date, "  # pit-ok' + NL + '  f"{_W} FROM t")')
    site = _sites(src)[0]
    assert not site.segments
    assert not site.waived("pit-ok", site.statements[0].find(exp.CurrentTimestamp))
