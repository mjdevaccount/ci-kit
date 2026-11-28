"""ci-untested-modules: the set of source modules NO test references may not GROW. Shrink-only.

MOVED FROM trade-data (v1.50.0, `scripts/lint_untested_modules.py`). There three methods counted
"untested" three ways (a basename grep 117, a dotted-path scan 166, a review row 92) and none
refused the next untested module, so a 1,119-line read stack arrived one unreferenced file at a
time. This gate is ONE definition. A module is TESTED when a tracked file under `--tests` reaches
it by either edge:
  1. a reference (`ci_kit.testrefs`): an import, a from-import, or a NON-PROSE string naming it
     (`monkeypatch.setattr("p.a.b.f")`, `"p/a/b.py"`). Docstrings and f-strings never count.
  2. a MIRROR test file: `<tests>/<path under the package>/test_<name>.py`
     (`pkg/usecases/decision.py` <- `tests/usecases/test_decision.py`).
The packages are the top directory of each `--dir`. `__init__.py`, `_pb2` / `_pb2_grpc`
generated files and any path with an `--exclude-part` directory are not subjects.

A RATCHET, NOT A BAN (`RatchetSet`): a baselined module stays grandfathered until a test reaches
it (then its line is slack); an unlisted module with no edge fails. The intentional case is a
comment `# untested-ok: <why>` in its first 10 lines (a re-export, a CLI shim): reason REQUIRED,
COUNTED on the OK line, never baselined.

  ci-untested-modules --dir pkg:200 [--dir ...] --tests tests[:FLOOR] [--exclude-part migrations]
                      [--baseline ci/untested_modules_baseline.txt] [--write-baseline] [--report]
                      [--root DIR] [--selftest]

Exit 0 clean, 1 a new untested module, 2 could not look (a dir or the test corpus under its floor
-- an empty corpus makes EVERY module untested, an empty module set makes the tree clean -- or a
file that will not decode or parse). `--report` prints the 15 largest untested modules.
"""
from __future__ import annotations

import pathlib
import re
import sys
import types
from collections.abc import Sequence

from ci_kit import astread, discover, site_lint
from ci_kit.ratchet import RatchetSet, kept_header
from ci_kit.testrefs import dotted_candidates, resolve

GATE = "untested-modules"
HEADER = (
    "# untested-modules baseline: source modules NO tracked test reaches by reference or a mirror\n"
    "# test file (`ci-untested-modules`). SHRINK-ONLY: an unlisted module with no test edge fails CI.\n"
    "# Intentional: `# untested-ok: <why>` in the first 10 lines. Re-freeze: --write-baseline.\n"
)
EXCLUDED_SUFFIXES = ("/__init__.py", "_pb2.py", "_pb2_grpc.py")
WAIVER = re.compile(r"#\s*untested-ok:\s*(\S.*)$")
WAIVER_LINES = 10
REPORT_TOP = 15


def in_scope(rel: str, exclude_parts: Sequence[str] = ()) -> bool:
    return (not ("/" + rel).endswith(EXCLUDED_SUFFIXES)
            and not set(exclude_parts).intersection(rel.split("/")[:-1]))


def mirror_test(rel: str, tests_dir: str) -> str:
    """`pkg/usecases/decision.py` -> `<tests_dir>/usecases/test_decision.py`."""
    p = pathlib.PurePosixPath(rel.partition("/")[2])
    return str(pathlib.PurePosixPath(tests_dir) / p.parent / f"test_{p.name}")


def waiver(source: str) -> str | None:
    for line in source.splitlines()[:WAIVER_LINES]:
        m = WAIVER.search(line)
        if m:
            return m.group(1).strip()
    return None


def classify(sources: dict[str, str], tests: dict[str, str], tests_dir: str) -> tuple[dict[str, int], dict[str, str]]:
    """({rel: lines} untested and unwaived, {rel: reason} waived). Pure: the selftest feeds it."""
    modules = {rel[:-3].replace("/", "."): rel for rel in sources}
    packages = sorted({rel.split("/", 1)[0] for rel in sources})
    cands: set[str] = set()
    for rel, src in sorted(tests.items()):
        cands |= dotted_candidates(astread.parse(src, rel, gate=GATE), packages)
    hit = resolve(cands, set(modules))
    untested: dict[str, int] = {}
    waived: dict[str, str] = {}
    for d, rel in sorted(modules.items()):
        if d in hit or mirror_test(rel, tests_dir) in tests:
            continue
        why = waiver(sources[rel])
        if why is not None:
            waived[rel] = why
        else:
            untested[rel] = len(sources[rel].splitlines())
    return untested, waived


_SRC = {
    "pkg/a/imported.py": "x = 1\n", "pkg/a/from_imported.py": "x = 1\n", "pkg/a/submod.py": "x = 1\n",
    "pkg/a/patched.py": "x = 1\n", "pkg/a/by_path.py": "x = 1\n", "pkg/usecases/mirrored.py": "x = 1\n",
    "pkg/a/only_in_docstring.py": "x = 1\n", "pkg/a/only_in_fstring.py": "x = 1\n",
    "pkg/a/waived.py": "# untested-ok: pure re-export shim\nx = 1\n", "pkg/a/bare_waiver.py": "# untested-ok:\nx = 1\n",
    "pkg/a/late_waiver.py": "\n" * 10 + "# untested-ok: too late\nx = 1\n",
    "pkg/a/nothing.py": "x = 1\ny = 2\nz = 3\n", "lib/daemon.py": "x = 1\n",
}
_TESTS = {
    "tests/test_edges.py": (
        '"""Prose naming pkg.a.only_in_docstring must not count."""\n'
        "import pkg.a.imported\nfrom pkg.a.from_imported import x\nfrom pkg.a import submod\n"
        'monkeypatch.setattr("pkg.a.patched.x", 2)\n'
        'spec_from_file_location("m", ROOT / "pkg/a/by_path.py")\n'
        'name = f"pkg.a.{thing}"\n"pkg.a.only_in_fstring"\nimport lib.daemon\n'),
    "tests/usecases/test_mirrored.py": "x = 1\n",
}
_EXPECT = {"pkg/a/only_in_docstring.py", "pkg/a/only_in_fstring.py", "pkg/a/bare_waiver.py",
           "pkg/a/late_waiver.py", "pkg/a/nothing.py"}


def selftest() -> list[str]:
    fails: list[str] = []
    got, waived = classify(_SRC, _TESTS, "tests")
    if set(got) != _EXPECT:
        fails.append(f"classified {sorted(got)}; expected {sorted(_EXPECT)}")
    if got.get("pkg/a/nothing.py") != 3:
        fails.append("an untested module's size was not its line count")
    if waived != {"pkg/a/waived.py": "pure re-export shim"}:
        fails.append(f"waivers were {waived}; expected exactly the reasoned first-10-lines one")
    if not in_scope("pkg/a/b.py") or in_scope("pkg/a/__init__.py") or in_scope("pkg/x_pb2.py") \
            or in_scope("pkg/migrations/m.py", ["migrations"]) or not in_scope("__init__x.py"):
        fails.append("scope: __init__ / _pb2 / an --exclude-part dir must be out, a module in")
    if mirror_test("pkg/usecases/decision.py", "tests") != "tests/usecases/test_decision.py":
        fails.append(f"mirror path was {mirror_test('pkg/usecases/decision.py', 'tests')}")
    return fails


def main(argv: Sequence[str] | None = None) -> int:
    ap = site_lint.gate_parser("ci-untested-modules", __doc__, baseline="ci/untested_modules_baseline.txt")
    ap.add_argument("--tests", default=None, metavar="DIR[:FLOOR]")
    ap.add_argument("--exclude-part", action="append", default=[], metavar="DIRNAME")
    ap.add_argument("--report", action="store_true")
    a, root, floors = site_lint.parse_gate(ap, argv, gate=GATE, selftest=selftest)
    if not a.tests:
        print(f"{GATE}: no --tests DIR[:FLOOR] -- with no test corpus every module reads untested", file=sys.stderr)
        return 2
    tfloors = site_lint.dir_floors([a.tests], gate=f"{GATE}[tests]")
    tests_dir = next(iter(tfloors))
    baseline = site_lint.under(root, a.baseline)
    seen = types.SimpleNamespace(files=0, tests=0, waived={}, last={})

    def scan() -> dict[str, int]:
        files = discover.tracked_floored(floors, gate=GATE, root=root, keep=lambda rel: in_scope(rel, a.exclude_part))
        tfiles = discover.tracked_floored(tfloors, gate=f"{GATE}[tests]", root=root)
        read = lambda rel: astread.read_text(root / rel, gate=GATE, origin=rel)  # noqa: E731
        untested, seen.waived = classify({r: read(r) for r in files}, {r: read(r) for r in tfiles}, tests_dir)
        seen.files, seen.tests, seen.last = len(files), len(tfiles), untested
        return untested

    if a.report:
        cur = scan()
        top = sorted(cur.items(), key=lambda kv: (-kv[1], kv[0]))[:REPORT_TOP]
        print(f"{GATE}: {len(cur)} untested module(s), {sum(cur.values())} line(s); the {len(top)} largest:")
        for rel, n in top:
            print(f"  {n:5d}  {rel}")
        print(f"  ({len(seen.waived)} module(s) waived with `# untested-ok:`)")
        return 0

    def describe(rel: str) -> str:
        return (f"NEW: {rel} ({seen.last[rel]} lines) -- no test imports it, names it in a non-prose string, "
                f"or lives at {mirror_test(rel, tests_dir)}. Write one, or waive it with "
                f"`# untested-ok: <why>` in its first {WAIVER_LINES} lines.")

    def ok(cur: dict[str, int], slack: list[str]) -> str:
        tail = f"; {len(slack)} baseline line(s) now tested or gone -- tighten with --write-baseline" if slack else ""
        return (f"{GATE}: OK -- {seen.files} module(s) and {seen.tests} test file(s) scanned, "
                f"{len(cur)} untested (all baselined), {len(seen.waived)} waived with untested-ok{tail}")

    return site_lint.run(
        ["--write"] if a.write else [], gate=GATE, scan=scan, scanned=lambda: seen.files,
        scan_dirs=list(floors), ratchet=RatchetSet(baseline, kept_header(baseline, HEADER)),
        selftest=selftest, gained="modules", advice=(), floor=1, describe=describe, ok=ok,
        header=lambda n: (f"{GATE}: {n} module(s) no test reaches and the baseline does not list "
                          f"({seen.files} modules, {seen.tests} test files scanned)"),
        written=lambda u: f"{GATE}: baseline written -- {len(u)} untested module(s) ({len(seen.waived)} waived)")


if __name__ == "__main__":
    sys.exit(main())
