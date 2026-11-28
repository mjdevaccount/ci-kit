"""ci-cog: every cog-generated fact in a tracked doc matches what its code renders NOW.

MOVED FROM trade-data (v1.50.0, `scripts/lint_cog.py`). A count or a roster in prose is a copy,
and copies rot: one audit there found four docs wrong by 30-160% ("~70 vars" for 90, "~35 tools"
for 64). Those facts became cog blocks (cogapp, Ned Batchelder) rendered by the repo's own
generators; this gate is `cog --check` over every TRACKED `--suffix` file (default `*.md`) that
carries a block, run in-process. A PR that moves a fact re-renders the docs (`--write`).

Discovery is `git ls-files`, never a walk: an untracked scratch doc is not one CI sees. A file
whose name only CONTAINS the suffix (`x.md.fixture`) is not cogged. The `--marker` (default an
HTML-comment cog opener) is read from the bytes, so a file that will not decode cannot hide a
block by being skipped.

  ci-cog [--suffix .md ...] [--marker '<!-- [[[cog'] [--include DIR ...] [--rerun TEXT] [--write]
         [--root DIR] [--selftest]

`--include` is cog's `-I` (where a block's `import` finds the generators; default the root);
`--rerun` is the command the STALE message tells the author to run (default `ci-cog --write`).

EXIT -- cog's own codes on the estate's three: 0 = cog 0 (current); 1 = cog 5 (`--check` failed:
a block is stale, the diff is printed); 2 = could not look: no tracked file carries a block, or
cog did not finish (2 usage, 3 output error, 4 a generator RAISED, 1 any other). A generator
that raises is a doc whose sentence no longer fits the code: never a pass, never "findings".
Needs the `cog` extra (cogapp).
"""
from __future__ import annotations

import io
import os
import pathlib
import sys
import tempfile
from collections.abc import Sequence

from ci_kit import discover, site_lint

GATE = "cog"
MARKER = "<!-- [[[cog"
#: cog prints this before a generator's traceback; a must-fire harness reads it as a CRASH of the
#: gate itself. The frames after it are kept -- they name the generator that raised.
_TB_HEADER = "Traceback (most recent call last):"
_EXIT = {0: 0, 5: 1}


def cog_files(root: pathlib.Path, suffixes: Sequence[str], marker: str = MARKER) -> list[str]:
    """Tracked files ending in one of `suffixes` whose bytes hold `marker` (repo-relative)."""
    out = []
    for rel in discover.tracked(["."], suffix="", root=root):
        if not rel.endswith(tuple(suffixes)):
            continue
        try:
            data = (root / rel).read_bytes()
        except OSError as exc:
            print(f"{GATE}: could not read {rel} ({exc})", file=sys.stderr)
            raise SystemExit(2) from exc
        if marker.encode() in data:
            out.append(rel)
    return out


def run_cog(files: Sequence[str], *, write: bool, root: pathlib.Path, includes: Sequence[pathlib.Path],
            rerun: str = "ci-cog --write") -> tuple[int, str]:
    """Run cog in-process on `files` -> (cog's exit code, its combined output, repo-relative)."""
    try:
        from cogapp import Cog
    except ImportError:
        print(f"{GATE}: cogapp is not installed -- install ci-kit[cog]", file=sys.stderr)
        raise SystemExit(2) from None
    buf = io.StringIO()
    cog = Cog()
    cog.set_output(stdout=buf, stderr=buf)
    mode = ["-r"] if write else ["--check", "--diff", f"--check-fail-msg=run `{rerun}`"]
    inc = [x for d in includes for x in ("-I", str(d))]
    rc = cog.main(["cog", *mode, "--verbosity=1", "-n", "utf-8", "-U", *inc, *[str(root / f) for f in files]])
    text = "\n".join(ln for ln in buf.getvalue().splitlines() if ln.strip() != _TB_HEADER)
    for prefix in (f"{root}{os.sep}", f"{root.as_posix()}/"):
        text = text.replace(prefix, "")
    return rc, text


_GOOD = 'Prose. <!-- [[[cog cog.outl("n=3") ]]] -->\nn=3\nmore <!-- [[[end]]] -->\n'
_STALE = _GOOD.replace("\nn=3\n", "\nn=2\n")
_RAISES = 'Prose. <!-- [[[cog raise ValueError("drift") ]]] -->\nx\nmore <!-- [[[end]]] -->\n'


def selftest() -> list[str]:
    """Current 0, stale 1, raising generator 2 (no traceback header), --write regenerates."""
    fails: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp)
        for name, body, want in (("good", _GOOD, 0), ("stale", _STALE, 1), ("raises", _RAISES, 2)):
            (base / f"{name}.md").write_text(body, encoding="utf-8", newline="\n")
            rc, text = run_cog([f"{name}.md"], write=False, root=base, includes=[base])
            if _EXIT.get(rc, 2) != want:
                fails.append(f"{name}: cog {rc} maps to {_EXIT.get(rc, 2)}, want {want}")
            if _TB_HEADER in text:
                fails.append(f"{name}: traceback header not stripped")
        rc, _ = run_cog(["stale.md"], write=True, root=base, includes=[base])
        if rc != 0 or (base / "stale.md").read_text(encoding="utf-8") != _GOOD:
            fails.append("--write did not regenerate the stale block")
    return fails


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # docs carry non-cp1252 text; a Windows console must not crash
        reconf = getattr(stream, "reconfigure", None)
        if reconf:
            reconf(encoding="utf-8", errors="replace")
    ap = site_lint.args_parser("ci-cog", __doc__)
    ap.add_argument("--suffix", action="append", default=[])
    ap.add_argument("--include", action="append", default=[], type=pathlib.Path)
    ap.add_argument("--rerun", default="ci-cog --write")
    ap.add_argument("--marker", default=MARKER)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if a.selftest:
        return site_lint.selftest_exit(GATE, selftest)
    root = site_lint.gate_root(a.root)
    suffixes = a.suffix or [".md"]
    includes = [site_lint.under(root, d) for d in a.include] or [root]
    files: list[str] = []

    def cog_or_blind(write: bool) -> int:
        files[:] = cog_files(root, suffixes, a.marker)
        if not files:
            print(f"{GATE}: BLIND -- no tracked {'/'.join(suffixes)} file carries a cog block; the gate "
                  "looked at nothing", file=sys.stderr)
            raise SystemExit(2)
        rc, text = run_cog(files, write=write, root=root, includes=includes, rerun=a.rerun)
        if text:
            print(text)
        if _EXIT.get(rc, 2) == 2:
            print(f"{GATE}: BLIND -- cog exited {rc} (2 usage / 3 output / 4 a generator raised / 1 other) "
                  f"on {len(files)} file(s); the blocks were not checked", file=sys.stderr)
            raise SystemExit(2)
        return rc

    def write() -> int:
        cog_or_blind(True)
        print(f"{GATE}: regenerated -- {len(files)} file(s): {', '.join(files)}")
        return 0

    def report(stale: Sequence[str]) -> None:
        if stale:
            print(f"{GATE}: STALE -- a generated fact no longer matches its code; run `{a.rerun}` and "
                  "commit the docs", file=sys.stderr)
        else:
            print(f"{GATE}: OK -- {len(files)} file(s): {', '.join(files)}")

    return site_lint.check(["--write"] if a.write else [], gate=GATE, modes={"--write": write},
                           scan=lambda: list(files) if cog_or_blind(False) == 5 else [],
                           selftest=selftest, report=report)


if __name__ == "__main__":
    sys.exit(main())
