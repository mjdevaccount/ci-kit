"""Does a python string OPEN a SQL statement? The classifier `sqlast` runs before it parses.

Tens of thousands of python strings, few of them SQL: a cheap text cut first, then sqlglot's
tokens decide. A value that CANNOT be tokenized is SQL (fail closed): the parse that follows
then refuses it and the gate exits 2, where answering "not SQL" would let every gate skip it.

Split out of `sqlast.py` at review, which found two holes a head-only read had: a value opening
with a `--` / `/* */` comment, and `WITH t(a) AS`.

TRUNCATE (v1.9.0): a write the head cut never opened, so no gate read a TRUNCATE value or the
statements chained after it (`TRUNCATE a; DELETE FROM b`). trade-data's `lint_sql_placement`
carried its own fold + parse for it over this package's privates; that branch moved here.
"""

from __future__ import annotations

from sqlglot.dialects.postgres import Postgres
from sqlglot.errors import SqlglotError
from sqlglot.tokens import Token, TokenType

_DIALECT = Postgres()
#: a hole in a folded value (an f-string `{x}`, a name the fold could not reach)
INTERP = "__interp__"
_HEADS = ("SELECT", "INSERT", "UPDATE", "DELETE", "WITH", "CREATE", "TRUNCATE")
_LEAD_PAIRS = {TokenType.INSERT: TokenType.INTO, TokenType.DELETE: TokenType.FROM}
#: what may stand between UPDATE and SET: `[ONLY] schema.table [AS] alias`
_UPDATE_TARGET = {TokenType.VAR, TokenType.DOT, TokenType.ALIAS, TokenType.ONLY}
#: `CREATE [TEMP] TABLE schema.name AS` -- the SELECT it materializes is a read like any other
_CREATE_AS = {TokenType.TEMPORARY, TokenType.TABLE, TokenType.VAR, TokenType.DOT}


def skip_lead(text: str) -> str:
    """`text` past leading whitespace, `(`, `-- line` and `/* block */` comments."""
    while True:
        text = text.lstrip(" \t\r\n(")
        if text.startswith("--"):
            nl = text.find("\n")
            text = "" if nl < 0 else text[nl + 1:]
        elif text.startswith("/*"):
            end = text.find("*/")
            text = "" if end < 0 else text[end + 2:]
        else:
            return text


def leads_sql(text: str) -> bool:
    """Does the value OPEN a statement? The head is tokenized cut at the first quote, so an
    apostrophe in a prose tail cannot make a real statement 'not SQL' or vice versa."""
    body = skip_lead(text)
    if not body[:8].upper().startswith(_HEADS):     # 8 = len("TRUNCATE"), the longest head
        return False
    try:
        toks = [t for t in _DIALECT.tokenize(body[:80].split("'", 1)[0])
                if t.token_type != TokenType.L_PAREN]
    except SqlglotError:
        return True
    kinds = [t.token_type for t in toks]
    first = kinds[0] if kinds else None
    if first == TokenType.SELECT:
        return True
    if first == TokenType.WITH:
        return _with_as(kinds[1:])
    if first in _LEAD_PAIRS:
        return len(kinds) > 1 and kinds[1] == _LEAD_PAIRS[first]
    if first == TokenType.UPDATE:
        return _update_sets(body)
    if first == TokenType.CREATE:
        return _create_as(kinds[1:])
    if first == TokenType.TRUNCATE:
        return len(toks) > 1 and (body[:8] == "TRUNCATE" or _truncate_target(toks[1:3]))
    return False


def _truncate_target(toks: list[Token]) -> bool:
    """`truncate [TABLE|ONLY] s.t` / `truncate s.t` / `truncate {hole}` -- never "truncate
    disabled" or "Truncate to the cap": prose reads exactly like an unqualified lower-case target,
    so that one shape is not SQL. Spelled upper-case (`TRUNCATE x`) it always is. A bare verb
    (`"TRUNCATE"` in a deny-list) is not, like a bare `"INSERT"` or `"DELETE"`."""
    kinds = [t.token_type for t in toks]
    return (kinds[:1] in ([TokenType.TABLE], [TokenType.ONLY]) or kinds[1:2] == [TokenType.DOT]
            or (bool(toks) and toks[0].text == INTERP))


def _with_as(kinds: list[TokenType]) -> bool:
    """`WITH x AS (`, `WITH x(a, b) AS (`, `WITH RECURSIVE` -- never "with no rows ..."."""
    if kinds[:1] == [TokenType.RECURSIVE]:
        return True
    if len(kinds) < 2 or kinds[0] != TokenType.VAR:
        return False
    rest = kinds[1:]
    if rest[0] == TokenType.ALIAS:
        return True
    for i, kind in enumerate(rest):           # a column list: `a, b)` then AS
        if kind == TokenType.R_PAREN:
            return rest[i + 1:i + 2] == [TokenType.ALIAS]
        if kind not in (TokenType.VAR, TokenType.COMMA):
            return False
    return False


def _update_sets(text: str) -> bool:
    """`UPDATE <target> SET`, read from the WHOLE value's tokens: a `-- comment` between the
    table and SET (with an apostrophe in it) hid a real UPDATE from a head-only read."""
    try:
        toks = _DIALECT.tokenize(text)
    except SqlglotError:
        return True
    for tok in toks[1:8]:
        if tok.token_type == TokenType.SET:
            return True
        if tok.token_type not in _UPDATE_TARGET:
            return False
    return False


def _create_as(kinds: list[TokenType]) -> bool:
    for i, kind in enumerate(kinds[:6]):
        if kind == TokenType.ALIAS:
            return i > 0 and TokenType.TABLE in kinds[:i] \
                and kinds[i + 1:i + 2] in ([TokenType.SELECT], [TokenType.WITH])
        if kind not in _CREATE_AS:
            return False
    return False
