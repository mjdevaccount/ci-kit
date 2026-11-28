"""ci-guards-have-tests: every guard script must be named by a test, OUTSIDE PROSE. Shrink-only.

MOVED FROM trade-data (v1.50.0, `scripts/lint_guards_have_tests.py`). A passing check and a check
that cannot fail are byte-identical from the outside; what separates them is someone having
watched the guard go red, and in a repo with pytest that proof belongs in a test. This gate does
not prove a guard works -- it stops a NEW guard landing with no test at all.

WHY IT PARSES AND DOES NOT GREP. The gate's first life substring-searched the text of `tests/`
and reported every guard covered; parsed, one was not: its only three mentions were docstrings.
A guard whose proof is a sentence ABOUT the guard has no proof. What counts is every name and
non-prose string in each tracked test file (`ci_kit.testrefs.names_in_tree`): a docstring, a
loose prose block, a comment or an f-string statement naming the guard does not; an import, an
attribute, or a string ARGUMENT (`_load("lint_x")`) does. A guard name is matched as a substring
of that corpus, so `scripts.lint_x` and `scripts/lint_x.py` both name `lint_x`.

A PROOF ON A DESELECTED LANE IS REPORTED, NOT COUNTED SILENTLY. A guard named only in tests
carrying a `--deselected-mark` (read from the AST: `pytest.mark.<m>` / `mark.<m>`) has a proof
the merge lane never runs. Not a failure -- the OK line names each one.

DISCOVERY IS `git ls-files`, NEVER A WALK, on both sides. Guards are the tracked files under
`--guards` whose name starts with `--prefix` and ends `--suffix` -- INCLUDING a copy of this gate:
a judge with a self-exemption is the defect it judges, one level up.

  ci-guards-have-tests --dir tests[:FLOOR] [--guards scripts] [--prefix lint_] [--suffix .py]
                       [--deselected-mark integration ...] [--baseline ci/guards_without_tests.txt]
                       [--write-baseline] [--root DIR] [--selftest]

Exit 0 clean, 1 a guard no test names (not in the baseline), 2 could not look: a test dir under
its floor, no guard found, or a test file that will not decode or parse.
"""
from __future__ import annotations

import ast
import pathlib
import sys
import types
from collections.abc import Sequence

from ci_kit import astread, discover, site_lint
from ci_kit.ratchet import RatchetSet, kept_header
from ci_kit.testrefs import names_in_tree

GATE = "guards-have-tests"
HEADER = (
    "# guards-have-tests baseline: guards that no test names OUTSIDE PROSE (`ci-guards-have-tests`).\n"
    "# SHRINK-ONLY: a NEW guard with no test fails CI. A docstring or comment naming it does not count.\n"
    "# Re-freeze: ci-guards-have-tests ... --write-baseline (a GROWN set needs a reason in the PR).\n"
)


def names_in_code(source: str, origin: str) -> set[str]:
    """`testrefs.names_in_tree` of one source text (exit 2 if it will not parse)."""
    return names_in_tree(astread.parse(source, origin, gate=GATE))


def marks_in(tree: ast.Module, marks: Sequence[str]) -> set[str]:
    """The `marks` a test module carries: `pytest.mark.<m>` or `mark.<m>`, decorator or pytestmark."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in marks:
            base = node.value
            if (isinstance(base, ast.Attribute) and base.attr == "mark") or (
                    isinstance(base, ast.Name) and base.id == "mark"):
                found.add(node.attr)
    return found


def judge(guards: Sequence[str], tests: dict[str, str], marks: Sequence[str]) -> tuple[set[str], set[str]]:
    """(guards no test names, guards named ONLY in deselected tests). Pure."""
    everywhere: set[str] = set()
    gated: set[str] = set()
    for rel, src in sorted(tests.items()):
        tree = astread.parse(src, rel, gate=GATE)
        names = names_in_tree(tree)
        everywhere |= names
        if not marks_in(tree, marks):
            gated |= names
    all_blob, gated_blob = chr(10).join(sorted(everywhere)), chr(10).join(sorted(gated))
    missing = {g for g in guards if g not in all_blob}
    return missing, {g for g in guards if g not in gated_blob} - missing


_GOOD = 'from scripts.lint_probe_good import check\n\n\ndef test_it():\n    assert check() == []\n'
_PROSE = ('"""Only TALKS about lint_probe_bad."""\n\n"See also lint_probe_bad."\n\n'
          '# lint_probe_bad in a comment\n\ndef test_x():\n    assert True\n')
_FSTR = 'x = 1\nf"""prose about lint_probe_fstr {x}"""\n'
_WRAPPED = '"prose about lint_probe_tuple",\n["lint_probe_list"]\n{"k": "lint_probe_dict"}\n'
_SLOW = 'import pytest\n\npytestmark = pytest.mark.slow\n\ndef test_s():\n    _load("lint_probe_slow")\n'


def selftest() -> list[str]:
    fails: list[str] = []
    named = lambda src: names_in_code(src, "<selftest>")  # noqa: E731
    if not any("lint_probe_good" in t for t in named(_GOOD)):
        fails.append("an import of the guard did not register its name")
    for label, src in (("prose", _PROSE), ("f-string statement", _FSTR), ("wrapped prose", _WRAPPED)):
        hit = sorted(t for t in named(src) if "lint_probe_" in t)
        if hit:
            fails.append(f"ACCEPTS TOO MUCH: {label} scored as a test: {hit}")
    if not any("lint_probe_call" in t for t in named('_load("lint_probe_call")\n')):
        fails.append("a bare call statement with the guard's name as an argument was thrown away")
    if not any("lint_probe_arg" in t for t in named("def f(lint_probe_arg):\n    pass\n")):
        fails.append("a name bound as a parameter was dropped")
    missing, desel = judge(["lint_probe_good", "lint_probe_bad", "lint_probe_slow"],
                           {"t/a.py": _GOOD, "t/b.py": _PROSE, "t/c.py": _SLOW}, ["slow"])
    if missing != {"lint_probe_bad"} or desel != {"lint_probe_slow"}:
        fails.append(f"judge: missing {sorted(missing)}, deselected-only {sorted(desel)}")
    return fails


def main(argv: Sequence[str] | None = None) -> int:
    ap = site_lint.gate_parser("ci-guards-have-tests", __doc__, baseline="ci/guards_without_tests.txt")
    ap.add_argument("--guards", default="scripts", metavar="DIR")
    ap.add_argument("--prefix", default="lint_")
    ap.add_argument("--suffix", default=".py")
    ap.add_argument("--deselected-mark", action="append", default=[], metavar="MARK")
    a, root, floors = site_lint.parse_gate(ap, argv, gate=GATE, selftest=selftest)
    baseline = site_lint.under(root, a.baseline)
    seen = types.SimpleNamespace(tests=0, guards=[], desel=set())

    def guards() -> list[str]:
        out = sorted(pathlib.PurePosixPath(rel).name[: -len(a.suffix)]
                     for rel in discover.tracked([a.guards], suffix=a.suffix, root=root)
                     if pathlib.PurePosixPath(rel).name.startswith(a.prefix))
        discover.require_scanned(len(out), gate=f"{GATE}[guards]", dirs=[a.guards])
        return out

    def scan() -> set[str]:
        files = discover.tracked_floored(floors, gate=GATE, root=root)
        tests = {rel: astread.read_text(root / rel, gate=GATE, origin=rel) for rel in files}
        names = guards()
        missing, desel = judge(names, tests, a.deselected_mark)
        seen.tests, seen.guards, seen.desel = len(files), names, desel
        return missing

    def ok(missing: set[str], gone: list[str]) -> str:
        names, desel = seen.guards, seen.desel
        lines = [f"{len(gone)} baselined guard(s) now tested -- tighten with --write-baseline: "
                 + ", ".join(gone)] if gone else []
        lines.append(f"{GATE}: OK ({len(names) - len(missing)}/{len(names)} guards named by a test "
                     f"outside prose, {len(missing)} baselined; {seen.tests} tracked test file(s) read)")
        if desel:
            lines.append(f"  {len(desel)} named ONLY in tests the merge lane deselects "
                         f"({'/'.join(a.deselected_mark)}): " + ", ".join(sorted(desel)))
        return chr(10).join(lines)

    return site_lint.run(
        ["--write"] if a.write else [], gate=GATE, scan=scan, scanned=lambda: seen.tests,
        scan_dirs=list(floors), ratchet=RatchetSet(baseline, kept_header(baseline, HEADER)),
        selftest=selftest, gained="guards", floor=1, ok=ok,
        header=lambda n: f"{GATE}: {n} NEW guard(s) that no test names outside prose:",
        describe=lambda g: f"{a.guards}/{g}{a.suffix}",
        advice=("", "A guard nobody has watched fail is indistinguishable from one that cannot.",
                "A docstring naming the guard is not a test: write one that plants what it forbids."),
        written=lambda m: f"{GATE}: baseline written ({len(m)} untested guard(s))")


if __name__ == "__main__":
    sys.exit(main())
