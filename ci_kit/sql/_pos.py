"""Which python line holds which characters of a SQL value -- for a line-scoped waiver.

python folds an implicitly concatenated literal (`"SELECT a "  "FROM t"`, over several lines)
into ONE `ast.Constant`, and an f-string's constant parts likewise, so the AST cannot say which
line a character came from. python's own TOKENS can: every STRING / FSTRING_MIDDLE token has a
position. Re-joining them (a hole filled exactly as `sqlast._parse` filled it) must give back
the parsed text, or there is no map and the waiver is held strict by `Site.waived`.

Split out of `sqlast.py` at review, which found a line waiver on one line of a concatenated
literal waiving every clock in the statement.
"""

from __future__ import annotations

import ast
import io
import tokenize

NL = chr(10)
BACKSLASH = chr(92)
_SKIP = {tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.FSTRING_START,
         tokenize.FSTRING_END}
#: a literal piece: (text, python line) -- or None for a hole
Part = tuple[str, int] | None


def py_tokens(source: str) -> tuple[dict[int, str], list[tokenize.TokenInfo]]:
    """python `#` comments by line, and every token (for `segments`)."""
    comments: dict[int, str] = {}
    toks = list(tokenize.generate_tokens(io.StringIO(source).readline))
    for tok in toks:
        if tok.type == tokenize.COMMENT:
            comments[tok.start[0]] = tok.string
    return comments, toks


def _char_col(lines: list[str], line: int, byte_col: int) -> int:
    """ast columns are UTF-8 byte offsets; tokenize's are characters."""
    return len(lines[line - 1].encode()[:byte_col].decode(errors="ignore"))


def _split(text: str, line: int) -> list[Part]:
    parts = text.split(NL)
    return [(p + (NL if k < len(parts) - 1 else ""), line + k) for k, p in enumerate(parts)]


def _parts(toks: list[tokenize.TokenInfo]) -> list[Part] | None:
    """The value as source-ordered literal pieces and holes; None where the tokens cannot be
    re-joined faithfully (an escape in an f-string, a literal whose newlines are escapes)."""
    out: list[Part] = []
    brace = 0
    in_f = 0
    for tok in toks:
        if tok.type == tokenize.FSTRING_START:
            in_f += 1
        elif tok.type == tokenize.FSTRING_END:
            in_f -= 1
        if brace:
            brace += {"{": 1, "}": -1}.get(tok.string, 0) if tok.type == tokenize.OP else 0
        elif tok.type == tokenize.OP and tok.string == "{" and in_f:
            out.append(None)
            brace = 1
        elif tok.type == tokenize.FSTRING_MIDDLE:
            if BACKSLASH in tok.string:
                return None
            out.extend(_split(tok.string, tok.start[0]))
        elif tok.type == tokenize.STRING:
            value = ast.literal_eval(tok.string)
            if value.count(NL) != tok.string.count(NL):
                return None
            out.extend(_split(value, tok.start[0]))
        elif tok.type in _SKIP or (tok.type == tokenize.OP and tok.string in "+()"):
            continue
        elif out[-1:] != [None]:
            out.append(None)                # a non-literal operand of a `+` chain
    return out


def segments(node: ast.expr, source_lines: list[str], toks: list[tokenize.TokenInfo],
             fill: tuple[str, ...], text: str) -> list[tuple[int, int, str]]:
    """(offset in `text`, python line, chunk) for every literal piece of `node`, or [] when
    re-joining its tokens with `fill` does not reproduce `text` (binds unprefixed)."""
    end = node.end_lineno or node.lineno
    lo = (node.lineno, _char_col(source_lines, node.lineno, node.col_offset))
    hi = (end, _char_col(source_lines, end, node.end_col_offset or 0))
    parts = _parts([t for t in toks if lo <= t.start and t.end <= hi])
    if parts is None or parts.count(None) != len(fill):
        return []
    out: list[tuple[int, int, str]] = []
    holes = iter(fill)
    at = 0
    line = node.lineno
    for part in parts:
        chunk, line = (f" {next(holes)} ", line) if part is None else part
        out.append((at, line, chunk))
        at += len(chunk)
    return out if "".join(c for *_, c in out) == text else []
