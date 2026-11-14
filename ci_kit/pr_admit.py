"""The review gate's ADMISSION: may this PR's full model review take the sealed runner now?

`python -m ci_kit.pr_admit --repo R --pr N --pool LABEL [--pending] [--attempt K]`, run twice by
.github/workflows/review-gate.yml (`queue-priority`): in `decide` (with --pending: a head whose
review is still deciding counts as demand) and as the first step of `model review`, before the PR
is checked out (GitHub hands a freed runner to any queued job, often not the oldest). Writes
`defer=true|false`, `head=`, `why=` to GITHUB_OUTPUT. The policy is pr_queue.admit (pure); the
scheduler that re-runs what this parks is ci_kit/pr_sched.py.

FAILS OPEN: a look that fails, a label that cannot be added (the label is how pr-autoupdate FINDS
parked work), or a run already re-run MAX_ATTEMPT times -- all REVIEW now. A park is never blind.
It ADDS the label, never creates it: pr_sched creates it only after proving its token can re-run
what is parked (`can_act`), so a repo whose token cannot parks nothing here either. A label made
while the grant held carries pr_sched's OFF description once it is lost: that parks nothing too.
Token: the job's GITHUB_TOKEN (pull-requests: write for the label, actions: read for the jobs).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from ci_kit import pr_hold, pr_queue
from ci_kit.pr_sched import MAX_ATTEMPT, PARKED, PARKED_OFF, PRE_JOB, Gh, _tail, active_runs, run_jobs


def head_demand(repo: str, h: dict, pool: str, read: Gh, pending: bool) -> bool:
    """The head has a job queued on `pool`, or (`pending`) a review gate run whose `decide` job has
    not yet decided whether it needs the sealed pool. Its `parked` label is NOT demand: a parked
    head is re-run by the next pass (at once), and then its queued job is the demand -- counting
    the label would park every other PR from the head's update until its new runs finish."""
    for run in active_runs(repo, read, str(h.get("headRefName") or "")):
        if run.get("head_sha") != h.get("headRefOid"):
            continue
        for j in (jobs := run_jobs(repo, int(run["id"]), read)):
            if pool in (j.get("labels") or []) and j.get("status") in ("queued", "waiting", "pending"):
                return True
        pre = next((j for j in jobs if _tail(j.get("name")) == PRE_JOB), None)
        if pending and pre is not None and pre.get("status") != "completed":
            return True
    return False


def admit_main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ci_kit.pr_admit", description="park a non-head PR's job on a scarce pool")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--pool", required=True, help="the scarce pool label this job would take ('' = not scarce: admit)")
    ap.add_argument("--pending", action="store_true", help="a head still deciding counts as demand (the pre job)")
    ap.add_argument("--attempt", type=int, default=1, help="this run's attempt: from MAX_ATTEMPT on it is never parked")
    args = ap.parse_args(argv)

    def read(*a: str) -> subprocess.CompletedProcess:
        return subprocess.run(["gh", *a], check=True, capture_output=True, text=True)

    defer, why, hn = False, "pool is not scarce", ""
    try:
        if args.attempt >= MAX_ATTEMPT:
            why = f"attempt {args.attempt}: parked often enough, runs now (no starving)"
        elif args.pool:
            # no statusCheckRollup: the review gate's GITHUB_TOKEN has no checks/statuses read, and
            # the GraphQL rollup would be refused (admission would then never park at all)
            prs = json.loads(read("pr", "list", "-R", args.repo, "--state", "open", "--limit", "200", "--json",
                                  "number,isDraft,isCrossRepository,headRefName,headRefOid,autoMergeRequest,labels,"
                                  "baseRefName,mergeStateStatus,mergeable").stdout)
            me = next((p for p in prs if int(p["number"]) == args.pr), {"number": args.pr})
            # the hold's head as far as this token can see it: a conflicting or stacked PR is never
            # the head. A RED lowest PR it cannot see: that one parks later PRs only while it still
            # has a job queued on the pool (its demand), bounded by MAX_ATTEMPT -- a known gap until
            # callers grant the review gate checks/statuses read
            h = pr_queue.head(prs, pr_queue.blocked(prs, base=pr_hold.default_branch(args.repo, read)))
            hn = str(h["number"]) if h else ""
            demand = bool(h) and int(h["number"]) < args.pr and head_demand(args.repo, h, args.pool, read, args.pending)
            defer, why = pr_queue.admit(me, h, demand)
            if defer:
                labels = json.loads(read("label", "list", "-R", args.repo, "--search", PARKED, "--json",
                                         "name,description").stdout or "[]")
                mine = next((lb for lb in labels if lb.get("name") == PARKED), None)
                if mine is None or str(mine.get("description") or "").startswith(PARKED_OFF[:12]):
                    defer, why = False, f"the PR queue is off (`{PARKED}` {'missing' if mine is None else 'marked OFF'}); running"
            if defer and not any(lb.get("name") == PARKED for lb in me.get("labels") or []):
                # the label is how pr-autoupdate FINDS parked work: no label, no park (never strand)
                r = subprocess.run(["gh", "pr", "edit", str(args.pr), "-R", args.repo, "--add-label", PARKED],
                                   capture_output=True, text=True)
                if r.returncode:
                    defer, why = False, f"could not add `{PARKED}` ({r.stderr.strip()[:200]}); running"
                    print(f"::warning::{why}")
    except (subprocess.CalledProcessError, ValueError, KeyError, OSError) as e:
        # could not look: RUN the job (the check it guards still runs in full) -- never park blind
        defer, why = False, f"admission could not look ({e}); running"
        print(f"::warning::{why}")
    why = " ".join(why.split())  # one line: it is a GITHUB_OUTPUT value
    print(f"defer={str(defer).lower()} head={hn}: {why}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"defer={str(defer).lower()}\nhead={hn}\nwhy={why}\n")
    return 0


if __name__ == "__main__":
    sys.exit(admit_main())
