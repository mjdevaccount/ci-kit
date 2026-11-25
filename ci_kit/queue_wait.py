#!/usr/bin/env python3
"""ci-queue-wait: how long one workflow's runs waited for a runner, over the last N hours.

WHY. The autoupdater treadmill (a consumer, 2026-10-24): every push to main re-queued CI on every
armed PR, and main's own CI and its deploy image waited behind them -- p90 235s, max 404s for
main, max 3340s for a PR -- while the service deploy chased main for two hours. Nothing measured
it, so nothing could say when it started or whether a fix held. This is the number, as a gate a
nightly job can run: exit 1 when the p90 (or p95) is over the limit.

PR SCOPE (2026-11-09). The 11-08 jam was PR runs on the home runner pool -- 48 jobs queued, 4.4 PR
runs per merge -- and main-push-only measuring could not see it. `--event pull_request --branch=`
(empty = every branch; through the action a quoted `''` stays literal) measures PR runs; that is
hundreds of runs a day, so the run list is PAGED up to `--max-runs`, newest first. Past the cap the
number is a sample of the NEWEST runs and the line says so.

CENSORED WAITS (2026-11-09, reviewer 580 F1). A job cancelled while still QUEUED (a superseded PR
run) never got a runner: GitHub stamps its started_at = its created_at and leaves runner_name
empty. Read as a start it scored a 25-minute wait as 0s; dropped, it hid exactly the long waits a
jam makes, and a jam that cancels most runs still exited 0. So it is scored as what it is: a wait of
AT LEAST its completed_at - created_at (a lower bound, sound for an upper-percentile gate; a
supersede-cancel 20s after creation costs 20s), counted in the percentiles and named in the line.

Queue wait of a run (`--unit run`) = its earliest picked-up job's started_at minus the run ATTEMPT's
start (`run_started_at`: a re-run hours later is not hours of queue), or the censored wait when no
job was ever picked up. `--unit job` = every job's own started_at - created_at (a pool behind another:
the run's lint can start in 16s while its unit job waits an hour).

IDLE WINDOWS (2026-11-25). A day with no main push, or no PR job, held no wait to measure, and the
old exit 2 ("could not measure") went red on every quiet Sunday (a consumer's 11-22 and 11-25 runs):
a false red that reads the same as a dead API. Too few waits in `--hours` now WIDENS the window,
doubling up to `--widen-to-hours` (default 168), and gates the widened sample, named in the line.
Still too few at the cap is IDLE -- a distinct verdict, exit 0 -- but only once the filter is shown
to match at least one completed run EVER: a filter that never matched (a branch or event typo) is
misconfigured, not idle, and stays 2, so a typo cannot read green forever.

Exit 0 = measured and every given limit (`--max-p90`, `--max-p95`) held, or IDLE; 1 = over one;
2 = could not measure (an API error, or a filter that matches no completed run at all -- a gate that
measured nothing has verified nothing, and a crashed detector never exits 0).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys

Wait = tuple[int, float, bool]  # (run id, seconds, censored)


def _ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _api(path: str) -> dict:
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=120, check=False)
    if r.returncode:
        raise RuntimeError(f"gh api {path}: {(r.stderr or r.stdout).strip()[:300]}")
    return json.loads(r.stdout)


def _ran(j: dict) -> bool:
    """Did a runner pick this job up? (A job cancelled while QUEUED has started_at = created_at and
    no runner_name; skipped jobs never had a runner either.)"""
    return bool(j.get("started_at")) and bool(j.get("runner_name")) and j.get("conclusion") != "skipped"


def _queued_out(j: dict) -> bool:
    """A job that waited and ended without a runner: cancelled/timed out while queued."""
    return (not _ran(j) and j.get("conclusion") != "skipped"
            and bool(j.get("created_at")) and bool(j.get("completed_at")))


def _anchor(run: dict) -> dt.datetime:
    return _ts(run.get("run_started_at") or run["created_at"])


def wait_s(run: dict, jobs: list[dict]) -> tuple[float, bool] | None:
    """-> (seconds this run attempt waited for its first runner, censored); None if it has no job
    that ran or waited (no jobs, or all skipped). Pure."""
    # A partial re-run carries the earlier attempt's finished jobs over with their OLD timestamps
    # (reviewer 581 N1: a consumer's run 42469246881 attempt 2 scored -1407s and was dropped). Only a job
    # this attempt ran -- started at or after the attempt's start -- is a pick-up.
    t0 = _anchor(run)
    starts = [t for t in (_ts(j["started_at"]) for j in jobs if _ran(j)) if t >= t0]
    if starts:
        return (min(starts) - t0).total_seconds(), False
    ends = [t for t in (_ts(j["completed_at"]) for j in jobs if _queued_out(j)) if t >= t0]
    return ((max(ends) - t0).total_seconds(), True) if ends else None


def job_waits(jobs: list[dict]) -> list[tuple[float, bool]]:
    """Each job's own wait (started_at - created_at), censored (completed_at - created_at) for a
    job that ended still queued; skipped jobs are not waits. Pure."""
    out = []
    for j in jobs:
        if _ran(j) and j.get("created_at"):
            out.append(((_ts(j["started_at"]) - _ts(j["created_at"])).total_seconds(), False))
        elif _queued_out(j):
            out.append(((_ts(j["completed_at"]) - _ts(j["created_at"])).total_seconds(), True))
    return out


def pct(xs: list[float], q: float) -> float:
    """Nearest-rank-below quantile, the estimator the p90 gate has always used. Pure."""
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


def p90(xs: list[float]) -> float:
    return pct(xs, 0.9)


def _filters(branch: str, event: str) -> str:
    return (f"&branch={branch}" if branch else "") + (f"&event={event}" if event else "")


def list_runs(repo: str, workflow: str, branch: str, event: str, hours: float,
              max_runs: int) -> tuple[list[dict], bool]:
    """-> (completed runs in the window, newest first, at most `max_runs`; True when the window
    held more). Reads until a short page or one run past the cap, so `truncated` is a fact."""
    since = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = f"per_page=100&created=>={since}" + _filters(branch, event)
    out: list[dict] = []
    seen: set[int] = set()
    page = 1
    total = 0
    while len(out) <= max_runs:
        body = _api(f"repos/{repo}/actions/workflows/{workflow}/runs?{q}&status=completed&page={page}")
        runs = body["workflow_runs"]
        total = max(total, int(body.get("total_count") or 0))
        # Offset paging over a list that grows at its head: a run completing between two reads
        # shifts the next page down one, and the page boundary's run comes back (reviewer 582 F3).
        for r in runs:
            if r.get("status") == "completed" and r["id"] not in seen:
                seen.add(r["id"])
                out.append(r)
        if len(runs) < 100:
            break
        page += 1
    # The filtered runs API stops serving at 1000 results (reviewer 581 N2): a short page there is
    # the API's cap, not the window's end, and total_count says how many the window really held.
    return out[:max_runs], len(out) > max_runs or total > len(out)


def ever_completed(repo: str, workflow: str, branch: str, event: str) -> int:
    """-> how many completed runs this filter has EVER matched (no date bound). 0 = the filter is
    wrong (a branch/event typo, a workflow that never ran), which is never 'idle'."""
    body = _api(f"repos/{repo}/actions/workflows/{workflow}/runs?per_page=1&status=completed"
                + _filters(branch, event))
    return max(int(body.get("total_count") or 0), len(body["workflow_runs"]))


def measure(repo: str, workflow: str, branch: str, event: str, hours: float,
            max_runs: int = 300, unit: str = "run",
            cache: dict[int, list[dict]] | None = None) -> tuple[list[Wait], int, bool, int]:
    """-> (waits, runs with nothing to measure, truncated, runs read). unit 'job' = one entry per job.
    `cache` (run id -> jobs) lets a widened re-measure skip the runs an earlier window already read."""
    out: list[Wait] = []
    empty = 0
    cache = {} if cache is None else cache
    runs, truncated = list_runs(repo, workflow, branch, event, hours, max_runs)
    for r in runs:
        if r["id"] not in cache:
            cache[r["id"]] = _api(f"repos/{repo}/actions/runs/{r['id']}/jobs?per_page=100")["jobs"]
        jobs = cache[r["id"]]
        got = ([(int(r["id"]), s, c) for s, c in job_waits(jobs)] if unit == "job"
               else [(int(r["id"]), *w) for w in [wait_s(r, jobs)] if w is not None])
        got = [g for g in got if g[1] >= 0]
        empty += not got
        out += got
    return out, empty, truncated, len(runs)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ci-queue-wait", description=__doc__.split("\n", 1)[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--workflow", required=True, help="workflow file name, e.g. ci.yml")
    ap.add_argument("--branch", default="main", help="empty (--branch=) = every branch")
    ap.add_argument("--event", default="push", help="empty (--event=) = every event")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--widen-to-hours", type=float, default=168,
                    help="too few waits in --hours doubles the window up to this; still too few = IDLE "
                         "(exit 0) when the filter has ever matched a run. Set equal to --hours to never widen")
    ap.add_argument("--max-p90", type=float, help="seconds; a p90 over this exits 1")
    ap.add_argument("--max-p95", type=float, help="seconds; a p95 over this exits 1")
    ap.add_argument("--min-runs", type=int, default=1, help="fewer measured waits (runs or jobs, per --unit) after widening = IDLE")
    ap.add_argument("--unit", choices=("run", "job"), default="run",
                    help="run = each run's first-job wait; job = every job's own wait (a pool behind another)")
    ap.add_argument("--max-runs", type=int, default=300,
                    help="newest completed runs read (each costs one jobs call; GITHUB_TOKEN allows 1000/h)")
    a = ap.parse_args(argv)
    if a.max_p90 is None and a.max_p95 is None:
        ap.error("give --max-p90 and/or --max-p95")
    need = max(1, a.min_runs)
    hours, cap = a.hours, max(a.hours, a.widen_to_hours)
    cache: dict[int, list[dict]] = {}
    try:
        while True:
            waits, empty, truncated, read = measure(a.repo, a.workflow, a.branch, a.event, hours,
                                                    a.max_runs, a.unit, cache)
            if len(waits) >= need or hours >= cap or truncated:
                break
            hours = min(cap, hours * 2)
        ever = ever_completed(a.repo, a.workflow, a.branch, a.event) if len(waits) < need else -1
    except (RuntimeError, LookupError, ValueError, TypeError, OSError, subprocess.SubprocessError) as e:
        # OSError: no gh on PATH; TypeError/IndexError: a response of an unexpected shape. Any is "did not
        # look", never an over-the-limit 1 (reviewer 582 F5).
        print(f"ci-queue-wait: DID NOT FINISH: {a.repo} {a.workflow}: {e}")
        return 2
    widened = f" (widened from {a.hours:g}h: too few waits there)" if hours > a.hours else ""
    scope = (f"{a.repo} {a.workflow} branch={a.branch or '*'} event={a.event or '*'} last {hours:g}h"
             f"{widened} per {a.unit}")
    if len(waits) < need:
        if ever == 0:
            print(f"ci-queue-wait: DID NOT FINISH: {scope}: the filter matches NO completed run ever "
                  f"-- a wrong --workflow/--branch/--event, not an idle window")
            return 2
        seen = f" (max {max(w for _, w, _ in waits):.0f}s)" if waits else ""
        line = (f"{scope}: {len(waits)} {a.unit} wait(s){seen} measured from {read} run(s), need {need}; "
                f"the filter has matched {ever} completed run(s) before, so this window was quiet, "
                f"not unmeasurable. No limit judged")
        print(f"ci-queue-wait: IDLE: {line}")
        _summary("IDLE", line)
        return 0
    vals = [w for _, w, _ in waits]
    worst = max(waits, key=lambda t: t[1])
    limits = {k: v for k, v in (("p90", a.max_p90), ("p95", a.max_p95)) if v is not None}
    got = {"p90": p90(vals), "p95": pct(vals, 0.95)}
    capped = f" (newest {read} runs of more: a sample)" if truncated else ""
    line = (f"{scope}: {len(vals)} {a.unit}(s){capped}, {sum(c for *_, c in waits)} ended still queued "
            f"(censored), {empty} run(s) with nothing to measure; queue wait p50 "
            f"{sorted(vals)[len(vals) // 2]:.0f}s p90 {got['p90']:.0f}s p95 {got['p95']:.0f}s "
            f"max {worst[1]:.0f}s (run {worst[0]}); limit " + " ".join(f"{k} {v:.0f}s" for k, v in limits.items()))
    bad = any(got[k] > v for k, v in limits.items())
    print(("ci-queue-wait: OVER LIMIT: " if bad else "ci-queue-wait: ok: ") + line)
    _summary("OVER LIMIT" if bad else "ok", line)
    return 1 if bad else 0


def _summary(verdict: str, line: str) -> None:
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(f"### CI queue wait\n\n{verdict}: {line}\n")


if __name__ == "__main__":
    sys.exit(main())
