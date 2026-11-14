"""Which armed PRs the updater pushes this run: a serial queue in front of strict protection.

WHY (measured on trade-data, 24h to 2026-10-24 01:22Z). Updating EVERY armed PR on every push to
main ran 228 PR CI runs for 31 main pushes (7.4 per push), and 159 of them (70%) were cancelled
before finishing: each later push superseded them. That queue sat in front of main's own CI (p90
wait 235s, max 404s) and of the deploy image built from it. Under "require branches to be up
to date", only ONE PR can merge per main sha anyway: the moment it merges, every other PR is
BEHIND again. So updating N PRs at once buys one merge and throws away N-1 CI runs.

THE GATE IS UNCHANGED. Branch protection still refuses a PR that is not up to date with main and
green on its own head. This module changes only WHEN the updater makes a PR up to date: at most
`max_in_flight` armed PRs are being tested against current main at a time, lowest PR number first (`landing_order`).

The rules, per armed PR, from `gh pr list --json mergeStateStatus,statusCheckRollup,...`:
- DIRTY (conflicts with main): always attempted. The PUT pushes nothing on a conflict, so it
  costs no CI, and it is how the conflict label and page fire.
- in flight: not BEHIND/DIRTY/UNKNOWN, and either CLEAN (auto-merge is about to fire) or with a
  check still running on its head. Each one holds a slot.
- BEHIND or UNKNOWN with a FAILING check on its head: held. It cannot merge however current it
  is; the author's fix pushes a new head, and that push runs its checks. UNLESS every failing
  check failed as MACHINERY (`machinery`, from pr_heal): then it is eligible, and the update is
  its fresh run.
- BEHIND or UNKNOWN with a check still RUNNING: held, not updated. An update would cancel that
  run. The caller re-runs the updater when a check workflow completes (`workflow_run`).
- BEHIND or UNKNOWN with every present check green: eligible. The free slots go to the lowest
  PR number (`landing_order`; arm time was the key until v1.26.0 and let a re-armed PR jump).

`required` narrows which checks count (the names branch protection requires); empty = every
check on the head. A required check that is ABSENT counts as neither running nor failing. So a
check that never reports cannot hold a slot forever, and an update gives the PR fresh checks.

`max_in_flight = 0` is the old behaviour: every armed PR is attempted.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ci_kit.redclass import FAIL, QUEUE_STATUS, red_checks

_FAIL = FAIL  # the one set (ci_kit/redclass.py)
_RUNNING_STATUS = {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED"}
_STALE_STATES = {"BEHIND", "UNKNOWN"}


@dataclass
class Plan:
    update: list[dict] = field(default_factory=list)  # attempt, in this order
    in_flight: list[dict] = field(default_factory=list)
    held: list[tuple[dict, str]] = field(default_factory=list)  # (pr, why)
    # eligible PRs past the free slots, in queue order: the caller attempts the next one when a
    # PR it picked is skipped for a running review (pr_autoupdate), so a skip does not idle the slot
    spare: list[dict] = field(default_factory=list)


def check_state(rollup: list[dict] | None, required: tuple[str, ...] = ()) -> str:
    """'failing' | 'running' | 'green' for the checks present on a PR's head. Pure."""
    running = False
    for c in rollup or []:
        name = c.get("name") or c.get("context") or ""
        if (required and name not in required) or name == QUEUE_STATUS:  # the hold is order, not a check
            continue
        if c.get("__typename") == "StatusContext" or ("state" in c and "status" not in c):  # commit status
            state = (c.get("state") or "").upper()
            if state in _FAIL:
                return "failing"
            running |= state in ("PENDING", "EXPECTED")
            continue
        if (c.get("status") or "").upper() != "COMPLETED":
            running = True
            continue
        if (c.get("conclusion") or "").upper() in _FAIL:
            return "failing"
    return "running" if running else "green"


def landing_order(p: dict) -> int:
    """THE order armed PRs land in, for the serial queue AND the merge train: lowest PR number
    first. The operator reads a backlog by number, so arm time (re-armed by any re-push or
    autoland toggle) reordered it behind his back (trade-data 2026-11-12: #2980 waited behind
    #2993/#2994). One key, so the two consumers cannot drift."""
    return int(p.get("number", 0))


def plan(armed: list[dict], max_in_flight: int, required: tuple[str, ...] = (),
         machinery: frozenset[int] = frozenset()) -> Plan:
    """Split the armed PRs into update / in flight / held. Pure: the policy the tests pin.

    `machinery` = PRs whose every failing check is MACHINERY (ci_kit/redclass.py, read by
    pr_heal): such a PR BEHIND main is eligible as if green -- the update gives it fresh checks,
    which is the rerun it needs, with no rerun permission. Its failing checks are no author's
    to fix, so "the author's fix re-queues it" would wait forever."""
    out = Plan()
    if max_in_flight <= 0:
        out.update = list(armed)
        return out
    eligible: list[dict] = []
    for p in sorted(armed, key=landing_order):
        merge = (p.get("mergeStateStatus") or "UNKNOWN").upper()
        checks = check_state(p.get("statusCheckRollup"), required)
        if merge == "DIRTY":
            out.update.append(p)
        elif merge not in _STALE_STATES:
            if merge == "CLEAN" or checks == "running":
                out.in_flight.append(p)
            else:
                out.held.append((p, f"current with main, checks {checks}"))
        elif checks == "failing" and p.get("number") not in machinery:
            out.held.append((p, "behind, a check is failing on its head (the author's fix re-queues it)"))
        elif checks == "running":
            out.held.append((p, "behind, checks still running (an update would cancel them)"))
        else:
            eligible.append(p)
    slots = max(0, max_in_flight - len(out.in_flight))
    out.update += eligible[:slots]
    out.spare = eligible[slots:]
    for i, p in enumerate(eligible[slots:], 1):
        out.held.append((p, f"queued, position {i} (in flight: {len(out.in_flight)}/{max_in_flight})"))
    return out


# ── the PR SCHEDULER (v1.29.0): ORDER and TIMING on the scarce runner pools, never fewer checks ──
#
# WHY (trade-data 2026-11-14 03:58Z, operator: "why are runners working on ANYTHING else other than
# 3073? its WAITING and its next in line"). The queue above decides which PR main is merged INTO;
# GitHub decides which job gets a runner, first come first served with no priority -- and measured
# over 11-07..11-14 it did not even keep that order: on the one-runner pools a lower-numbered PR's job
# sat behind a higher one's 90x on home-sealed (23% of PR jobs, wait p90 21 min) and 245x on
# home-heavy (30%, p90 61 min, 100 runner-hours of waiting), and in half of those the other job was
# DISPATCHED AFTER the waiting one queued. This is the policy that puts the queue's order on the
# pools. It changes when a job runs and nothing else: every PR still passes every required check on
# the exact head that merges -- a parked job is re-run, never skipped, and a parked check is red.
#
#   HEAD       the lowest-numbered same-repo, non-draft queue member: auto-merge ARMED (`member`). A
#              train is never one. Renovate merges itself (platformAutomerge off), so it is no
#              member; its landing past an in-flight head is paged instead (pr_sched.out_of_queue).
#   DEMAND     the head has a job QUEUED on a scarce pool (`capacity`, from the caller's config).
#   WORK-CONSERVING: a non-head job is parked only while the head has demand on ITS pool; with none
#              it runs, so an idle pool never waits for the head.
#   PREEMPT    while the head has demand on a pool, every non-head job QUEUED there is cancelled (it
#              could be dispatched first, and in half the measured cases it was), and as many
#              non-head jobs RUNNING there for less than `preempt_after_s` as the head's queued jobs
#              need. Measured: a sealed carry finishes in ~0.2 min, a full review p50 4.8; a unit job
#              p50 6.1; 85/90 sealed and 196/245 heavy inversions arrived within 5 min of the holder
#              starting -- so 3 min. Older holders finish. Never the head, never a non-PR run (main's
#              push CI, nightly), never a run already on its `max_attempt`th attempt (no starving).
#   STALL      the head's oldest queued job on a pool has waited past `stall_s` while a non-head job
#              runs there: the line the caller turns red (15 min: just over the p90 full review).

def member(p: dict) -> bool:
    """A PR the queue orders: same-repo, not a draft, not a train, and ARMED. Pure.

    Renovate's PRs are members only when armed. ci-kit's preset sets `platformAutomerge: false`:
    Renovate merges ITSELF once every check passes, so its PRs are never armed -- and the list
    gives no way to tell a self-merging one from a major bump left for a human. Counted by author
    or branch, that bump left open a week would be the head all week, and the PR the operator is
    waiting on would never get the head's priority. So Renovate's merges are watched, not ordered:
    one landing past an in-flight head is paged (pr_sched.out_of_queue). Arm them (pr_reconcile
    does, hourly; or Renovate `platformAutomerge: true`) and they join the queue in number order,
    like any PR. With the queue hold on (ci_kit/pr_hold.py) an UNARMED one reads `queue/position`
    pending, so Renovate's own merge waits for that arming (td#3079, 06:49Z 11-14)."""
    branch = str(p.get("headRefName") or "")
    return (not p.get("isCrossRepository") and not p.get("isDraft") and not branch.startswith("train/")
            and bool(p.get("autoMergeRequest")))


def head(prs: list[dict], skip: frozenset[int] = frozenset()) -> dict | None:
    """THE queue head: the lowest-numbered member (landing_order) not in `skip`. Pure. A head blocked
    on its own red holds no runner -- it has no demand -- so it costs the rest nothing
    (work-conserving). `skip` = `blocked(...)`: the members that cannot land as they stand."""
    members = [p for p in prs if member(p) and int(p["number"]) not in skip]
    return min(members, key=landing_order) if members else None


def blocked(prs: list[dict], required: tuple[str, ...] = (), eligible: frozenset[int] = frozenset(),
            base: str | None = None) -> frozenset[int]:
    """The PRs that cannot land AS THEY STAND -- never the head, for the hold, the updater and the
    scheduler alike (one head, pr_queue.head(prs, skip)). Pure. Blocked:
      * DIRTY or CONFLICTING (a conflict with its base; the merge state reads UNKNOWN for a while
        after a push to main, `mergeable` often already knows);
      * a required check RED on its head (redclass.red_checks: failing or CANCELLED, newest run per
        name), unless the PR is `eligible` (parked or machinery-red) AND behind main: then the
        queue's own update is its fresh run. Current with main, a re-run is owed that this token
        may not be able to make (no Actions write), so it is skipped until that run is going;
      * stacked: into a branch other than `base` (the default branch; None = unknown, none).
    WHY: the queue hold (ci_kit/pr_hold.py) gives `queue/position` success to ONE PR; were that one
    unable to land, nothing could merge into main (review of the hold, finding 1). A skipped PR is
    the head again the moment its fix (or re-run) leaves it running or green."""
    out = set()
    for p in prs:
        n = int(p["number"])
        stale = (p.get("mergeStateStatus") or "UNKNOWN").upper() in _STALE_STATES
        if (p.get("mergeStateStatus") or "").upper() == "DIRTY" or (p.get("mergeable") or "").upper() == "CONFLICTING" \
                or (base and p.get("baseRefName") != base):
            out.add(n)
        elif not (n in eligible and stale) and red_checks(p.get("statusCheckRollup"), required):
            out.add(n)
    return frozenset(out)


@dataclass(frozen=True)
class Job:
    """One queued or running job on a pool, from the Actions jobs API."""
    pr: int          # 0 = not a pull_request run (push to main, schedule): never preempted
    run: int
    attempt: int
    name: str
    pool: str        # the scarce pool label it targets ('' = none)
    status: str      # queued | in_progress
    created: float   # epoch seconds
    started: float | None = None


@dataclass
class Sched:
    head: int | None
    preempt: list[tuple[Job, str]] = field(default_factory=list)   # (job, why) -- cancel its run
    demand: dict[str, float] = field(default_factory=dict)          # pool -> head's oldest queued wait (s)
    stalled: list[str] = field(default_factory=list)                # one line per starved pool


def schedule(head_pr: int | None, jobs: list[Job], capacity: dict[str, int], now: float,
             preempt_after_s: float = 180, stall_s: float = 900, max_attempt: int = 4) -> Sched:
    """Which non-head runs to cancel so the head gets the pool next, and which pools starve it. Pure."""
    out = Sched(head_pr)
    if head_pr is None:
        return out
    for pool, cap in sorted(capacity.items()):
        here = [j for j in jobs if j.pool == pool]
        mine = [j for j in here if j.pr == head_pr and j.status == "queued"]
        if not mine:
            continue
        wait = now - min(j.created for j in mine)
        out.demand[pool] = wait
        others = [j for j in here if j.pr not in (0, head_pr) and j.attempt < max_attempt]
        running = [j for j in here if j.status == "in_progress"]
        holders = [j for j in running if j.pr not in (0, head_pr)]
        if wait > stall_s and holders:
            out.stalled.append(f"{pool}: head #{head_pr} has waited {wait / 60:.0f} min while "
                               + ", ".join(f"#{j.pr} ({j.name})" for j in holders) + " held it")
        for j in (j for j in others if j.status == "queued"):
            out.preempt.append((j, f"queued on {pool} ahead of head #{head_pr}"))
        need = len(mine) - max(0, cap - len(running))
        young = sorted((j for j in others if j.status == "in_progress" and j.started is not None
                        and now - j.started < preempt_after_s), key=lambda j: -(j.started or 0))
        for j in young[:max(0, need)]:
            out.preempt.append((j, f"started {(now - (j.started or now)) / 60:.1f} min ago on {pool}, "
                                   f"head #{head_pr} waiting"))
    seen: set[int] = set()
    out.preempt = [(j, w) for j, w in out.preempt if not (j.run in seen or seen.add(j.run))]
    return out


def admit(me: dict, head_pr: dict | None, head_demand: bool) -> tuple[bool, str]:
    """(park?, why) for one PR's job about to take a scarce pool. Pure. Parks ONLY a PR numbered
    after the head while the head has demand there: the head itself, a PR ahead of it in landing
    order (a fixed red, about to be head), and every PR when the head needs nothing all run."""
    if head_pr is None:
        return False, "no queue head"
    if int(me["number"]) == int(head_pr["number"]):
        return False, "this PR is the queue head"
    if int(me["number"]) < int(head_pr["number"]):
        return False, f"ahead of the queue head #{head_pr['number']} in landing order"
    if not head_demand:
        return False, f"queue head #{head_pr['number']} needs nothing on this pool (work-conserving)"
    return True, f"parked: queue head #{head_pr['number']} needs this pool; re-run automatically when it is free"
