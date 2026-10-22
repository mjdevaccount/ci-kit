"""SQL string literals out of python source — one extractor, not one per gate.

WHY THIS FILE EXISTS (2026-09-18, first written in a service repo). Three gates there each pulled
"SQL-looking string literals" out of python source, three different ways, at three different
correctness levels — and the correct one was written FIRST. One paired raw quote CHARACTERS, so
an apostrophe in a comment opened a literal and a real read could land inside a bogus span and
never be scanned; one ran a non-greedy regex over triple-quoted blocks with no prose skip at all.
THE FINDING WAS NEVER THE APOSTROPHE — it is that a correct pattern sat one file away and had no
path to the next author, which is the same failure as a guard nobody invokes. This module is
that path.

WHAT EXTRACTING IT FOUND, which is why this is not a tidy-up. `tokenize` on python 3.12+ does NOT
emit an f-string as a `STRING` token: it emits FSTRING_START / FSTRING_MIDDLE / OP / FSTRING_END.
The tokenizer fix that closed the apostrophe hole collected `STRING` only, so it opened a NEW one
— **every f-string query became invisible**, on the exact interpreter CI ran (measured: 105
f-string literals that parse as SQL statements in that one repo). Copying the helper a third
time would have propagated it.

CONTRACT
--------
  literals(src)      every string literal as (text, first_line, last_line), 1-indexed.
                     Comments are not strings.  Implicit concatenation is ONE literal.
                     f-strings are reassembled from their token group.  RAISES `Unlexable`.
  prose_lines(src)   every line covered by a bare string EXPRESSION statement — a docstring or
                     a commentary block.  Never executed, so never the SQL a gate protects.
  sql_literals(src)  `literals` minus the ones that START on a prose line — the combination
                     every caller wants.

`Unlexable` IS THE POINT OF RAISING. On an unterminated literal `tokenize` raises and a quote-
pairing regex finds nothing either, so the old code returned `[]` and the file reported CLEAN.
"Nothing here" and "I could not look" are different answers and a gate must not merge them:
callers turn this into exit **2**, never 0 and never 1.
"""

from __future__ import annotations

import ast
import io
import sys
import tokenize

#: Tokens permitted BETWEEN two pieces of one implicitly-concatenated literal. Python allows
#: only whitespace, newlines and comments there, so anything else ends the literal.
_JOINABLE = frozenset({tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT,
                       tokenize.DEDENT})

#: py3.12+ splits f-strings into a token GROUP; py<=3.11 emits one STRING token. `None` on the
#: older lexer, where the STRING branch below already covers f-strings by itself.
_FSTRING_START = getattr(tokenize, "FSTRING_START", None)
_FSTRING_END = getattr(tokenize, "FSTRING_END", None)


class Unlexable(Exception):
    """python could not tokenize this file, so no rule above it can claim anything about it."""


def _span(lines: list[str], start: tuple[int, int], end: tuple[int, int]) -> str:
    """Raw source between two (row, col) token positions; rows are 1-indexed."""
    (r1, c1), (r2, c2) = start, end
    if r1 == r2:
        return lines[r1 - 1][c1:c2]
    out = [lines[r1 - 1][c1:]]
    out.extend(lines[r1:r2 - 1])
    out.append(lines[r2 - 1][:c2])
    return "".join(out)


def literals(src: str) -> list[tuple[str, int, int]]:
    """Every string literal as (text, first_line, last_line), 1-indexed, via python's own lexer.

    Four properties a quote-pairing regex cannot have:

      * A COMMENT IS NOT A STRING. The tokenizer classifies it, so no amount of prose
        punctuation can open, close or shift a literal.
      * IMPLICIT CONCATENATION IS ONE LITERAL. `("SELECT ... " "WHERE source = 'measured'")` is
        two STRING tokens and one SQL statement; scanning the halves separately reports the
        first as unfiltered while the filter sits in the second.
      * AN F-STRING IS A LITERAL. On 3.12+ it is a token GROUP, not a STRING, and a `STRING`-only
        loop drops it silently — see the module docstring for what that cost.
      * A STRING INSIDE AN F-STRING'S REPLACEMENT FIELD IS NOT A SEPARATE LITERAL.
        `f"SELECT {', '.join(cols)} FROM t"` holds a `', '` STRING token that belongs to the
        f-string, and emitting it on its own would split one statement into three.

    The text carries its own quotes and prefix, exactly as the tokenizer saw them — callers
    search it with regexes, and the delimiters are never what they match on.
    """
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError) as exc:
        raise Unlexable(str(exc)) from exc

    lines = src.splitlines(keepends=True)
    out: list[tuple[str, int, int]] = []
    buf: list[str] = []
    first = last = 0
    depth = 0                                    # f-string nesting (3.12+ allows nesting)
    fstart: tuple[int, int] | None = None

    for tok in toks:
        if _FSTRING_START is not None and tok.type == _FSTRING_START:
            if depth == 0:
                fstart = tok.start
            depth += 1
            continue
        if depth:
            # Everything up to the matching FSTRING_END belongs to the f-string, replacement
            # fields included. Only the closer at depth 1 emits.
            if tok.type == _FSTRING_END:
                depth -= 1
                if depth == 0 and fstart is not None:
                    if not buf:
                        first = fstart[0]
                    buf.append(_span(lines, fstart, tok.end))
                    last = tok.end[0]
            continue
        if tok.type == tokenize.STRING:
            if not buf:
                first = tok.start[0]
            buf.append(tok.string)
            last = tok.end[0]
            continue
        if buf and tok.type not in _JOINABLE:
            out.append(("".join(buf), first, last))
            buf = []
    if buf:
        out.append(("".join(buf), first, last))
    return out


def prose_lines(src: str) -> set[int]:
    """Every line covered by a bare string EXPRESSION statement — docstrings and prose blocks.

    A query is always USED: assigned, passed, concatenated, returned. A string that is an
    expression statement on its own is documentation. So this is not a docstring heuristic, it
    is the structural difference between prose and a value.

    Returns an empty set when the source will not parse. This is a PERMISSIVE parse handler,
    and it is permissive in the LOUD direction: empty here means "exclude no lines from the
    scan", so every docstring in the file becomes scannable text and the caller reports MORE,
    never fewer. The failure this kit exists to prevent — a blind state indistinguishable from a
    clean one — needs the opposite bias.

    And the `Unlexable` path must stay reachable: a caller may catch `Unlexable` from `literals`,
    fall back to a cruder pairing on purpose ("a noisy finding beats a silent pass"), and then
    call this. Raising a second exception here would delete the fallback that caller argued for.
    """
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        # blind-ok: empty = exclude nothing = MORE findings. See above.
        return set()
    out: set[int] = set()
    for n in ast.walk(tree):
        if (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)):
            out.update(range(n.value.lineno, (n.value.end_lineno or n.value.lineno) + 1))
    return out


def sql_literals(src: str) -> list[tuple[str, int, int]]:
    """`literals(src)` minus the ones that START on a line covered by prose.

    KNOWN AND ACCEPTED MISS: a statement opening on the LAST line of a docstring
    (`\"\"\"doc.\"\"\"; q = \"\"\"SELECT ...\"\"\"`) is skipped. Tokens cannot overlap, so this is the
    only collision possible, and skipping is the conservative half of it for a gate whose other
    failure mode is reporting its own documentation.
    """
    prose = prose_lines(src)
    return [(text, first, last) for text, first, last in literals(src) if first not in prose]


def selftest(caller: str = "sqllit") -> int:
    """Every shape the three gates covered BETWEEN them, in one place.

    Split across three files these cases were each proven once and inherited nowhere, which is
    the defect this module exists to remove. `expect` is `None` where the right answer is a
    REFUSAL — a boolean cannot express 'did not finish looking', and that is exactly the
    bug a boolean-only table hid.
    """
    NL = chr(10)
    Q3 = chr(34) * 3
    APOS = chr(39)
    fails: list[str] = []

    # (name, source, expected number of literals that survive the prose skip AND look like SQL)
    def sqlish(src: str) -> list[str]:
        return [t for t, _f, _l in sql_literals(src) if "SELECT" in t.upper()]

    cases: list[tuple[str, str, int]] = [
        # ── an apostrophe in a COMMENT must not open a literal ────────────────────────────
        ("apostrophe in a comment",
         "# it doesn" + APOS + "t freeze the loop" + NL
         + "q = " + Q3 + "SELECT spot FROM quant.t WHERE source = " + APOS + "measured" + APOS
         + Q3 + NL, 1),
        # The dangerous half: the shifted span used to SWALLOW the next literal, so the read
        # after an apostrophe was never scanned as its own body.
        ("apostrophe above a read, read still seen whole",
         "# doesn" + APOS + "t matter" + NL + "a = " + Q3 + "SELECT 1 FROM t" + Q3 + NL
         + "b = " + Q3 + "SELECT 2 FROM u" + Q3 + NL, 2),

        # ── an apostrophe in a DOCSTRING, and prose generally, is not a query ─────────────
        ("apostrophe in a docstring",
         "def f():" + NL + "    " + Q3 + "It doesn" + APOS + "t SELECT from anything." + Q3 + NL
         + "    return 1" + NL, 0),
        ("module docstring naming a shared table in a SELECT sentence",
         Q3 + "This module SELECTs rows FROM quant.pattern_instances, which is shared." + Q3 + NL
         + "x = 1" + NL, 0),
        # ENGLISH HAS A WORD "FROM".
        ("prose containing the word from",
         "def f():" + NL + "    " + Q3 + "room_up came from gex_levels_history" + APOS
         + "s call wall." + Q3 + NL, 0),
        ("a real read in a function that also has a docstring",
         "def f():" + NL + "    " + Q3 + "Reads the tape." + Q3 + NL
         + "    q = " + Q3 + "SELECT spot FROM quant.t" + Q3 + NL, 1),

        # ── implicit concatenation is ONE literal ─────────────────────────────────────────
        ("implicit concatenation joined",
         "q = (" + chr(34) + "SELECT spot FROM quant.t " + chr(34) + NL
         + "     " + chr(34) + "WHERE source = " + APOS + "measured" + APOS + chr(34) + ")" + NL,
         1),
        ("two separate statements are two literals",
         "a = " + chr(34) + "SELECT 1 FROM t" + chr(34) + NL
         + "b = " + chr(34) + "SELECT 2 FROM u" + chr(34) + NL, 2),

        # ── f-strings ────────────────────────────────────────────────────────────────────
        ("f-string triple-quoted",
         "q = f" + Q3 + "SELECT spot FROM quant.t WHERE ts > {lo}" + Q3 + NL, 1),
        ("f-string single-quoted",
         "q = f" + chr(34) + "SELECT spot FROM quant.t WHERE ts > {lo}" + chr(34) + NL, 1),
        # A STRING inside the replacement field must not be emitted on its own, or one
        # statement becomes several and a gate counts the pieces.
        ("f-string with a string inside its replacement field",
         "q = f" + chr(34) + "SELECT {" + APOS + ", " + APOS + ".join(c)} FROM quant.t"
         + chr(34) + NL, 1),
        ("f-string concatenated with a plain literal",
         "q = (" + chr(34) + "SELECT spot FROM quant.t " + chr(34) + NL
         + "     f" + chr(34) + "WHERE ts > {lo}" + chr(34) + ")" + NL, 1),
    ]

    for name, src, want in cases:
        try:
            got = sqlish(src)
        except Unlexable as exc:
            fails.append(f"{name}: raised Unlexable ({exc}); expected {want} literal(s)")
            continue
        if len(got) != want:
            fails.append(f"{name}: got {len(got)} SQL literal(s), expected {want} — {got}")

    # ── the one that cannot be a boolean: an UNTERMINATED literal must REFUSE ─────────────
    # tokenize raises AND a quote-pairing regex finds no pair either, so the pre-tokenizer code
    # returned [] and the run exited 0 — a gate that could not look reporting clean.
    unterminated = "q = " + Q3 + "SELECT spot FROM quant.t" + NL
    try:
        literals(unterminated)
        fails.append("an unterminated literal: expected Unlexable, got an answer")
    except Unlexable:
        pass
    # An UNBALANCED BRACKET refuses too — `tokenize` reports "unexpected EOF in multi-line
    # statement" — and the two refusals are NOT interchangeable to a caller with a regex
    # fallback: the bracket case leaves the quotes balanced, so the fallback still finds the
    # read, while the unterminated case defeats the fallback as well. That difference is why
    # a `def broken(:` case can pass without ever exercising the path it was written for,
    # and it is the CALLER's to pin, not this module's.
    # What belongs here is that both refuse rather than answering [].
    bracket = "def broken(:" + NL + "q = " + Q3 + "SELECT spot FROM quant.t" + Q3 + NL
    try:
        literals(bracket)
        fails.append("an unbalanced bracket: expected Unlexable, got an answer")
    except Unlexable:
        pass

    # prose_lines must cover the WHOLE docstring, not just its first line.
    doc = ("def f():" + NL + "    " + Q3 + "line one" + NL + "    line two" + NL
           + "    line three" + Q3 + NL + "    return 1" + NL)
    if prose_lines(doc) != {2, 3, 4}:
        fails.append(f"prose_lines covered {sorted(prose_lines(doc))}, expected lines 2-4")
    if prose_lines("def broken(:" + NL) != set():
        fails.append("prose_lines on unparseable source must be empty, not an exception")

    if fails:
        print(f"{caller}: SELFTEST FAILED")
        for f in fails:
            print("  " + f)
        return 1
    print(f"{caller}: selftest OK ({len(cases)} extraction cases, unterminated literals refuse, "
          f"f-strings are literals on {'3.12+' if _FSTRING_START else 'this lexer'})")
    return 0


if __name__ == "__main__":
    sys.exit(selftest())
