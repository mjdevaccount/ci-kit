#!/usr/bin/env python3
"""ci-gate-lint: a workflow whose jobs can SKIP must funnel into one always-running aggregate.

WHY. GitHub counts a SKIPPED job as passing, for a required status check and for auto-merge.
trade-infra#806 (2026-11-06) auto-merged with four required jobs skipped: its `detect` job
(dorny/paths-filter) died on runner DNS, every `needs: detect` job skipped, and the four checks
branch protection required went green without running. The guard existed; it could not fire.

The fix is one job that ALWAYS runs and is the only required check -- `if: always()`, `needs:`
every other job, and [re-actors/alls-green](https://github.com/re-actors/alls-green) over
`${{ toJSON(needs) }}`, which is red on any failed/cancelled need and on any skip outside
`allowed-skips`. This gate checks that shape so it cannot rot:

  no-aggregate   a job has `needs:` (it skips when its need fails) but no alls-green job exists
  many           more than one alls-green job (which one does branch protection require?)
  not-always     the aggregate's `if:` is not `always()` -- it skips with its needs
  jobs-input     its `jobs:` input is not `${{ toJSON(needs) }}` -- it judges something else
  uncovered      a job missing from the aggregate's `needs:` -- a check nothing requires
  skip-upstream  an allowed skip names a job others `need` -- its skip hides theirs (detect)
  skip-no-if     an allowed skip names a job with no `if:` -- it can only skip by a need failing
  skip-unknown   an allowed skip names no job in the workflow

Branch protection naming the aggregate is outside a checkout; an estate audit reads that.

Exit 0 clean, 1 findings, 2 could not look (a missing or unparseable workflow, no `jobs:`).
Needs PyYAML: install `ci-kit[workflow]` (the action does).
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
from typing import Any

from ci_kit import site_lint

GATE = "ci-gate-lint"
ALLS_GREEN = "re-actors/alls-green@"
_EXPR = re.compile(r"^\$\{\{\s*(.*?)\s*\}\}$", re.S)


def _bare(expr: Any) -> str:
    """`${{ x }}` -> `x`, whitespace-normalised; a non-string `if:` (true) as its str."""
    s = str(expr).strip()
    m = _EXPR.match(s)
    return re.sub(r"\s+", " ", m.group(1) if m else s)


def _needs(job: dict) -> list[str]:
    n = job.get("needs") or []
    return [n] if isinstance(n, str) else [str(x) for x in n]


def _alls_green_step(job: dict) -> dict | None:
    for step in job.get("steps") or []:
        if isinstance(step, dict) and str(step.get("uses", "")).startswith(ALLS_GREEN):
            return step
    return None


def findings_in(jobs: dict[str, dict], origin: str) -> list[str]:
    """Every rule above, over one workflow's `jobs:` mapping. Pure."""
    aggs = [j for j, b in jobs.items() if _alls_green_step(b)]
    out: list[str] = []
    if len(aggs) > 1:
        return [f"{origin}: many -- {len(aggs)} alls-green jobs ({', '.join(aggs)}); keep one"]
    if not aggs:
        skippers = [j for j, b in jobs.items() if _needs(b)]
        if skippers:
            out.append(f"{origin}: no-aggregate -- {', '.join(skippers)} skip(s) when a need fails, and a "
                       f"skip reads green; add one `if: always()` alls-green job that needs every job")
        return out
    agg = aggs[0]
    body = jobs[agg]
    step = _alls_green_step(body) or {}
    with_ = step.get("with") or {}
    if _bare(body.get("if", "")) != "always()":
        out.append(f"{origin}: not-always -- `{agg}` has if: {body.get('if')!r}; it must be `always()`")
    if _bare(with_.get("jobs", "")) != "toJSON(needs)":
        out.append(f"{origin}: jobs-input -- `{agg}` passes jobs: {with_.get('jobs')!r}, not ${{{{ toJSON(needs) }}}}")
    covered = set(_needs(body))
    for j in sorted(set(jobs) - {agg} - covered):
        out.append(f"{origin}: uncovered -- `{j}` is not in `{agg}`.needs: a check nothing requires")
    upstream = {n for j, b in jobs.items() if j != agg for n in _needs(b)}
    skips = [s.strip() for s in str(with_.get("allowed-skips", "")).replace("\n", ",").split(",") if s.strip()]
    for s in skips:
        if s not in jobs:
            out.append(f"{origin}: skip-unknown -- allowed skip `{s}` is no job in this workflow")
        elif s in upstream:
            out.append(f"{origin}: skip-upstream -- `{s}` is needed by other jobs; allowing its skip hides theirs")
        elif "if" not in jobs[s]:
            out.append(f"{origin}: skip-no-if -- `{s}` has no `if:`; it can only skip because a need failed")
    return out


def load_jobs(path: pathlib.Path, rel: str) -> dict[str, dict]:
    """The workflow's `jobs:` mapping, or exit 2 naming the file."""
    try:
        import yaml
    except ImportError:
        print(f"{GATE}: DID NOT FINISH LOOKING -- PyYAML missing; install ci-kit[workflow]", file=sys.stderr)
        raise SystemExit(2) from None
    text = site_lint.read_source(path, rel, gate=GATE)
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        print(f"{GATE}: DID NOT FINISH LOOKING -- {rel}: {e}", file=sys.stderr)
        raise SystemExit(2) from None
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, dict) or not jobs:
        print(f"{GATE}: DID NOT FINISH LOOKING -- {rel}: no `jobs:` mapping", file=sys.stderr)
        raise SystemExit(2)
    return {str(k): (v if isinstance(v, dict) else {}) for k, v in jobs.items()}


_GOOD = """
jobs:
  detect: {runs-on: x, steps: [{run: 'true'}]}
  test: {needs: detect, if: "needs.detect.outputs.code == 'true'", runs-on: x, steps: [{run: 'true'}]}
  lint: {uses: o/r/.github/workflows/lint.yml@v1}
  gate:
    if: ${{ always() }}
    needs: [detect, test, lint]
    runs-on: x
    steps:
      - uses: re-actors/alls-green@b5b5b37504aa4183270bd3d855c52a67f212be35
        with: {allowed-skips: test, jobs: '${{ toJSON(needs) }}'}
"""

#: (label, old, new) -- each mutation of _GOOD must produce exactly the named finding
_MUTATIONS = [
    ("no-aggregate", "  gate:\n    if: ${{ always() }}\n    needs: [detect, test, lint]\n    runs-on: x\n    steps:\n"
                     "      - uses: re-actors/alls-green@b5b5b37504aa4183270bd3d855c52a67f212be35\n"
                     "        with: {allowed-skips: test, jobs: '${{ toJSON(needs) }}'}\n", ""),
    ("not-always", "if: ${{ always() }}", "if: success()"),
    ("jobs-input", "jobs: '${{ toJSON(needs) }}'", "jobs: '{}'"),
    ("uncovered", "needs: [detect, test, lint]", "needs: [detect, test]"),
    ("skip-upstream", "allowed-skips: test,", "allowed-skips: 'test, detect',"),
    ("skip-no-if", "allowed-skips: test,", "allowed-skips: 'test, lint',"),
    ("skip-unknown", "allowed-skips: test,", "allowed-skips: 'test, tset',"),
]


def selftest() -> list[str]:
    import yaml

    fails: list[str] = []
    good = findings_in(yaml.safe_load(_GOOD)["jobs"], "good.yml")
    fails += [f"FALSE POSITIVE on the good workflow: {f}" for f in good]
    for label, old, new in _MUTATIONS:
        if old not in _GOOD:
            fails.append(f"mutation {label}: anchor missing from the fixture")
            continue
        got = findings_in(yaml.safe_load(_GOOD.replace(old, new))["jobs"], "bad.yml")
        if not got or not all(f.split(": ", 1)[1].startswith(f"{label} --") for f in got):
            fails.append(f"mutation {label}: want exactly that finding, got {got}")
    return fails


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog=GATE, description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=".", help="checkout root (default .)")
    ap.add_argument("--workflow", action="append", default=[],
                    help="workflow file, relative to --root; repeatable (default .github/workflows/ci.yml)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    root = pathlib.Path(args.root)
    files = args.workflow or [".github/workflows/ci.yml"]

    def scan() -> list[str]:
        out: list[str] = []
        for rel in files:
            p = root / rel
            if not p.is_file():
                print(f"{GATE}: DID NOT FINISH LOOKING -- {rel} does not exist", file=sys.stderr)
                raise SystemExit(2)
            out += findings_in(load_jobs(p, rel), rel)
        return out

    return site_lint.check(
        ["--selftest"] if args.selftest else [], gate=GATE, scan=scan, selftest=selftest,
        header=lambda n: f"{GATE}: {n} finding(s) -- a job here can skip GREEN:",
        advice=["GitHub counts a skipped job as a passing required check. One `if: always()` job running",
                "re-actors/alls-green over ${{ toJSON(needs) }}, needing every job, is the only required check."],
        ok=lambda: f"{GATE}: OK -- {', '.join(files)}")


if __name__ == "__main__":
    raise SystemExit(main())
