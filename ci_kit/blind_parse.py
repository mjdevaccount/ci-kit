"""ci-blind-parse: no parse-failure handler in a gate's code may go quiet. Hard gate, no baseline.

MOVED FROM trade-data (v1.49.0, `scripts/lint_blind_parse.py`; the other half of `ci_kit.astread`).
Measured there at 5a0e2729 (2026-09-18): seventeen gate scripts called `ast.parse`, and THIRTEEN of
their parse handlers answered a parse failure with `continue`, `return []` or `return set()` --
values byte-identical to a CLEAN file. Three more spoke through the findings channel, so "could not
read this" exited 1. A check whose success state is indistinguishable from its blind state.

THE RULE. Every handler catching SyntaxError (or a tuple holding it, or its subclasses
IndentationError / TabError) must END in something that leaves: a `raise`, `sys.exit(...)`, or a
call known never to return (`astread.blind`, plus `--noreturn NAME`). `continue`, `break`, `pass`
and any `return` are the swallow, by name. "Ends in" is STRUCTURAL: an `if` counts only when both
branches leave, and a `raise` above a `return` does not make the handler loud. A bare `except:` is
not this gate's ground (a broader defect, a different rule).

AN AST, NOT A GREP: a grep cannot tell a USE from a MENTION, and this module's own docstring and
selftest are full of the forbidden text. Comments are not in the AST, so the waiver is read from
the handler's own source lines: `# blind-ok: <why>` -- WITH a reason; a bare waiver is a finding.
The legitimate use is a handler permissive in the LOUD direction (an empty mask that makes the
caller scan MORE text).

WHAT IT DOES NOT COVER: an `ast.parse` with no handler raises, which is loud (exit 1, not 2).

  ci-blind-parse --dir scripts[:MIN_FILES] [--dir ...] [--min-handlers 1] [--noreturn NAME ...]
                 [--root DIR] [--selftest]

Exit 0 clean, 1 a swallow, 2 could not look (a dir under its floor, fewer handlers than
`--min-handlers` -- a rule with nothing to judge cannot fire -- or a file that will not parse).
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterable, Sequence

from ci_kit import astread, discover, site_lint

GATE = "blind-parse"
WAIVER = "blind-ok"
PARSE_ERROR_NAMES = frozenset({"SyntaxError", "IndentationError", "TabError"})
NORETURN_CALLS = frozenset({"sys.exit", "exit", "os._exit", "astread.blind", "_astread.blind", "blind"})


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else ""
    return ""


def catches_parse_error(handler: ast.ExceptHandler) -> bool:
    """Does this handler catch a parse failure? A bare `except:` does NOT count."""
    t = handler.type
    if t is None:
        return False
    parts = t.elts if isinstance(t, ast.Tuple) else [t]
    return any(_dotted(p).rsplit(".", 1)[-1] in PARSE_ERROR_NAMES for p in parts)


def leaves(body: list[ast.stmt], noreturn: frozenset[str] = NORETURN_CALLS) -> bool:
    """Does every path through `body` leave loudly? Structural, on the LAST statement."""
    if not body:
        return False
    last = body[-1]
    if isinstance(last, ast.Raise):
        return True
    if isinstance(last, ast.Expr) and isinstance(last.value, ast.Call):
        return _dotted(last.value.func) in noreturn
    if isinstance(last, ast.If):
        return leaves(last.body, noreturn) and leaves(last.orelse, noreturn)
    if isinstance(last, ast.With | ast.AsyncWith):
        return leaves(last.body, noreturn)
    if isinstance(last, ast.Try):
        if last.finalbody and leaves(last.finalbody, noreturn):
            return True
        main = leaves(last.orelse, noreturn) if last.orelse else leaves(last.body, noreturn)
        return main and all(leaves(h.body, noreturn) for h in last.handlers)
    return False


def swallow_kind(handler: ast.ExceptHandler, noreturn: frozenset[str] = NORETURN_CALLS) -> str | None:
    """Name the swallow, or None if the handler leaves."""
    if leaves(handler.body, noreturn):
        return None
    for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        if isinstance(node, ast.Continue):
            return "`continue` -- the file is skipped and counted as scanned"
        if isinstance(node, ast.Break):
            return "`break` -- the rest of the scan is abandoned silently"
        if isinstance(node, ast.Return):
            return "a `return` -- whatever it returns, the caller cannot tell it from a clean file's answer"
    return "no raise and no exit -- control falls out of the handler"


def _waiver_reason(handler: ast.ExceptHandler, lines: list[str]) -> str | None:
    """The handler's own waiver. Lines of a NESTED handler are skipped: its waiver excuses that
    handler, and read here it would silently excuse the outer one too."""
    nested = {ln for n in ast.walk(handler) if isinstance(n, ast.ExceptHandler) and n is not handler
              for ln in range(n.lineno, (n.end_lineno or n.lineno) + 1)}
    for no in range(handler.lineno, (handler.end_lineno or handler.lineno) + 1):
        raw = lines[no - 1] if no - 1 < len(lines) else ""
        if no in nested:
            continue
        if WAIVER in raw:
            return raw.split(WAIVER, 1)[1].lstrip(": ").strip()
    return None


def findings_in(source: str, tree: ast.Module, origin: str,
                noreturn: frozenset[str] = NORETURN_CALLS) -> tuple[list[str], int]:
    """(findings, parse handlers seen) for one parsed file."""
    out: list[str] = []
    handlers = 0
    lines = source.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or not catches_parse_error(node):
            continue
        handlers += 1
        reason = _waiver_reason(node, lines)
        if reason == "":
            out.append(f"{origin}:{node.lineno}: bare `# {WAIVER}` with no reason -- a waiver nobody "
                       f"had to justify is how this class comes back")
        elif reason is None and (kind := swallow_kind(node, noreturn)):
            out.append(f"{origin}:{node.lineno}: a parse failure is swallowed -- {kind}. Call "
                       f"`ci_kit.astread.parse(...)` / `parse_file(...)`, which exits 2 (did not finish "
                       f"looking), or waive it with `# {WAIVER}: <why>`")
    return out, handlers


_NL = chr(10)


def _wrap(body: str, catch: str = "SyntaxError") -> str:
    return (f"import ast{_NL}def f(src):{_NL}    try:{_NL}        return ast.parse(src){_NL}"
            f"    except {catch}:{_NL}{body}")


#: (label, source, flagged?) -- every swallow shape found in the source repo, and every loud one.
_CASES: tuple[tuple[str, str, bool], ...] = (
    ("continue", f"for x in []:{_NL} try:{_NL}  pass{_NL} except SyntaxError:{_NL}  continue{_NL}", True),
    ("return []", _wrap(f"        return []{_NL}"), True),
    ("return two empties", _wrap(f"        return [], []{_NL}"), True),
    ("bare return", _wrap(f"        return{_NL}"), True),
    ("pass", _wrap(f"        pass{_NL}"), True),
    ("report then return", _wrap(f"        problems.append('x'){_NL}        return []{_NL}"), True),
    ("raise above a return", _wrap(f"        if 0:{_NL}            raise{_NL}        return []{_NL}"), True),
    ("tuple (OSError, SyntaxError)", _wrap(f"        return []{_NL}", "(OSError, SyntaxError)"), True),
    ("IndentationError alone", _wrap(f"        return []{_NL}", "IndentationError"), True),
    ("as-bound name", _wrap(f"        return []{_NL}", "SyntaxError as exc"), True),
    ("bare waiver", _wrap(f"        # {WAIVER}{_NL}        return set(){_NL}"), True),
    ("bare raise", _wrap(f"        raise{_NL}"), False),
    ("raise ... from", _wrap(f"        raise RuntimeError('x') from None{_NL}"), False),
    ("sys.exit", _wrap(f"        sys.exit(2){_NL}"), False),
    ("astread.blind", _wrap(f"        astread.blind('f', Exception(), gate='g'){_NL}"), False),
    ("both branches raise", _wrap(f"        if 1:{_NL}            raise ValueError(){_NL}        else:{_NL}"
                                  f"            raise TypeError(){_NL}"), False),
    ("a log line THEN a raise", _wrap(f"        print('x'){_NL}        raise{_NL}"), False),
    ("bare except", f"try:{_NL} pass{_NL}except:{_NL} pass{_NL}", False),
    ("except KeyError", _wrap(f"        return []{_NL}", "KeyError"), False),
    ("waiver with a reason", _wrap(f"        # {WAIVER}: empty here means scan MORE{_NL}        return set(){_NL}"), False),
    ("a MENTION in a comment", f"# except SyntaxError: continue{_NL}", False),
    ("a MENTION in a fixture string", f"BAD = '''try:{_NL} pass{_NL}except SyntaxError:{_NL} continue'''{_NL}", False),
)


def selftest() -> list[str]:
    fails = [f"{label}: expected {'a finding' if want else 'silence'}, got {got or 'silence'}"
             for label, src, want in _CASES
             if bool(got := findings_in(src, ast.parse(src), label)[0]) != want]
    custom = _wrap(f"        my_blind(){_NL}")
    if not findings_in(custom, ast.parse(custom), "c")[0] or \
            findings_in(custom, ast.parse(custom), "c", NORETURN_CALLS | {"my_blind"})[0]:
        fails.append("--noreturn did not make a caller's own exit-2 helper count as leaving")
    return fails


def main(argv: Sequence[str] | None = None) -> int:
    ap = site_lint.gate_parser("ci-blind-parse", __doc__, dir_meta="DIR[:MIN_FILES]")
    ap.add_argument("--min-handlers", type=int, default=1)
    ap.add_argument("--noreturn", action="append", default=[], metavar="NAME",
                    help="a call that never returns (dotted, as written at the call site)")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if a.selftest:
        return site_lint.selftest_exit(GATE, selftest)
    root = site_lint.gate_root(a.root)
    floors = site_lint.dir_floors(a.dir, gate=GATE)
    noreturn = NORETURN_CALLS | frozenset(a.noreturn)
    seen = {"files": 0, "handlers": 0}

    def scan() -> list[str]:
        files: Iterable[str] = discover.tracked(floors, root=root)
        discover.require_scanned_each(discover.count_by_dir(files, floors), floors, gate=GATE)
        out: list[str] = []
        for rel in files:
            src = astread.read_text(root / rel, gate=GATE, origin=rel)
            found, n = findings_in(src, astread.parse(src, rel, gate=GATE), rel, noreturn)
            out += found
            seen["files"] += 1
            seen["handlers"] += n
        discover.require_scanned(seen["handlers"], gate=f"{GATE} (parse handlers)", dirs=floors,
                                 floor=a.min_handlers)
        return sorted(out)

    return site_lint.check(
        [], gate=GATE, scan=scan, selftest=selftest,
        header=lambda _n: (f"{GATE}: FAILED -- a parse failure is answered with a value a CLEAN file also "
                           f"produces, so the gate reports OK while blind to that file."),
        ok=lambda: (f"{GATE}: OK ({seen['handlers']} parse handler(s) across {seen['files']} file(s), "
                    f"every one of them audible)"))


if __name__ == "__main__":
    sys.exit(main())
