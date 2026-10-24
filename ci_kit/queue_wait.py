#!/usr/bin/env python3
"""ci-queue-wait: how long one workflow's runs waited for a runner, over the last N hours.

WHY. The autoupdater treadmill (trade-data, 2026-10-24): every push to main re-queued CI on every
armed PR, and main's own CI and its deploy image waited behind them -- p90 235s, max 404s for
main, max 3340s for a PR -- while the service deploy chased main for two hours. Nothing measured
it, so nothing could say when it started or whether a fix held. This is the number, as a gate a
nightly job can run: exit 1 when the p90 is over the limit.

Queue wait of a run = its earliest non-skipped job's `started_at` minus the run's `created_at`
(the time it sat waiting for a runner, before any step ran).

Exit 0 = measured and p90 within `--max-p90`; 1 = over it; 2 = could not measure (an API error,
or fewer than `--min-runs` runs to measure -- a gate that measured nothing has verified nothing).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys


def _ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _api(path: str) -> dict:
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=120, check=False)
    if r.returncode:
        raise RuntimeError(f"gh api {path}: {(r.stderr or r.stdout).strip()[:300]}")
    return json.loads(r.stdout)


def wait_s(run: dict, jobs: list[dict]) -> float | None:
    """Seconds this run waited for its first runner; None if no job ever started. Pure."""
    starts = [_ts(j["started_at"]) for j in jobs if j.get("started_at") and j.get("conclusion") != "skipped"]
    return (min(starts) - _ts(run["created_at"])).total_seconds() if starts else None


def p90(xs: list[float]) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(0.9 * len(s)))]


def measure(repo: str, workflow: str, branch: str, event: str, hours: float) -> list[tuple[int, float]]:
    since = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = f"per_page=100&created=>={since}" + (f"&branch={branch}" if branch else "") + (f"&event={event}" if event else "")
    runs = _api(f"repos/{repo}/actions/workflows/{workflow}/runs?{q}")["workflow_runs"]
    out = []
    for r in runs:
        if r.get("status") != "completed":
            continue
        w = wait_s(r, _api(f"repos/{repo}/actions/runs/{r['id']}/jobs?per_page=100")["jobs"])
        if w is not None and w >= 0:
            out.append((int(r["id"]), w))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ci-queue-wait", description=__doc__.split("\n", 1)[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--workflow", required=True, help="workflow file name, e.g. ci.yml")
    ap.add_argument("--branch", default="main", help="'' = every branch")
    ap.add_argument("--event", default="push", help="'' = every event")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--max-p90", type=float, required=True, help="seconds; a p90 over this exits 1")
    ap.add_argument("--min-runs", type=int, default=1, help="fewer measured runs exits 2")
    a = ap.parse_args(argv)
    scope = f"{a.repo} {a.workflow} branch={a.branch or '*'} event={a.event or '*'} last {a.hours:g}h"
    try:
        waits = measure(a.repo, a.workflow, a.branch, a.event, a.hours)
    except (RuntimeError, KeyError, ValueError, subprocess.SubprocessError) as e:
        print(f"ci-queue-wait: DID NOT FINISH: {scope}: {e}")
        return 2
    if len(waits) < a.min_runs:
        print(f"ci-queue-wait: DID NOT FINISH: {scope}: {len(waits)} run(s) measured, need {a.min_runs}")
        return 2
    vals = [w for _, w in waits]
    worst = max(waits, key=lambda t: t[1])
    line = (f"{scope}: {len(vals)} run(s), queue wait p50 {sorted(vals)[len(vals) // 2]:.0f}s "
            f"p90 {p90(vals):.0f}s max {worst[1]:.0f}s (run {worst[0]}); limit p90 {a.max_p90:.0f}s")
    bad = p90(vals) > a.max_p90
    print(("ci-queue-wait: OVER LIMIT: " if bad else "ci-queue-wait: ok: ") + line)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(f"### CI queue wait\n\n{'OVER LIMIT' if bad else 'ok'}: {line}\n")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
