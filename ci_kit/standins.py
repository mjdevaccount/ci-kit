"""ci-test-standins: no new SIGNATURE-BLIND stand-in for a project callable in the tests. Shrink-only.

MOVED FROM trade-data (v1.50.0, `scripts/lint_test_standins.py`). There a nightly went red
because a test replaced a source function with `lambda symbols, period: {...}`; a later PR passed
the real function a new keyword, production was fine, and the lambda raised TypeError -- in the
nightly only, because PR CI never ran that directory. A bare lambda or a hand-written def carries
ITS OWN signature, not the function's it stands in for: a second, unchecked copy that drifts. The
fix is `mock.create_autospec(real, side_effect=fake)` -- the fake takes the REAL signature.

WHAT IS A SIGNATURE-BLIND STAND-IN (AST, never grep): a replacement that is a `lambda`, or the
NAME of a def / async def written in the same file, installed over an attribute of something the
file imported from a `--package` by
  * `monkeypatch.setattr(obj, "attr", fake)` / `monkeypatch.setattr("pkg.x.attr", fake)`
  * `mock.patch("pkg.x.attr", fake)` / `patch(..., new=fake)`
  * `mock.patch.object(obj, "attr", fake)` (`new=` too)
where `obj` is reached through an import of the package (an alias, an attribute chain off one) or
is a dotted string. Constants, local classes, `create_autospec`, `autospec=True` and
`return_value=` are not stand-ins. ruff's PT008 is the neighbour, not the cover: it never reads
`monkeypatch.setattr`, and its advice (`return_value=`) is a MagicMock that accepts ANY call.

It does NOT decide a fake is wrong. Everything already in the tree is a per-file count baseline;
a fake that must not carry the signature takes `# standin-ok: <why>` on any line of the call
(reason REQUIRED, >= 8 chars; a bare tag is reported, never honoured).

  ci-test-standins --dir tests[:FLOOR] --package NAME [--package ...] [--exempt PATH ...]
                   [--baseline ci/test_standins_baseline.txt] [--write-baseline] [--root DIR] [--selftest]

Exit 0 clean, 1 findings, 2 could not look (no --package, or no scanned file imports or names one;
a dir under its floor; a file that will not decode or parse).
"""
from __future__ import annotations

import ast
import sys
from collections.abc import Sequence

from ci_kit import astread, discover, site_lint
from ci_kit.ratchet import RatchetCount, kept_header

GATE = "test-standins"
WAIVER = "standin-ok"
HEADER = (
    "# test-standins baseline: per-file count of signature-blind stand-ins (a lambda or a local def\n"
    "# installed over a project callable by monkeypatch.setattr / mock.patch[.object]; `ci-test-standins`).\n"
    "# SHRINK-ONLY: a NEW file or a GROWN count fails CI. Re-freeze: --write-baseline.\n"
)


def _dotted(n: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(n, ast.Attribute):
        parts.append(n.attr)
        n = n.value
    if not isinstance(n, ast.Name):
        return ""
    parts.append(n.id)
    return ".".join(reversed(parts))


class Reader:
    """The predicate for one package set: `findings_in(source, origin) -> [line]`, bare waivers kept."""

    def __init__(self, packages: Sequence[str]) -> None:
        self.packages = tuple(packages)
        self.bare: list[str] = []
        #: files that import a package or name one in a dotted string: 0 means --package reached nothing
        self.reached = 0

    def _ours(self, dotted: str) -> bool:
        return dotted.split(".", 1)[0] in self.packages

    def _imports(self, tree: ast.Module) -> set[str]:
        """Local names bound by an import of one of the packages, anywhere in the file."""
        out: set[str] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                out.update(a.asname or a.name.split(".", 1)[0] for a in n.names if self._ours(a.name))
            elif isinstance(n, ast.ImportFrom) and n.module and not n.level and self._ours(n.module):
                out.update(a.asname or a.name for a in n.names)
        return out

    def _target(self, obj: ast.expr, imports: set[str]) -> bool:
        if isinstance(obj, ast.Constant) and isinstance(obj.value, str):
            return self._ours(obj.value)
        head = _dotted(obj).split(".", 1)[0]
        return bool(head) and head in imports

    def standin(self, call: ast.Call, imports: set[str], local_defs: set[str]) -> bool:
        def blind(fake: ast.expr | None) -> bool:
            return isinstance(fake, ast.Lambda) or (isinstance(fake, ast.Name) and fake.id in local_defs)
        fn = _dotted(call.func)
        tail = fn.rsplit(".", 1)[-1]
        args, kw = call.args, {k.arg: k.value for k in call.keywords if k.arg}
        if tail == "setattr" and fn != "setattr":       # monkeypatch.setattr, not the builtin
            if len(args) >= 3:
                return self._target(args[0], imports) and blind(args[2])
            return len(args) == 2 and self._target(args[0], imports) and blind(args[1])
        if fn.endswith("patch.object") and len(args) >= 2:
            return self._target(args[0], imports) and blind(args[2] if len(args) >= 3 else kw.get("new"))
        if tail == "patch" and args:
            return self._target(args[0], imports) and blind(args[1] if len(args) >= 2 else kw.get("new"))
        return False

    def findings_in(self, source: str, origin: str) -> list[int]:
        tree = astread.parse(source, origin, gate=GATE)
        imports = self._imports(tree)
        if imports or any(isinstance(n, ast.Constant) and isinstance(n.value, str) and "." in n.value
                          and self._ours(n.value) for n in ast.walk(tree)):
            self.reached += 1
        local_defs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}
        waived, bare = site_lint.waived_lines(source, WAIVER)
        self.bare.extend(f"{origin}:{ln} bare `# {WAIVER}:` with no reason -- say WHY the fake may not "
                         f"carry the real signature" for ln, _ in bare)
        hit: set[int] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and self.standin(n, imports, local_defs) and not any(
                    i in waived for i in range(n.lineno, (n.end_lineno or n.lineno) + 1)):
                hit.add(n.lineno)
        return sorted(hit)


_BAD = '''\
from unittest import mock
from unittest.mock import patch

import pkg.loader as src
from pkg import clock
from pkg.repos.store import Store


def _fake_run(symbols, period):
    return {"rows": 1}


async def _fake_rows(self, sym):
    return []


def test_a(monkeypatch):
    monkeypatch.setattr(src, "run", lambda symbols, period: {"rows": 1})  # BAD
    monkeypatch.setattr(src, "run", _fake_run)  # BAD
    monkeypatch.setattr(Store, "rows", _fake_rows)  # BAD
    monkeypatch.setattr("pkg.clock.today", lambda d: d)  # BAD
    monkeypatch.setattr(clock.dt, "date", lambda: None)  # BAD
    with mock.patch("pkg.loader.run", lambda *a, **k: None):  # BAD
        pass
    with patch.object(src, "run", new=_fake_run):  # BAD
        pass
    monkeypatch.setattr(  # BAD
        src, "run",
        lambda symbols, period: {"rows": 1})
    monkeypatch.setattr(src, "run", _fake_run)  # standin-ok:  # BAD
'''
_GOOD = '''\
"""A docstring may say monkeypatch.setattr(src, "run", lambda: 1) freely."""
import json
from unittest import mock

import pkg.loader as src
from pkg.repos.store import Store


def _fake_run(symbols, period):
    return {"rows": 1}


def test_b(monkeypatch):
    # A comment may say monkeypatch.setattr(src, "run", lambda: 1): comments never enter the AST.
    monkeypatch.setattr(src, "run", mock.create_autospec(src.run, return_value={"rows": 1}))
    monkeypatch.setattr(src, "run", mock.create_autospec(src.run, side_effect=_fake_run))
    with mock.patch.object(Store, "rows", autospec=True, return_value=[]):
        pass
    monkeypatch.setattr(src, "BATCH", 5)
    monkeypatch.setattr(json, "dumps", lambda o: "{}")
    setattr(src, "run", _fake_run)
    monkeypatch.setattr(src, "run", lambda *a, **k: None)  # standin-ok: the call itself is the subject
    monkeypatch.setattr(
        src, "run",
        _fake_run)  # standin-ok: a multi-line call's waiver may sit on any line
'''


def selftest() -> list[str]:
    r = Reader(["pkg"])
    want = [i for i, ln in enumerate(_BAD.splitlines(), 1) if ln.rstrip().endswith("# BAD")]
    fails = [f"bad: flagged {got}, want {want}"] if (got := r.findings_in(_BAD, "bad.py")) != want else []
    if got := r.findings_in(_GOOD, "good.py"):
        fails.append(f"FALSE POSITIVE at good.py:{got}")
    if len(r.bare) != 1:
        fails.append(f"a bare `# {WAIVER}:` must be reported exactly once, got {r.bare}")
    return fails


def main(argv: Sequence[str] | None = None) -> int:
    ap = site_lint.gate_parser("ci-test-standins", __doc__, baseline="ci/test_standins_baseline.txt")
    ap.add_argument("--package", action="append", default=[], metavar="NAME")
    ap.add_argument("--exempt", action="append", default=[], metavar="PATH")
    a, root, floors = site_lint.parse_gate(ap, argv, gate=GATE, selftest=selftest)
    if not a.package:
        print(f"{GATE}: no --package -- a gate with no project package to protect cannot fire", file=sys.stderr)
        return 2
    baseline = site_lint.under(root, a.baseline)
    reader = Reader(a.package)
    scanned = [0]

    def scan() -> dict[str, int]:
        reader.bare.clear()
        reader.reached = 0
        by_dir = discover.count_by_dir(discover.tracked(floors, root=root), floors)
        discover.require_scanned_each(by_dir, floors, gate=GATE)
        counts, scanned[0] = site_lint.count_sites(root, list(floors), [site_lint.norm_dir(e) for e in a.exempt],
                                                   reader.findings_in, gate=GATE)
        if not reader.reached:
            print(f"{GATE}: could not look -- none of {scanned[0]} test file(s) imports or names a --package "
                  f"({', '.join(a.package)}): a misspelt package is a gate that cannot fire", file=sys.stderr)
            raise SystemExit(2)
        return counts

    return site_lint.run(
        ["--write"] if a.write else [], gate=GATE, scan=scan, scanned=lambda: scanned[0],
        scan_dirs=list(floors), ratchet=RatchetCount(baseline, kept_header(baseline, HEADER)),
        selftest=selftest, gained="a signature-blind stand-in", floor=min(floors.values()),
        hard=lambda _cur: list(reader.bare), advice=(
            "Use mock.create_autospec(real_fn, return_value=... | side_effect=fake): the fake then takes",
            "the REAL signature, so a call the real function would refuse is refused in the test too.",
            f"A fake that must not carry it? `# {WAIVER}: <why>` on the call."))


if __name__ == "__main__":
    sys.exit(main())
