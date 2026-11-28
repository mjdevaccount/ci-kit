"""ci-image-pr-filter: every FILE the Dockerfile COPYs by name is a path in the PR build's paths-filter.

MOVED FROM trade-data (v1.50.0, `scripts/lint_image_pr_filter.py`). A pre-merge image build that
runs only when a dorny/paths-filter key matches a HAND-LISTED set of paths (the Dockerfile, the
workflow, the single files it COPYs by name) is skipped on a positive no-match. Comments on both
sides said "keep these in sync" and nothing checked it: add `COPY docs/NEW.md docs/`, forget the
filter, and a later PR renaming that file gets a green "nothing to build" -- the image breaks
only after merge.

WHAT IS COMPARED. Each COPY / ADD source that is a tracked FILE (`git ls-files`, never a walk)
must appear in the filter EXACTLY -- the filter is a list of paths, not globs, and a glob matcher
here would be a second picomatch that accepts more than the real one. A source with a trailing
slash, one that prefixes tracked paths, or one carrying a wildcard is a directory or a pattern
and is skipped (listing those would build the image on every code PR). `--from=` sources live in
another stage. A source that is neither a tracked file nor a directory is a finding too: that
COPY fails at build time.

  ci-image-pr-filter [--dockerfile Dockerfile] [--workflow .github/workflows/ci.yml]
                     [--filter image] [--root DIR] [--selftest]

Exit 0 clean, 1 findings, 2 could not look: no Dockerfile, no COPY/ADD, no workflow, unparseable
YAML, or no step whose `filters:` carries the `--filter` key. Needs the `workflow` extra (PyYAML).
"""
from __future__ import annotations

import posixpath
import sys
from collections.abc import Sequence

from ci_kit import astread, discover, site_lint
from ci_kit.dockerfile import instructions

GATE = "image-pr-filter"
_WILD = ("*", "?", "[")


def copied_files(text: str, tracked: set[str], dockerfile: str = "Dockerfile") -> tuple[set[str], list[str]]:
    """(tracked files the Dockerfile COPYs by name, findings for sources the repo does not hold)."""
    files: set[str] = set()
    problems: list[str] = []
    for tokens in instructions(text):
        for src in tokens[:-1]:
            if src.endswith("/") or any(c in src for c in _WILD):
                continue
            rel = posixpath.normpath(src)
            if rel in tracked:
                files.add(rel)
            elif not any(t.startswith(rel + "/") for t in tracked):
                problems.append(f"{dockerfile} COPYs {src!r}, which is neither a tracked file nor a "
                                "directory -- that COPY fails at build time")
    return files, problems


def filter_paths(text: str, key: str, origin: str) -> list[str]:
    """The `key` filter's paths from the workflow, or exit 2. dorny/paths-filter takes its filters
    as a YAML STRING inside the step's `with:`, so this is two parses."""
    import yaml
    try:
        doc = yaml.safe_load(text) or {}
        for job in (doc.get("jobs") or {}).values():
            for step in (job or {}).get("steps") or []:
                filters = ((step or {}).get("with") or {}).get("filters")
                if isinstance(filters, str):
                    parsed = yaml.safe_load(filters) or {}
                    if isinstance(parsed, dict) and key in parsed:
                        return [posixpath.normpath(str(p)) for p in parsed[key] or []]
    except yaml.YAMLError as exc:
        astread.blind(origin, exc, gate=GATE)
    print(f"{GATE}: could not look -- no step in {origin} has a `filters:` with a {key!r} key, so "
          "there is nothing to compare the Dockerfile against", file=sys.stderr)
    raise SystemExit(2)


def findings(files: set[str], patterns: list[str], dockerfile: str, workflow: str, key: str) -> list[str]:
    return [f"{dockerfile} COPYs {f} but {workflow}'s {key!r} filter does not list it -- a PR renaming "
            "or deleting it gets a green 'nothing to build' and the image breaks after merge"
            for f in sorted(files) if f not in patterns]


_WORKFLOW = """\
jobs:
  changes:
    steps:
      - uses: actions/checkout@v4
      - uses: dorny/paths-filter@v3
        with:
          filters: |
            image:
              - 'Dockerfile'
              - 'pyproject.toml'
              - 'docs/GATE.md'
"""
_DOCKERFILE = """\
# syntax=docker/dockerfile:1.7
FROM python:3.14-slim AS deps
COPY pyproject.toml \\
     # a comment inside the continuation
     ./
COPY --chown=app:app app/ app/
COPY --chown=app:app infra/seeds infra/seeds/
COPY --from=deps /app/.venv /app/.venv
COPY ["docs/GATE.md", "docs/"]
ADD prompts/*.yaml prompts/
"""
_TRACKED = {"Dockerfile", "pyproject.toml", "docs/GATE.md", "docs/NEW.md", "app/main.py",
            "infra/seeds/a.yaml", "prompts/x.yaml"}


def selftest() -> list[str]:
    import contextlib
    import io
    fails: list[str] = []
    patterns = filter_paths(_WORKFLOW, "image", "<selftest>")
    files, problems = copied_files(_DOCKERFILE, _TRACKED)
    if files != {"pyproject.toml", "docs/GATE.md"}:
        fails.append(f"read COPY sources {sorted(files)}: continuation, --chown, directories, --from and "
                     "wildcards must all be handled")
    if problems or findings(files, patterns, "D", "W", "image"):
        fails.append(f"ACCEPTS TOO LITTLE: a Dockerfile whose files are all listed was flagged: {problems}")
    bad, _ = copied_files(_DOCKERFILE + "COPY --chown=app:app docs/NEW.md docs/\n", _TRACKED)
    got = findings(bad, patterns, "D", "W", "image")
    if not (len(got) == 1 and "docs/NEW.md" in got[0]):
        fails.append(f"ACCEPTS TOO MUCH: `COPY docs/NEW.md docs/` with no filter entry was not the one finding: {got}")
    if not any("GONE.md" in p for p in copied_files("COPY docs/GONE.md docs/\n", _TRACKED)[1]):
        fails.append("a COPY of a path the repo does not hold was not flagged")
    for label, text in (("no image key", _WORKFLOW.replace("image:", "other:")), ("unparseable yaml", "jobs: [\n")):
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                filter_paths(text, "image", "<selftest>")
            fails.append(f"a workflow with {label} returned a verdict instead of exit 2")
        except SystemExit as e:
            if e.code != 2:
                fails.append(f"a workflow with {label} exited {e.code}, not 2")
    return fails


def main(argv: Sequence[str] | None = None) -> int:
    ap = site_lint.args_parser("ci-image-pr-filter", __doc__)
    ap.add_argument("--dockerfile", default="Dockerfile")
    ap.add_argument("--workflow", default=".github/workflows/ci.yml")
    ap.add_argument("--filter", default="image", dest="key")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if a.selftest:
        return site_lint.selftest_exit(GATE, selftest)
    root = site_lint.gate_root(a.root)
    seen: list[tuple[set[str], list[str]]] = []

    def scan() -> list[str]:
        tracked = set(discover.tracked(["."], suffix="", root=root))
        discover.require_scanned(len(tracked), gate=GATE, dirs=["."])
        text = astread.read_text(root / a.dockerfile, gate=GATE, origin=a.dockerfile)
        if not instructions(text):
            print(f"{GATE}: could not look -- {a.dockerfile} has no COPY/ADD instruction", file=sys.stderr)
            raise SystemExit(2)
        files, problems = copied_files(text, tracked, a.dockerfile)
        patterns = filter_paths(astread.read_text(root / a.workflow, gate=GATE, origin=a.workflow), a.key, a.workflow)
        seen.append((files, patterns))
        return problems + findings(files, patterns, a.dockerfile, a.workflow, a.key)

    def ok() -> str:
        files, patterns = seen[-1]
        extra = sorted(set(patterns) - files - {a.dockerfile, a.workflow})
        return (f"{GATE}: OK ({len(files)} file(s) COPYd by name, all in {a.workflow}'s {a.key!r} filter"
                + (f"; filter also lists {extra}" if extra else "") + ")")

    return site_lint.check([], gate=GATE, scan=scan, selftest=selftest, ok=ok)


if __name__ == "__main__":
    sys.exit(main())
