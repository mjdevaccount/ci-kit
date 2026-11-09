#!/usr/bin/env python3
"""ci-queue-wait: how long one workflow's runs waited for a runner, over the last N hours.

WHY. The autoupdater treadmill (trade-data, 2026-10-24): every push to main re-queued CI on every
armed PR, and main's own CI and its deploy image waited behind them -- p90 235s, max 404s for
main, max 3340s for a PR -- while the service deploy chased main for two hours. Nothing measured
it, so nothing could say when it started or whether a fix held. This is the number, as a gate a
nightly job can run: exit 1 when the p90 (or p95) is over the limit.

PR SCOPE (2026-11-09). The 11-08 jam was PR runs on the home runner pool -- 48 jobs queued, 4.4 PR
runs per merge -- and main-push-only measuring could not see it. `--event pull_request --branch ''`
measures PR runs; that is hundreds of runs a day, so the run list is PAGED (it read one page of 100
before) up to `--max-runs`, newest first, and the line says when it stopped there. Runs that never
started (cancelled while queued) have no wait and are counted separately, never as zero.

Queue wait of a run = its earliest non-skipped job's `started_at` minus the run's `created_at`
(the time it sat waiting for a runner, before any step ran).

Exit 0 = measured and every given limit (`--max-p90`, `--max-p95`) held; 1 = over one; 2 = could not measure (an API error,
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


def _ran(j: dict) -> bool:
    """Did a runner pick this job up? A job cancelled while QUEUED gets `started_at` = its
    creation time and no runner_name (measured 2026-11-09, trade-data run of 02:29:58Z): read as a
    start, it scored a 25-minute wait as 0s. Skipped jobs never had a runner either."""
    return bool(j.get("started_at")) and bool(j.get("runner_name")) and j.get("conclusion") != "skipped"


def wait_s(run: dict, jobs: list[dict]) -> float | None:
    """Seconds this run waited for its first runner; None if no job ever ran. Pure."""
    starts = [_ts(j["started_at"]) for j in jobs if _ran(j)]
    return (min(starts) - _ts(run["created_at"])).total_seconds() if starts else None


def job_waits(jobs: list[dict]) -> list[float]:
    """Each job's own wait (its started_at - its created_at), for jobs a runner picked up. Pure.
    The run-level wait is the FIRST job's: on 11-08 a run whose lint job started in 16s waited
    60 min for its unit-tests job on the heavy pool, and the run-level number said 16s."""
    return [(_ts(j["started_at"]) - _ts(j["created_at"])).total_seconds() for j in jobs
            if _ran(j) and j.get("created_at")]


def pct(xs: list[float], q: float) -> float:
    """Nearest-rank-below quantile, the estimator the p90 gate has always used. Pure."""
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


def p90(xs: list[float]) -> float:
    return pct(xs, 0.9)


def list_runs(repo: str, workflow: str, branch: str, event: str, hours: float, max_runs: int) -> list[dict]:
    """Completed runs in the window, newest first, paged until the window or `max_runs` ends."""
    since = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = f"per_page=100&created=>={since}" + (f"&branch={branch}" if branch else "") + (f"&event={event}" if event else "")
    out: list[dict] = []
    page = 1
    while len(out) < max_runs:
        runs = _api(f"repos/{repo}/actions/workflows/{workflow}/runs?{q}&status=completed&page={page}")["workflow_runs"]
        out += [r for r in runs if r.get("status") == "completed"]
        if len(runs) < 100:
            break
        page += 1
    return out[:max_runs]


def measure(repo: str, workflow: str, branch: str, event: str, hours: float,
            max_runs: int = 300, unit: str = "run") -> tuple[list[tuple[int, float]], int]:
    """-> ([(run id, wait s)], runs no runner ever picked up). unit 'job' = one entry per job."""
    out, never = [], 0
    for r in list_runs(repo, workflow, branch, event, hours, max_runs):
        jobs = _api(f"repos/{repo}/actions/runs/{r['id']}/jobs?per_page=100")["jobs"]
        w = wait_s(r, jobs)
        if w is None:
            never += 1
        elif unit == "job":
            out += [(int(r["id"]), x) for x in job_waits(jobs) if x >= 0]
        elif w >= 0:
            out.append((int(r["id"]), w))
    return out, never


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ci-queue-wait", description=__doc__.split("\n", 1)[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--workflow", required=True, help="workflow file name, e.g. ci.yml")
    ap.add_argument("--branch", default="main", help="'' = every branch")
    ap.add_argument("--event", default="push", help="'' = every event")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--max-p90", type=float, help="seconds; a p90 over this exits 1")
    ap.add_argument("--max-p95", type=float, help="seconds; a p95 over this exits 1")
    ap.add_argument("--min-runs", type=int, default=1, help="fewer measured runs exits 2")
    ap.add_argument("--unit", choices=("run", "job"), default="run",
                    help="run = each run's first-job wait; job = every job's own wait (a pool behind another)")
    ap.add_argument("--max-runs", type=int, default=300,
                    help="newest completed runs read (each costs one jobs call; GITHUB_TOKEN allows 1000/h)")
    a = ap.parse_args(argv)
    if a.max_p90 is None and a.max_p95 is None:
        ap.error("give --max-p90 and/or --max-p95")
    scope = f"{a.repo} {a.workflow} branch={a.branch or '*'} event={a.event or '*'} last {a.hours:g}h per {a.unit}"
    try:
        waits, never = measure(a.repo, a.workflow, a.branch, a.event, a.hours, a.max_runs, a.unit)
    except (RuntimeError, KeyError, ValueError, subprocess.SubprocessError) as e:
        print(f"ci-queue-wait: DID NOT FINISH: {scope}: {e}")
        return 2
    if len(waits) < a.min_runs:
        print(f"ci-queue-wait: DID NOT FINISH: {scope}: {len(waits)} run(s) measured, need {a.min_runs}")
        return 2
    vals = [w for _, w in waits]
    worst = max(waits, key=lambda t: t[1])
    limits = {k: v for k, v in (("p90", a.max_p90), ("p95", a.max_p95)) if v is not None}
    got = {"p90": p90(vals), "p95": pct(vals, 0.95)}
    capped = f" (stopped at --max-runs {a.max_runs})" if len({i for i, _ in waits}) + never >= a.max_runs else ""
    line = (f"{scope}: {len(vals)} {a.unit}(s){capped}, {never} run(s) never picked up; queue wait p50 "
            f"{sorted(vals)[len(vals) // 2]:.0f}s p90 {got['p90']:.0f}s p95 {got['p95']:.0f}s "
            f"max {worst[1]:.0f}s (run {worst[0]}); limit " + " ".join(f"{k} {v:.0f}s" for k, v in limits.items()))
    bad = any(got[k] > v for k, v in limits.items())
    print(("ci-queue-wait: OVER LIMIT: " if bad else "ci-queue-wait: ok: ") + line)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(f"### CI queue wait\n\n{'OVER LIMIT' if bad else 'ok'}: {line}\n")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
