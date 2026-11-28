"""ci-function-clones: a file may not ADD a function that is a structural twin of one in another file.

MOVED FROM trade-data (v1.49.0, `scripts/lint_function_clones.py`; DEDUP-CONSOLIDATION D8,
2026-10-07). The duplication tax there was never the text clones (1.85% of lines); it was
RE-IMPLEMENTED LOGIC that then drifted: a Black-Scholes engine with a private mirror, so one fix
had to be written twice; two prereg-stamp twins that had already diverged; three streamers whose
float coercion let NaN through while five siblings refused it. A consolidation nobody re-measures
regrows, so this watches it from outside.

WHAT IT MEASURES. Every function and method (nested ones included) under the ruled dirs, its
docstring dropped, NORMALISED: every Name and argument becomes `_`, every constant its type name,
annotations go. Attribute names are KEPT, so `self.repo.fetch()` and `self.repo.store()` stay
distinct. Two functions with the same normalised AST, at least `--min-nodes` nodes, in DIFFERENT
files, are a clone group (the exact, type-2 lens; a similarity threshold is a number a gate would
have to defend on every false positive).

A RATCHET PER FILE: the baseline counts, per file, the functions that sit in a cross-file group.
Existing twins are grandfathered (a sealed research lab must re-run from the code that produced
it); GROWTH fails. Copying a function into a NEW file raises that file from 0 AND the original's
count, so both are named. This is not `ci-dup-code`: jscpd matches token windows, so a rename
inside a copied function hides it from jscpd and not from this.

  ci-function-clones --dir service:100 [--dir DIR[:FLOOR] ...] [--min-nodes 40]
                     [--baseline ci/function_clones_baseline.txt] [--write-baseline] [--root DIR] [--selftest]

Exit 0 clean, 1 a new twin, 2 could not look (a ruled dir under its floor, a file that will not parse).
"""

from __future__ import annotations

import ast
import copy
import pathlib
import sys
import types
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence

from ci_kit import astread, discover, site_lint
from ci_kit.ratchet import RatchetCount, kept_header

GATE = "function-clones"
HEADER = (
    "# function-clones baseline: per file, the functions that are an exact structural twin\n"
    "# (normalised AST) of a function in ANOTHER file (`ci-function-clones`). SHRINK-ONLY.\n"
    "# A grown or new count fails CI: consolidate instead of copying. Shrink = tighten here.\n"
)
Fn = tuple[str, int, str]           # (name, line, normalised shape)
Member = tuple[str, str, int]       # (file, name, line)


class _Norm(ast.NodeTransformer):
    def visit_Name(self, n: ast.Name) -> ast.AST:
        return ast.copy_location(ast.Name(id="_", ctx=n.ctx), n)

    def visit_arg(self, n: ast.arg) -> ast.AST:
        n.arg, n.annotation = "_", None
        return n

    def visit_Constant(self, n: ast.Constant) -> ast.AST:
        return ast.copy_location(ast.Constant(value=type(n.value).__name__), n)


def _shape(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, int] | None:
    """(normalised dump, node count) of a function body without its docstring; None if empty."""
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    if not body:
        return None
    mod = ast.Module(body=copy.deepcopy(body), type_ignores=[])
    return ast.dump(_Norm().visit(mod), annotate_fields=False), sum(1 for _ in ast.walk(mod))


def functions(tree: ast.Module, min_nodes: int) -> list[Fn]:
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef):
            s = _shape(n)
            if s is not None and s[1] >= min_nodes:
                out.append((n.name, n.lineno, s[0]))
    return out


def groups(by_file: Mapping[str, list[Fn]]) -> list[list[Member]]:
    """Cross-FILE clone groups: members sharing one shape across >= 2 distinct files."""
    idx: dict[str, list[Member]] = defaultdict(list)
    for rel, fns in by_file.items():
        for name, line, shape in fns:
            idx[shape].append((rel, name, line))
    return [sorted(g) for g in idx.values() if len({f for f, _, _ in g}) >= 2]


def counts(gs: list[list[Member]]) -> dict[str, int]:
    return dict(Counter(f for g in gs for f, _, _ in g))


def selftest() -> list[str]:
    import tempfile
    fails: list[str] = []
    body = "\n".join(f"    t{i} = x * {i} + y.attr - z" for i in range(6)) + "\n    return t0\n"
    a = ast.parse(f"def f(x, y, z):\n    '''doc'''\n{body}")
    b = ast.parse("def g(p, q, r):\n" + body.replace("x", "p").replace("y.", "q.").replace("z", "r"))
    c = ast.parse("def h(x, y, z):\n" + body.replace(".attr", ".other"))
    fa, fb, fc = functions(a, 40), functions(b, 40), functions(c, 40)
    if not (fa and fb and fc):
        fails.append("a seven-line arithmetic body fell under 40 nodes")
    elif fa[0][2] != fb[0][2]:
        fails.append("a renamed twin (names, args, docstring differ) did not normalise to one shape")
    elif fa[0][2] == fc[0][2]:
        fails.append("two bodies that call different attributes normalised to the same shape")
    gs = groups({"x/a.py": fa, "x/b.py": fb, "x/c.py": fc, "x/d.py": fa})
    if [[f for f, _, _ in g] for g in gs] != [["x/a.py", "x/b.py", "x/d.py"]]:
        fails.append(f"cross-file grouping wrong: {gs}")
    if groups({"x/a.py": fa + fa}):
        fails.append("two copies inside ONE file were reported as a cross-file group")
    if functions(ast.parse("def k(x):\n    return x + 1\n"), 40):
        fails.append("a one-line function was counted; --min-nodes is not applied")
    with tempfile.TemporaryDirectory() as d:
        r = RatchetCount(pathlib.Path(d) / "b.txt", HEADER)
        r.write({"x/a.py": 1})
        if r.regressions({"x/a.py": 1, "x/new.py": 1}) != [("x/new.py", 1, 0)]:
            fails.append("a new file with one twinned function was not reported as a regression")
    return fails


def main(argv: Sequence[str] | None = None) -> int:
    ap = site_lint.gate_parser("ci-function-clones", __doc__, baseline="ci/function_clones_baseline.txt")
    ap.add_argument("--min-nodes", type=int, default=40)
    a, root, floors = site_lint.parse_gate(ap, argv, gate=GATE, selftest=selftest)
    baseline = site_lint.under(root, a.baseline)
    seen = types.SimpleNamespace(files=0, groups=[])

    def scan() -> dict[str, int]:
        files = discover.tracked_floored(floors, gate=GATE, root=root)
        by_file = {rel: functions(astread.parse_file(root / rel, gate=GATE, origin=rel), a.min_nodes)
                   for rel in files}
        seen.files, seen.groups = len(files), groups(by_file)
        return counts(seen.groups)

    def describe(rel: str, now: int, base: int) -> str:
        twins = ["    " + " == ".join(f"{f}:{line} {name}" for f, name, line in g)
                 for g in seen.groups if any(f == rel for f, _, _ in g)]
        return "\n".join([f"{rel}: {now} twinned function(s), baseline {base}", *twins])

    def ok(cur: dict[str, int], slack: list[tuple[str, int, int]]) -> str:
        tail = f", {len(slack)} file(s) shrank - tighten with --write-baseline in the same PR" if slack else ""
        return (f"{GATE}: OK - {seen.files} file(s) scanned, {len(seen.groups)} cross-file "
                f"group(s) over {len(cur)} file(s), none new{tail}")

    return site_lint.run(
        ["--write"] if a.write else [], gate=GATE, scan=scan, scanned=lambda: seen.files,
        scan_dirs=list(floors), ratchet=RatchetCount(baseline, kept_header(baseline, HEADER)),
        selftest=selftest, floor=1, gained="a structural twin of a function in another file",
        advice=("Import the existing function instead of copying it. A deliberate copy (a sealed "
                "lab) needs --write-baseline and a reason in the PR.",),
        header=lambda n: f"{GATE}: {n} file(s) gained a structural twin of a function in another file",
        describe=describe, ok=ok,
        written=lambda c: f"{GATE}: baseline written, {len(c)} file(s), {sum(c.values())} function(s)")


if __name__ == "__main__":
    sys.exit(main())
