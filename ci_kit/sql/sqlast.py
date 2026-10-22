"""SQL statements out of python source, PARSED — sqlglot's postgres dialect, one reader for every
SQL gate. Replaces per-gate regexes over SQL text (first written for a service repo's gates,
2026-09-25; moved here so a second repo reads SQL the same way instead of copying the reader).

WHY A PARSER AND NOT A PATTERN. A regex over SQL text cannot tell a use from a mention: `'a::b'`
inside a string literal, `-- CAST(:x AS date)` on a commented-out line, `:x` in a comment all read
as code, and a statement wrapped across lines hides from a line scan. sqlglot's tokenizer
classifies comments and string literals, and its parser hands each gate the NODE it is asking
about (a Cast whose operand is a bind, a Table named X, a CurrentTimestamp) instead of a shape.

THE PIPELINE, one python module at a time:

  1. python's own AST finds the string VALUES: a constant, an f-string, or a `+` chain of them
     (a folded chain is ONE statement, never its fragments). Bare string expressions —
     docstrings, prose blocks — are never executed and are skipped, and so is a pattern handed
     to `re.*`. An interpolation is a hole; a module/class string constant interpolated by name
     (`{self._CADENCE_DAYS}`) is INLINED, and read there rather than alone.
  2. A value is SQL when its head, past any leading comment, OPENS a statement — SELECT,
     INSERT INTO, UPDATE .. SET, DELETE FROM, WITH x [(cols)] AS / WITH RECURSIVE, CREATE
     [TEMP] TABLE .. AS SELECT — read from sqlglot's tokens (`_head.py`).
  3. SQLAlchemy binds (`:name`) are prefixed `b__` first: sqlglot models `:name` as a
     Placeholder, except when the name is a keyword (`:like`, `:out` do not parse).
  4. Parse, filling holes as a name, then as nothing (a clause fragment such as
     `{sym_clause}`), then as an operator, then every mix. Still failing, a `{field}` in the
     text is treated as a `str.format` hole and the fills retried.

A SQL VALUE THAT WILL NOT PARSE EXITS 2, NEVER PASSES — and so does one sqlglot could only keep
as an opaque `Command`, which is a parse that read nothing. Measured on the first consumer's
tree (2026-09-25): 1671 SQL values, every one parsed. Prose that opens with a SQL verb
("SELECT INTO is not permitted") therefore stops a gate: reword it.

POSITIONS. sqlglot records a line on identifiers only; `_PG.Parser` stamps every node with the
line of the last token it consumed, which is what a line-scoped waiver (`-- pit-ok`) needs.
`Site.py_line` maps it back to the file exactly when the literal's newlines ARE the file's (a
triple-quoted string); an implicitly concatenated literal is mapped through python's STRING
tokens. A node that maps to no python line holds its waiver STRICT (its own SQL line), never
widened to the value: that widening once let one `# pit-ok` waive every clock in a statement.
"""

from __future__ import annotations

import ast
import itertools
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field

from sqlglot import exp
from sqlglot.dialects.postgres import Postgres
from sqlglot.errors import SqlglotError
from sqlglot.tokens import Token, TokenType

from ci_kit import astread
from ci_kit.sql._head import leads_sql
from ci_kit.sql._pos import py_tokens, segments

INTERP = "__interp__"
BIND_PREFIX = "b__"
_MAX_MIXES = 5            # 3**5 fill mixes, then give up and exit 2

#: sqlglot LOGS "unsupported syntax. Falling back to parsing as a 'Command'" and returns an
#: opaque node. The node is refused below; the log line is only noise on a gate's stderr.
logging.getLogger("sqlglot").setLevel(logging.ERROR)


class _PG(Postgres):
    class Parser(Postgres.Parser):
        def expression(self, instance, token=None, comments=None):  # noqa: ANN001, ANN201
            out = super().expression(instance, token, comments)
            if "line" not in out.meta and self._prev is not None:
                out.update_positions(self._prev)
            return out


_DIALECT = _PG()


class Unparsed(Exception):
    """A SQL value this reader could not turn into statements: the gate did not look at it."""


@dataclass
class Site:
    """One SQL value in one python file, parsed."""

    relpath: str
    line: int                                  # python line the value starts on
    end_line: int
    text: str                                  # what was parsed: binds prefixed, holes filled
    statements: list[exp.Expression]
    tokens: list[Token]
    comments: dict[int, str] = field(default_factory=dict)   # python `#` comments by line
    #: (offset in the unprefixed value, python line, text) per physical line of the literal
    segments: list[tuple[int, int, str]] = field(default_factory=list)

    @property
    def exact(self) -> bool:
        """True when SQL line N is python line `line + N - 1` (a triple-quoted literal)."""
        return self.text.count("\n") == self.end_line - self.line

    def py_line(self, sql_line: int | None) -> int:
        if sql_line is None or not self.exact:
            return self.line
        return self.line + sql_line - 1

    def waived(self, token: str, node: exp.Expression | None = None) -> bool:
        """`token` in a SQL comment on the node's python line, or a python comment on it.

        No node: anywhere in the value (a value-scoped waiver). A node whose python line cannot
        be named is held STRICT -- its own SQL line only -- never widened to the whole value:
        one `# pit-ok` on a concatenated literal once waived every clock in it (found at review)."""
        # sqlglot hangs a comment on a neighbouring TOKEN, not on a line, so "in a SQL comment
        # on this line" is: the token is in some comment AND in this line's text.
        in_comment = any(token in c for t in self.tokens for c in t.comments)
        if node is None:
            span = range(self.line, self.end_line + 1)
            return in_comment or any(token in self.comments.get(n, "") for n in span)
        mapped = self._mapped(node)
        if mapped is not None:
            py, chunk = mapped
            return (in_comment and token in chunk) or token in self.comments.get(py, "")
        line = line_of(node)
        sql_lines = self.text.split("\n")
        on_line = line is not None and 0 < line <= len(sql_lines) and token in sql_lines[line - 1]
        if self.exact and line is not None:
            return (in_comment and on_line) or token in self.comments.get(self.py_line(line), "")
        one_line = self.line == self.end_line
        return (in_comment and on_line) or (one_line and token in self.comments.get(self.line, ""))

    def _mapped(self, node: exp.Expression) -> tuple[int, str] | None:
        """(python line, that line's SQL text) for a node in an implicitly concatenated literal,
        which python folds into ONE constant: its STRING tokens say which line holds which
        characters. None when the value is not such a literal (an f-string, a `+` chain)."""
        off = next((int(n.meta["start"]) for n in node.walk()
                    if isinstance(n, exp.Expression) and "start" in n.meta), None)
        if not self.segments or off is None:
            return None
        raw = off - len(BIND_PREFIX) * self.text[:off].count(":" + BIND_PREFIX)
        py = next((ln for start, ln, _ in reversed(self.segments) if start <= raw), None)
        if py is None:
            return None
        return py, "".join(chunk for _, ln, chunk in self.segments if ln == py)


def line_of(node: exp.Expression) -> int | None:
    """The SQL line a node ends on (see `_PG`), or its first positioned descendant's."""
    for n in node.walk():
        if isinstance(n, exp.Expression) and "line" in n.meta:
            return int(n.meta["line"])
    return None


def is_bind(node: exp.Expression | None) -> bool:
    return isinstance(node, exp.Placeholder) and str(node.this).startswith(BIND_PREFIX)


def bind_name(node: exp.Placeholder) -> str:
    return str(node.this)[len(BIND_PREFIX):]


def _ref_name(node: ast.expr) -> str | None:
    """`X` or `self.X` / `cls.X` — how a module or class constant is spelled at its use."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
            and node.value.id in ("self", "cls"):
        return node.attr
    return None


def _format_pieces(text: str) -> list[str | None]:
    """`str.format` text as pieces: each `{...}` field is a hole, `{{` / `}}` a brace."""
    out: list[str | None] = []
    buf: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "{}" and text[i + 1:i + 2] == ch:
            buf.append(ch)
            i += 2
            continue
        if ch == "{" and (end := text.find("}", i)) > i:
            out.extend(["".join(buf), None])
            buf, i = [], end + 1
            continue
        buf.append(ch)
        i += 1
    return [*out, "".join(buf)]


class _Module:
    """What the fold needs to know about a whole module before it reads one value."""

    def __init__(self, tree: ast.Module) -> None:
        self.named: dict[str, ast.Constant] = {}
        for scope in [tree, *(n for n in tree.body if isinstance(n, ast.ClassDef))]:
            for st in scope.body:
                if (isinstance(st, ast.Assign) and len(st.targets) == 1
                        and isinstance(st.targets[0], ast.Name)
                        and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str)):
                    self.named[st.targets[0].id] = st.value
        skip: set[int] = set()        # prose, pieces of a larger value, `re.*` patterns
        cands: list[ast.expr] = []
        for n in ast.walk(tree):      # ONE walk: this runs over every module, for every gate
            if isinstance(n, ast.Expr):
                skip.add(id(n.value))
            elif isinstance(n, ast.JoinedStr):
                skip.update(id(v) for v in n.values)
                cands.append(n)
            elif isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
                skip.update((id(n.left), id(n.right)))
                cands.append(n)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str):
                cands.append(n)
            elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.args
                    and isinstance(n.func.value, ast.Name) and n.func.value.id == "re"):
                skip.add(id(n.args[0]))
        self._cands = [n for n in cands if id(n) not in skip]

    def _fold(self, node: ast.expr, out: list[str | None], used: set[int]) -> bool:
        """Flatten a str-valued expression into pieces; `None` is a hole."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
            return True
        if isinstance(node, ast.JoinedStr):
            for v in node.values:
                if isinstance(v, ast.Constant):
                    out.append(v.value)
                elif isinstance(v, ast.FormattedValue):
                    self._ref(v.value, out, used)
            return True
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self._fold(node.left, out, used), self._fold(node.right, out, used)
            return left or right
        self._ref(node, out, used)
        return False

    def _ref(self, node: ast.expr, out: list[str | None], used: set[int]) -> None:
        name = _ref_name(node)
        const = self.named.get(name) if name else None
        if const is None:
            out.append(None)
            return
        used.add(id(const))
        out.append(str(const.value))

    def sql_values(self) -> list[tuple[ast.expr, list[str | None]]]:
        """Every value that opens a statement, minus the named fragments one of them inlined —
        `_CADENCE_DAYS = "WITH days AS (...),"` is half a statement and is read inside the
        statement it belongs to."""
        found: list[tuple[ast.expr, list[str | None]]] = []
        inlined: set[int] = set()
        for n in self._cands:
            pieces: list[str | None] = []
            used: set[int] = set()
            if self._fold(n, pieces, used) and leads_sql(
                    "".join(p if p is not None else INTERP for p in pieces)):
                found.append((n, pieces))
                inlined |= used
        return [(n, p) for n, p in found if id(n) not in inlined]


def _prefix_binds(text: str) -> str:
    toks = _DIALECT.tokenize(text)
    out: list[str] = []
    pos = 0
    for a, b in itertools.pairwise(toks):
        if a.token_type == TokenType.COLON and b.start == a.end + 1 and b.text.isidentifier():
            out.append(text[pos:b.start] + BIND_PREFIX)
            pos = b.start
    return "".join(out) + text[pos:]


def _parse(pieces: list[str | None]) -> tuple[str, list[exp.Expression], list[Token],
                                              tuple[str, ...]]:
    """The first fill that parses wins. A fill only ever stands in for text the file does not
    spell, so none can invent a finding except a column named `__interp__`, which every gate
    ignores."""
    holes = pieces.count(None)
    fills: Iterator[tuple[str, ...]] = iter([(INTERP,) * holes, ("",) * holes])
    if 0 < holes <= _MAX_MIXES:
        fills = itertools.chain(fills, itertools.product((INTERP, "", "="), repeat=holes))
    first_error: Exception | None = None
    for fill in fills:
        it = iter(fill)
        try:
            text = _prefix_binds("".join(p if p is not None else f" {next(it)} " for p in pieces))
            tokens = _DIALECT.tokenize(text)
            stmts = [s for s in _DIALECT.parser().parse(tokens, text) if s is not None]
        except SqlglotError as exc:
            first_error = first_error or exc
            continue
        opaque = [s for s in stmts if isinstance(s, exp.Command)]
        if opaque:
            first_error = first_error or Unparsed(
                f"sqlglot kept `{opaque[0].sql()[:60]}` only as an opaque Command")
            continue
        return text, stmts, tokens, fill
    raise Unparsed(" ".join(str(first_error).split())[:300])


def sites(relpath: str, source: str, tree: ast.Module) -> list[Site]:
    """Every SQL value in one parsed python module. Raises `Unparsed` naming file and line."""
    values = _Module(tree).sql_values()
    comments, toks = py_tokens(source) if values else ({}, [])
    lines = source.split(chr(10))
    out: list[Site] = []
    for node, pieces in values:
        try:
            try:
                text, stmts, tokens, fill = _parse(pieces)
            except Unparsed:
                # a `str.format` template (`FROM {table}`) is known only by failing as SQL: the
                # `.format` call is usually on a NAME, far from the literal
                templ = [q for p in pieces for q in (_format_pieces(p) if p is not None else [p])]
                if templ == pieces:
                    raise
                text, stmts, tokens, fill = _parse(templ)
        except (Unparsed, SqlglotError) as exc:
            raise Unparsed(f"{relpath}:{node.lineno}: a SQL value sqlglot could not parse "
                           f"({exc}). Fix the SQL; if it is prose, do not open it with a SQL "
                           f"verb") from exc
        out.append(Site(relpath, node.lineno, node.end_lineno or node.lineno, text, stmts,
                        tokens, comments,
                        segments(node, lines, toks, fill, text.replace(":" + BIND_PREFIX, ":"))))
    return out


def one(sql: str, relpath: str = "<selftest>") -> Site:
    """A single SQL string as a Site — for selftests and unit tests. Raises `Unparsed`."""
    src = "q = " + repr(sql)
    got = sites(relpath, src, ast.parse(src))
    if len(got) != 1:
        raise Unparsed(f"expected one SQL value, got {len(got)}: {sql[:60]!r}")
    return got[0]


def module_sites(relpath: str, source: str, *, gate: str) -> list[Site]:
    """`sites()` for a gate: an unparseable module or SQL value EXITS 2 via `astread.blind`.

    Memoized on (path, text): a test suite drives several gates over the same tree in one
    process, and the parse is the whole cost. Callers must not mutate what they get back."""
    key = (relpath, source)
    if key in _MEMO:
        return _MEMO[key]
    tree = astread.parse(source, relpath, gate=gate)
    try:
        got = sites(relpath, source, tree)
    except Unparsed as exc:
        astread.blind(relpath, exc, gate=gate)
    if len(_MEMO) > 8192:
        _MEMO.clear()
    _MEMO[key] = got
    return got


_MEMO: dict[tuple[str, str], list[Site]] = {}
