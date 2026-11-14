"""Wake the merge train when a pr-autoupdate pass sees work for it (v1.32.0).

WHY (trade-data, 2026-11-14 00:00-17:38Z). merge-train ran on a cron, "7,27,47 * * * *": 49 of
its 52 passes logged "fewer than 2 candidates", and each was a single-use job on the caller's
SECURE pool (three slots estate-wide, ~1.64 runner-min a job whatever its length). pr-autoupdate
already reads every open PR with its checks on every push and every PR's last check, so it can
say when a train pass would find work, and dispatch one then. The caller's cron stays as an
hourly BACKSTOP: a pass this misses (an unreadable run list, a candidate a later event never
re-announces) waits at most an hour, not forever.

WHEN, pure (`decide`). Dispatch the caller's train workflow only when ALL hold:
  * no train PR is open -- an open train's own checks and close events wake its pass;
  * at least `min_batch` CANDIDATES by the train's own rule (ci_kit/merge_train/policy.candidates:
    armed, green, not conflicting, not ejected, not a draft, not opted out) -- one definition, so
    this can never call a PR ready that the train would not take;
  * none of them is about to LAND ON ITS OWN (mergeStateStatus CLEAN / HAS_HOOKS / UNSTABLE): its
    merge to main is a push, the next pass decides again. This narrows the race, it does not close
    it: the queue head still in CI is no candidate, but its landing a minute after a train is
    composed makes that train stale, and a green rider held only by `queue/position` can turn
    CLEAN as the new head and land beside the train -- the same races the cron pass already had;
  * no PASS of the train workflow is queued or running (its concurrency group would only stack it);
  * the newest PASS was created before the newest candidate became ready (`ready_at`: the last
    required check completing, or auto-merge being armed). A pass created after already judged
    this exact candidate set; asking again changes nothing until a head, a check or an arming
    does. That is the dedup: no state kept here, none needed. (`run_started_at` is read first, but
    GitHub sets it equal to `created_at` for a first attempt -- it differs only on a re-run. So a
    pass that waited for a runner and read a candidate made ready while it waited still dates
    from its creation: one extra dispatch at worst, never a missed one.)

WHAT IS A PASS. The train workflow also fires on every CI / review / image completion and on PR
labels and closes; its job `if:` skips all but a train's, and GitHub still records each as a run
(conclusion `skipped`) -- 9 of the newest 10 on trade-data, 2026-11-14. Such a run judged nothing,
so the run list is read per PASS EVENT (`schedule`, `workflow_dispatch`: never skipped by the
`if:`) and a skipped or cancelled run is not a pass either. The caller's OTHER real passes -- a
`merge-train` label, a train closing, a train's own check completing -- are not read: when one of
them lands a train and composes the next, the push to main can dispatch a pass beside it, which
the concurrency group queues and which then finds the new train open. One extra secure-pool job
per train landing at worst, never a missed wake; counting every in-flight run of any event as
busy would instead let a skipped run's few queued seconds swallow a wake until the backstop.

FAILS LOUD. An unreadable run list or a refused dispatch is an error (a red pr-autoupdate run)
naming the grant -- the PAT's Actions: Read and write -- and dispatches nothing: a dispatch on
every pass because the dedup could not read would cost more than the cron it replaces.

TELEMETRY. Every decision is a summary line (`merge train: ...`), so the run list says when and
why a train was woken; the caller's merge-train run list shows each dispatched pass as event
`workflow_dispatch`. From outside, mcp ci-schedule-audit watches the backstop cron.
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import datetime

from ci_kit.merge_train import TRAIN_PREFIX, policy
from ci_kit.redclass import QUEUE_STATUS

MIN_BATCH = 2
ACTIVE = frozenset({"queued", "in_progress", "waiting", "pending", "requested"})
PASS_EVENTS = ("workflow_dispatch", "schedule")  # the events the train job's `if:` never skips
NOT_A_PASS = frozenset({"skipped", "cancelled"})
SELF_LANDING = frozenset({"CLEAN", "HAS_HOOKS", "UNSTABLE"})

Gh = Callable[..., subprocess.CompletedProcess]


def _ts(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def ready_at(pr: dict, required: tuple[str, ...]) -> datetime | None:
    """When this candidate last changed in a way a train pass would see: its newest required
    check completing (a status: posted), or auto-merge being armed. Pure. The queue hold's
    `queue/position` is ORDER, re-posted every pass -- never a readiness signal."""
    stamps = [_ts((pr.get("autoMergeRequest") or {}).get("enabledAt"))]
    for c in pr.get("statusCheckRollup") or []:
        name = c.get("name") or c.get("context") or ""
        if name == QUEUE_STATUS or (required and name not in required):
            continue
        stamps.append(_ts(c.get("completedAt") or c.get("createdAt")))
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


def passes(runs: list[dict]) -> list[dict]:
    """The runs that judged (or will judge) the candidate set: a pass event, never skipped or
    cancelled. Pure."""
    return [r for r in runs if str(r.get("event") or "") in PASS_EVENTS
            and str(r.get("conclusion") or "") not in NOT_A_PASS]


def decide(prs: list[dict], required: tuple[str, ...], base: str, runs: list[dict],
           min_batch: int = MIN_BATCH) -> tuple[bool, str]:
    """(dispatch?, why). `runs` = the train workflow's newest runs (GitHub's `workflow_runs`, any
    event: only `passes` count). Pure."""
    trains = [p for p in prs if str(p.get("headRefName") or "").startswith(TRAIN_PREFIX)
              and not p.get("isCrossRepository")]
    if trains:
        return False, f"train #{trains[-1]['number']} is open (its own checks wake its pass)"
    cands, _ = policy.candidates(prs, required, base)
    if len(cands) < min_batch:
        return False, f"{len(cands)} candidate(s), fewer than {min_batch}"
    names = ", ".join(f"#{p['number']}" for p in cands)
    landing = [p for p in cands if str(p.get("mergeStateStatus") or "").upper() in SELF_LANDING]
    if landing:
        return False, (f"{len(cands)} candidates ({names}) but #{landing[0]['number']} is landing on its own "
                       "auto-merge; its push to main re-decides")
    runs = passes(runs)
    active = [r for r in runs if str(r.get("status") or "") in ACTIVE]
    if active:
        return False, f"{len(cands)} candidates ({names}); pass run {active[0].get('id')} already {active[0].get('status')}"
    ready = max((t for t in (ready_at(p, required) for p in cands) if t), default=None)
    last = max((t for t in (_ts(r.get("run_started_at") or r.get("created_at")) for r in runs) if t), default=None)
    if ready and last and last >= ready:
        return False, (f"{len(cands)} candidates ({names}) unchanged since the last pass "
                       f"({last:%H:%M:%SZ} >= ready {ready:%H:%M:%SZ})")
    return True, f"{len(cands)} candidates ({names}) ready since {f'{ready:%H:%M:%SZ}' if ready else '?'}"


def kick(repo: str, workflow: str, prs: list[dict], required: tuple[str, ...], base: str,
         gh: Gh, summary: list[str], min_batch: int = MIN_BATCH) -> int:
    """One decision, and the dispatch when it says so. Returns the errors (0 or 1)."""
    runs: list[dict] = []
    try:
        for event in PASS_EVENTS:  # per event: the skipped workflow_run rows would crowd a page out
            r = gh("api", f"repos/{repo}/actions/workflows/{workflow}/runs?event={event}&per_page=5", check=False)
            if r.returncode:
                raise ValueError((r.stderr or "").strip()[:200] or f"exit {r.returncode}")
            runs += json.loads(r.stdout or "{}").get("workflow_runs") or []
    except (ValueError, AttributeError) as e:
        summary.append(f"merge train: could not read {workflow}'s runs ({e}); NOT dispatched -- the token "
                       f"needs Actions: Read and write, and `train-workflow` must name the caller's train "
                       f"workflow FILE in .github/workflows (a 404 is usually a wrong name)")
        print(f"::error::merge train: could not read {workflow}'s runs: {e}")
        return 1
    go, why = decide(prs, required, base, runs, min_batch)
    if not go:
        summary.append(f"merge train: not woken, {why}")
        return 0
    d = gh("workflow", "run", workflow, "-R", repo, "--ref", base, check=False)
    if d.returncode:
        err = (d.stderr or "").strip()[:200]
        summary.append(f"merge train: dispatch of {workflow} REFUSED ({err}) -- the token needs Actions: Read and "
                       "write, and the workflow must declare `workflow_dispatch`")
        print(f"::error::merge train: dispatch of {workflow} refused: {err}")
        return 1
    summary.append(f"merge train: {workflow} dispatched, {why}")
    return 0
