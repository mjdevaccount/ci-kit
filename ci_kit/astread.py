"""Read and parse a source file for a gate -- or exit 2 saying it could not look.

WHY THIS EXISTS. Measured in the service repo this came from (2026-09-18): seventeen gate scripts
called `ast.parse`, and THIRTEEN of their parse handlers answered a parse failure with `continue`,
`return []` or `return set()` -- values byte-identical to "I read this file and it was clean".
Three more spoke through the FINDINGS channel, so "I could not read this" exited 1 alongside
"here is a violation". That is the defect a gate kit exists to prevent:

    a check whose SUCCESS STATE is indistinguishable from its BLIND STATE

THE TRIGGER WAS A BYTE. Two modules began with a UTF-8 BOM. Python's importer strips it, so both
imported and ran; `path.read_text(encoding="utf-8")` does NOT, so `ast.parse` refused line 1 with
`invalid non-printable character U+FEFF` and the gates that read `utf-8` skipped both files in
silence.

TWO THINGS, ONE PLACE.

  * `ENCODING` is `utf-8-sig`, so a BOM is tolerated rather than mistaken for a syntax error. A
    gate that called a healthy file broken would be the opposite failure and just as wrong.
  * `blind()` is the only way out of a parse failure: it prints WHAT could not be read and WHY
    that is not a clean result, then exits **2** -- the code `discover.require_scanned` uses for
    "this gate did not run", distinct from 1 (findings).

A caller that wants the rule enforced can lint its own scripts for `except SyntaxError` handlers
that reach an ordinary `continue`/`return`; this module is what such handlers should call.
"""

from __future__ import annotations

import ast
import pathlib
import sys
from typing import NoReturn

#: BOM-TOLERANT ON PURPOSE. `utf-8-sig` decodes a BOM'd file AND a plain one; `utf-8` turns the
#: first into a SyntaxError that looks exactly like broken code. Same value as `site_lint.ENCODING`.
ENCODING = "utf-8-sig"


def blind(origin: str, exc: BaseException, *, gate: str) -> NoReturn:
    """Exit 2: this gate could not read `origin`, so it has verified nothing about it.

    NEVER returns. Callers are written as `except (SyntaxError, ...) as exc: blind(...)` with no
    fallback line after it.
    """
    print(f"{gate}: DID NOT FINISH LOOKING — {origin}: "
          f"{type(exc).__name__}: {exc}", file=sys.stderr)
    print(f"{gate}: exit 2. A file this gate could not parse is not a file it found clean, so "
          f"this is neither 0 (clean) nor 1 (findings). Fix the file, or fix the reader — "
          f"a BOM'd source is read correctly with encoding={ENCODING!r}.", file=sys.stderr)
    raise SystemExit(2)


def parse(source: str, origin: str, *, gate: str) -> ast.Module:
    """`ast.parse(source)`, or exit 2 naming `origin`.

    `origin` is whatever identifies the text to a human -- a repo-relative path for a file, or
    something like `flows/foo.yaml::rawscript` for source embedded in data. It is the half a bare
    `SyntaxError` never carries, and the reason this takes it rather than deriving one.
    """
    # SPELLED OUT, not hidden behind a module constant, so an AST-reading handler audit can see
    # it. IndentationError and TabError are SyntaxError subclasses; ValueError is what `ast.parse`
    # raises on a NUL byte, which would otherwise escape as a traceback -- exit 1, the findings code.
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        blind(origin, exc, gate=gate)


def read_text(path: pathlib.Path | str, *, gate: str, origin: str | None = None) -> str:
    """The file's text, decoded `utf-8-sig`, or exit 2.

    An unreadable file is the same verdict as an unparseable one: the gate did not look. That
    includes the decode failure -- `errors="replace"` silently turns a mojibake file into one that
    parses into the WRONG tree, which is worse than refusing it.
    """
    p = pathlib.Path(path)
    where = origin or rel(p)
    try:
        return p.read_text(encoding=ENCODING)
    except (OSError, UnicodeDecodeError) as exc:
        blind(where, exc, gate=gate)


def parse_file(path: pathlib.Path | str, *, gate: str, origin: str | None = None) -> ast.Module:
    """`read_text` then `parse` -- the whole of what most gates need from this module."""
    p = pathlib.Path(path)
    where = origin or rel(p)
    return parse(read_text(p, gate=gate, origin=where), where, gate=gate)


def rel(p: pathlib.Path, root: pathlib.Path | None = None) -> str:
    """`p` relative to `root` (default: the current directory) as POSIX, else `p` as given.

    Never raises: a gate is not allowed to die while composing the message that says it could not
    look. Gates run from their checkout's root, so the default names a file the way CI does; pass
    `origin=` to `read_text`/`parse_file` when the gate knows better.
    """
    try:
        return p.resolve().relative_to((root or pathlib.Path.cwd()).resolve()).as_posix()
    except (ValueError, OSError):
        return p.as_posix()
