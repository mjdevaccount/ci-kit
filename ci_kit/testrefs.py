"""Which names and source modules a TEST file references -- outside prose. Library (v1.50.0).

MOVED FROM trade-data (`scripts/_testrefs.py` + the reader half of `lint_guards_have_tests.py`),
shared by `ci-guards-have-tests` and `ci-untested-modules`: one definition of "a test references
X", so the two gates cannot disagree about it.

PROSE NEVER COUNTS. A string in STATEMENT position -- a docstring, or an unattached commentary
block between definitions -- evaluates and is discarded, so it is evidence of nothing. That holds
through DATA wrappers too (a trailing comma makes a prose line a tuple statement; a list, a dict,
an IfExp, an f-string's literal segments). The walk stops at anything that can DO something: a
bare `_load("lint_x")` statement is a call, a real exercise, and still counts. Comments never
enter the tree. A statement string inside a BinOp / BoolOp / Compare still counts (`+` can call
`__add__`; over-exclusion is the more expensive direction).

MODULE REFERENCES (`dotted_candidates` + `resolve`), for the packages a caller names:
  * `import p.a.b`                     -> p.a.b
  * `from p.a import b`                -> p.a AND p.a.b (b may be a module)
  * a non-prose string `"p.a.b.f"` or `"p/a/b.py"` -> p.a.b by the LONGEST known-module prefix
    (`monkeypatch.setattr("p.a.b.f")`, `importlib.import_module`, `spec_from_file_location`).
An f-string names nothing a reader can resolve; relative imports are skipped.
"""
from __future__ import annotations

import ast
from collections.abc import Iterable

__all__ = ["dotted_candidates", "names_in_tree", "prose_nodes", "resolve"]

#: Nodes a statement-position string can hide inside while remaining pure data.
_DATA_NODES = (ast.Constant, ast.JoinedStr, ast.FormattedValue, ast.Tuple, ast.List, ast.Set,
               ast.Dict, ast.IfExp, ast.Starred)


def _prose_strings(node: ast.AST, out: set[int]) -> None:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            out.add(id(node))
        return
    if not isinstance(node, _DATA_NODES):
        return
    for child in ast.iter_child_nodes(node):
        _prose_strings(child, out)


def prose_nodes(tree: ast.Module) -> set[int]:
    """ids of every str Constant in statement position, through data wrappers only."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr):
            _prose_strings(node.value, out)
    return out


def names_in_tree(tree: ast.Module) -> set[str]:
    """Every identifier and non-prose string a reader of the tree sees: names, attributes, import
    aliases and modules, def/class names, parameters, keywords, handler names, globals."""
    prose = prose_nodes(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.update(n for n in (node.name, node.asname) if n)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
        elif isinstance(node, ast.arg):
            found.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            found.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            found.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            found.update(node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in prose:
            found.add(node.value)
    return found


def _as_dotted(s: str, packages: tuple[str, ...]) -> str | None:
    """A string constant as a dotted module candidate, or None when it cannot name one."""
    s = s.strip().replace(chr(92), "/")
    if s.endswith(".py"):
        s = s[:-3]
    if "/" in s:
        if any(ch in s for ch in " :*?<>|\n"):
            return None
        s = s.strip("/").replace("/", ".")
    if not s or s.split(".", 1)[0] not in packages or "." not in s:
        return None
    return s if all(part.isidentifier() for part in s.split(".")) else None


def dotted_candidates(tree: ast.Module, packages: Iterable[str]) -> set[str]:
    """Every dotted path under `packages` that the tree's imports or non-prose strings name.
    Unresolved: the caller owns the module set (see `resolve`)."""
    pk = tuple(packages)
    prose = prose_nodes(tree)
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            out.add(node.module)
            out.update(f"{node.module}.{a.name}" for a in node.names if a.name != "*")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in prose:
            d = _as_dotted(node.value, pk)
            if d:
                out.add(d)
    return {c for c in out if c.split(".", 1)[0] in pk}


def resolve(candidates: Iterable[str], modules: set[str]) -> set[str]:
    """The `modules` (dotted) some candidate names, by longest prefix: `p.a.b.f` -> `p.a.b`."""
    hit: set[str] = set()
    for c in candidates:
        parts = c.split(".")
        for n in range(len(parts), 1, -1):
            prefix = ".".join(parts[:n])
            if prefix in modules:
                hit.add(prefix)
                break
    return hit
