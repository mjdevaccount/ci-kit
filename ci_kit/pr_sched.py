"""The PR scheduler's GitHub side: read who holds the scarce pools, park, re-run, and say so.

The POLICY is pure and lives in ci_kit/pr_queue.py (`head`, `schedule`, `admit`; the WHY and the
measured thresholds are there). This module is its two actors:

  * `pass_(...)`, called by pr_autoupdate on every run: publishes nothing new, it ACTS --
      - cancels each non-head run `schedule` names (a PREEMPTION), labels its PR `parked`;
      - RE-RUNS parked work: the head's at once (a head BEHIND main gets the queue's update
        instead, which is its fresh run), a non-head's only while no scarce pool has a queued job
        and the head needs none -- one per pass, lowest number first, so a freed pool is refilled
        in landing order rather than GitHub's;
      - takes the label off a PR with nothing parked left (a new push replaced the parked run);
      - pages an OUT-OF-QUEUE MERGE: a push to main that merged a PR other than the head while the
        head was in flight against the old main (td#3072, Renovate's own automerge, 03:53Z
        11-14, invalidated head #3073);
      - names the head, its wait per pool, every parking and re-run in the summary, and returns an
        error for a STALLED head so the run goes red (mcp ci-schedule-audit reads that streak).
  * `admit_main`, the review gate's `pre` job and the first step of its sealed job: parks a
    non-head PR's model review while the head needs the sealed pool (pr_queue.admit).

PARKED IS RED, AND LABELLED. A parked review fails `review deferred`; a preempted run is
cancelled. Both block the merge exactly as a missing check does -- nothing is waived -- and both
read as "CI failed" to an author's agent or an auto-fix bot, so the PR carries the `parked` label
(`queue:parked`; its description says it re-runs itself) from the moment it is parked until the
re-run starts. pr_heal leaves a parked PR alone: re-running it is this module's job, in order.

THE TOKENS. Reads: the job's GITHUB_TOKEN (actions: read) first, then the PAT (pr_autoupdate's
`read`). Cancel / re-run / label: the PAT, which already holds Actions: Read and write for
pr_heal's reruns; a refusal is an ERROR naming the grant, never a silent skip.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from collections.abc import Callable

from ci_kit import pr_queue
from ci_kit.ntfy import page
from ci_kit.pr_heal import PARKED  # ONE name; pr_heal reads it too

PARKED_COLOR = "bfd4f2"
PARKED_DESCRIPTION = "PR queue: CI/review parked so the queue head gets the runner; re-runs itself. NOT a failure"
DEFERRED_JOB = "review deferred"   # the review gate's job that fails when it parks a review
RECORD_JOB = "record verdict"      # the review gate's job that writes the verdict a carry reads
PRE_JOB = "review"                 # the review gate's first job (the name mcp review_gate_auth reads)
MAX_ATTEMPT = 4                    # a run re-run this often is never parked again (no starving)

Gh = Callable[..., subprocess.CompletedProcess]


def _ts(s: str | None) -> float | None:
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _tail(name: str) -> str:
    """'reviewer / review deferred' -> 'review deferred' (a reusable job's check is prefixed)."""
    return str(name or "").rsplit(" / ", 1)[-1]


def _json(read: Gh, *args: str) -> object:
    return json.loads(read(*args).stdout or "null")


def capacity_of(text: str) -> dict[str, int]:
    """'{"home-sealed": 1, "home-heavy": 1}' -> dict; '' = the scheduler is off. Raises on junk."""
    if not text.strip():
        return {}
    got = json.loads(text)
    if not isinstance(got, dict) or not all(isinstance(k, str) and isinstance(v, int) and v > 0
                                            for k, v in got.items()):
        raise ValueError(f"scarce-pools must be a JSON object of pool label -> capacity >= 1, got {text!r}")
    return got


# ── reading the pools ─────────────────────────────────────────────────────────────────────────
def active_runs(repo: str, read: Gh, branch: str = "") -> list[dict]:
    """Queued + in-progress workflow runs (REST shape), optionally one branch's."""
    out: list[dict] = []
    for status in ("queued", "in_progress"):
        q = f"repos/{repo}/actions/runs?status={status}&per_page=100" + (f"&branch={branch}" if branch else "")
        got = _json(read, "api", q)
        out += (got or {}).get("workflow_runs") or [] if isinstance(got, dict) else []
    return out


def run_jobs(repo: str, run_id: int, read: Gh) -> list[dict]:
    got = _json(read, "api", f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100")
    return (got or {}).get("jobs") or [] if isinstance(got, dict) else []


def pr_of(run: dict, by_branch: dict[str, int]) -> int:
    """The same-repo PR a run belongs to; 0 for a push/schedule run or a fork."""
    if run.get("event") not in ("pull_request", "pull_request_target"):
        return 0
    for p in run.get("pull_requests") or []:
        if p.get("number"):
            return int(p["number"])
    return by_branch.get(str(run.get("head_branch") or ""), 0)


def pool_jobs(repo: str, read: Gh, capacity: dict[str, int], by_branch: dict[str, int]) -> list[pr_queue.Job]:
    """Every queued/running job on a scarce pool, repo-wide."""
    out: list[pr_queue.Job] = []
    for run in active_runs(repo, read):
        for j in run_jobs(repo, int(run["id"]), read):
            status = str(j.get("status") or "")
            if status not in ("queued", "in_progress", "waiting", "pending"):
                continue
            pool = next((lb for lb in j.get("labels") or [] if lb in capacity), "")
            if not pool:
                continue
            out.append(pr_queue.Job(pr_of(run, by_branch), int(run["id"]), int(run.get("run_attempt") or 1),
                                    _tail(j.get("name")), pool, "in_progress" if status == "in_progress" else "queued",
                                    _ts(j.get("created_at")) or _ts(run.get("created_at")) or 0.0,
                                    _ts(j.get("started_at")) if status == "in_progress" else None))
    return out


def parked_runs(repo: str, p: dict, read: Gh) -> tuple[list[dict], bool]:
    """(the PR's parked runs at its CURRENT head, anything still active there). A parked run is the
    newest run of its workflow at that head and is cancelled, or holds a failed `review deferred`."""
    sha = str(p.get("headRefOid") or "")
    got = _json(read, "api", f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100")
    runs = (got or {}).get("workflow_runs") or [] if isinstance(got, dict) else []
    newest: dict[int, dict] = {}
    for r in runs:
        w = int(r.get("workflow_id") or 0)
        if w not in newest or str(r.get("created_at")) > str(newest[w].get("created_at")):
            newest[w] = r
    active = any(r.get("status") != "completed" for r in newest.values())
    out = []
    for r in newest.values():
        if r.get("status") != "completed":
            continue
        if r.get("conclusion") == "cancelled":
            out.append(r)
        elif r.get("conclusion") == "failure" and any(
                _tail(j.get("name")) == DEFERRED_JOB and j.get("conclusion") == "failure"
                for j in run_jobs(repo, int(r["id"]), read)):
            out.append({**r, "deferred": True})
    return out, active


def review_settled(repo: str, branch: str, read: Gh) -> bool:
    """No review run on this branch still owes a verdict: each active run of the review gate has
    finished `record verdict` (or parked). An update now cancels only the gate's tail, and the next
    run can carry -- the 03:58Z case: head #3073 BEHIND, verdict recorded, update held for the tail."""
    for run in active_runs(repo, read, branch):
        names = {_tail(j.get("name")): j for j in run_jobs(repo, int(run["id"]), read)}
        if PRE_JOB not in names:
            continue  # not the review gate
        done = [n for n in (RECORD_JOB, DEFERRED_JOB) if (names.get(n) or {}).get("status") == "completed"]
        if not done:
            return False
    return True


# ── acting ────────────────────────────────────────────────────────────────────────────────────
def _label(repo: str, n: int, write: Gh, add: bool) -> bool:
    from ci_kit.pr_autoupdate import add_label  # lazy: pr_autoupdate imports this module
    if add:
        return add_label(repo, n, PARKED, PARKED_COLOR, PARKED_DESCRIPTION)[0]
    return write("pr", "edit", str(n), "-R", repo, "--remove-label", PARKED).returncode == 0


def ensure_label(repo: str, write: Gh) -> bool:
    """The review gate's admission adds `queue:parked` with the job token, which cannot create it."""
    return write("label", "create", PARKED, "-R", repo, "--color", PARKED_COLOR,
                 "--description", PARKED_DESCRIPTION, "--force").returncode == 0


def _grant(msg: str) -> str:
    if "resource not accessible" in msg.lower() or "403" in msg:
        return msg + " -- the push token needs Actions: Read and write to park or re-run a run"
    return msg


def rerun(repo: str, r: dict, write: Gh) -> str:
    """'' on success. A parked review re-runs WHOLE (its `pre` job must decide again); a preempted
    run re-runs its cancelled jobs (finished ones are kept)."""
    verb = "rerun" if r.get("deferred") else "rerun-failed-jobs"
    got = write("api", "-X", "POST", f"repos/{repo}/actions/runs/{r['id']}/{verb}")
    return "" if got.returncode == 0 else _grant((got.stderr or got.stdout or "").strip()[-300:])


def out_of_queue(repo: str, prs: list[dict], before: str, sha: str, read: Gh) -> str:
    """'' or the page body: the push `before..sha` merged a PR numbered AFTER the queue head while
    the head was in flight (current with `before`, no failing check) -- only an in-flight head was
    hurt. A lower-numbered merge was the head itself (it is closed now, so `head` is the next one)."""
    if not before or not sha or set(before) == {"0"}:
        return ""
    merged = _json(read, "api", f"repos/{repo}/commits/{sha}/pulls")
    # a train landing IS the queue (ci_kit/merge_train); anything else merged past an in-flight head is not
    nums = [int(m["number"]) for m in merged or [] if isinstance(m, dict) and m.get("merged_at")
            and not str((m.get("head") or {}).get("ref") or "").startswith("train/")]
    h = pr_queue.head(prs)
    nums = [n for n in nums if h is not None and n > int(h["number"])]
    if not nums or h is None:
        return ""
    if pr_queue.check_state(h.get("statusCheckRollup")) == "failing":
        return ""
    cmp = _json(read, "api", f"repos/{repo}/compare/{before}...{h['headRefOid']}")
    if not isinstance(cmp, dict) or int(cmp.get("behind_by", 1)) != 0:
        return ""
    return (f"#{', #'.join(map(str, nums))} merged to main while queue head #{h['number']} was in flight "
            f"against the previous main ({before[:12]}): #{h['number']} must re-test. A merge outside the "
            "queue (Renovate's own automerge, a hand merge) costs the head its run.")


def pass_(repo: str, prs: list[dict], capacity: dict[str, int], read: Gh, write: Gh, summary: list[str],
          *, preempt_after_s: float, stall_s: float, behind: Callable[[dict], bool],
          before: str = "", sha: str = "", now: float | None = None, updated: frozenset[int] = frozenset()) -> int:
    """One scheduler pass (see the module doc). `updated` = PRs this run merged main into: their
    push starts fresh runs, so nothing of theirs is re-run here. Returns the errors."""
    now = dt.datetime.now(dt.UTC).timestamp() if now is None else now
    errors = 0
    same = [p for p in prs if not p.get("isCrossRepository")]
    by_branch = {str(p.get("headRefName")): int(p["number"]) for p in same}
    h = pr_queue.head(same)
    hn = int(h["number"]) if h else None
    if capacity and not ensure_label(repo, write):
        errors += 1  # the review gate's admission can only park a labelled PR: until this lands it runs all
        print(f"::error::{repo}: could not create the `{PARKED}` label; admission parks nothing until it exists")
    oq = out_of_queue(repo, same, before, sha, read)
    if oq:
        summary.append(f"OUT-OF-QUEUE MERGE: {oq}; page " + page(f"{repo.rsplit('/', 1)[-1]}: out-of-queue merge", oq,
                                                                 str(h.get("url") or "") if h else ""))
        print(f"::warning::{oq}")
    jobs = pool_jobs(repo, read, capacity, by_branch)
    s = pr_queue.schedule(hn, jobs, capacity, now, preempt_after_s, stall_s, MAX_ATTEMPT)
    load = ", ".join(f"{pool} {sum(j.status == 'in_progress' for j in jobs if j.pool == pool)}/{cap} running "
                     f"{sum(j.status == 'queued' for j in jobs if j.pool == pool)} queued" for pool, cap in sorted(capacity.items()))
    summary.append(f"scheduler: head #{hn}" if hn else "scheduler: no queue head")
    summary.append(f"pools: {load}")
    summary += [f"head #{hn} waiting on {pool}: {w / 60:.1f} min" for pool, w in sorted(s.demand.items())]
    for line in s.stalled:
        errors += 1
        print(f"::warning::queue-head-starved {line}")
        summary.append(f"HEAD STARVED: {line}")
    errors += _park(repo, s.preempt, write, summary)
    room = not any(j.status == "queued" for j in jobs) and not s.demand
    errors += _refill(repo, same, hn, {j.pr for j, _ in s.preempt} | set(updated), room, read, write, summary, behind)
    return errors


def _park(repo: str, preempt: list[tuple[pr_queue.Job, str]], write: Gh, summary: list[str]) -> int:
    """Label each PR `schedule` named, THEN cancel its run. Returns the errors.

    Label first: `_refill` finds parked work by the label, so a run cancelled on an unlabelled PR
    would be stranded. No label, no cancel -- the job keeps the pool and runs to its end."""
    errors = 0
    for j, why in preempt:
        if not _label(repo, j.pr, write, True):
            errors += 1
            print(f"::error::#{j.pr}: could not add `{PARKED}`, so run {j.run} was NOT parked")
            summary.append(f"#{j.pr}: park of run {j.run} skipped: `{PARKED}` could not be added")
            continue
        got = write("api", "-X", "POST", f"repos/{repo}/actions/runs/{j.run}/cancel")
        if got.returncode:
            errors += 1
            err = _grant((got.stderr or got.stdout or "").strip()[-300:])
            print(f"::error::#{j.pr}: could not park run {j.run}: {err}")
            summary.append(f"#{j.pr}: park of run {j.run} REFUSED: {err}")
            continue  # the label stays; _refill drops it once nothing at the head is parked
        summary.append(f"#{j.pr}: PARKED run {j.run} ({j.name}): {why}")
    return errors


def _refill(repo: str, prs: list[dict], hn: int | None, just: set[int], room: bool, read: Gh, write: Gh,
            summary: list[str], behind: Callable[[dict], bool]) -> int:
    """Re-run parked work: the head's now, one non-head's when the pools have room. Never strands:
    a PR with the label and nothing parked left loses the label. Returns the errors."""
    errors, refilled = 0, False
    for p in sorted((p for p in prs if any(lb.get("name") == PARKED for lb in p.get("labels") or [])),
                    key=pr_queue.landing_order):
        n = int(p["number"])
        if n in just:
            continue  # parked this pass, or updated (its push is the fresh run)
        runs, active = parked_runs(repo, p, read)
        if not runs:
            if not active:
                _label(repo, n, write, False)
                summary.append(f"#{n}: nothing parked at its head any more; `{PARKED}` removed")
            continue
        is_head = n == hn
        if is_head and p.get("autoMergeRequest") and behind(p):
            summary.append(f"#{n}: head, parked and behind main -> the queue's update is its fresh run")
            continue
        if not is_head and (refilled or not room):
            summary.append(f"#{n}: parked ({len(runs)} run(s)), waits for a free pool")
            continue
        # no budget needed here: admission and preemption stop parking a run at its MAX_ATTEMPT-th
        # attempt, so a re-run is the last one that can be parked
        errs = [e for e in (rerun(repo, r, write) for r in runs) if e]
        if errs:
            errors += 1
            print(f"::error::#{n}: re-run refused: {'; '.join(errs)}")
            summary.append(f"#{n}: parked re-run REFUSED: {'; '.join(errs)}")
            continue
        refilled |= not is_head
        _label(repo, n, write, False)
        summary.append(f"#{n}: parked run(s) {', '.join(str(r['id']) for r in runs)} RE-RUN "
                       + ("(queue head)" if is_head else "(pool free, next in landing order)"))
    return errors


# ── the review gate's admission (no PAT: the job's GITHUB_TOKEN, actions: read) ─────────────────
def head_demand(repo: str, h: dict, pool: str, read: Gh, pending: bool) -> bool:
    """The head has a job queued on `pool`, is parked, or (`pending`) has a review gate run whose
    `pre` job has not yet decided whether it needs the sealed pool."""
    if any(lb.get("name") == PARKED for lb in h.get("labels") or []):
        return True
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
    ap = argparse.ArgumentParser(prog="python -m ci_kit.pr_sched", description="park a non-head PR's job on a scarce pool")
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
            prs = json.loads(read("pr", "list", "-R", args.repo, "--state", "open", "--limit", "200", "--json",
                                  "number,isDraft,isCrossRepository,headRefName,headRefOid,autoMergeRequest,labels").stdout)
            me = next((p for p in prs if int(p["number"]) == args.pr), {"number": args.pr})
            h = pr_queue.head(prs)
            hn = str(h["number"]) if h else ""
            demand = bool(h) and int(h["number"]) < args.pr and head_demand(args.repo, h, args.pool, read, args.pending)
            defer, why = pr_queue.admit(me, h, demand)
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
