"""Which armed PRs the updater pushes this run: a serial queue in front of strict protection.

WHY (measured on trade-data, 24h to 2026-10-24 01:22Z). Updating EVERY armed PR on every push to
main ran 228 PR CI runs for 31 main pushes (7.4 per push), and 159 of them (70%) were cancelled
before finishing: each later push superseded them. That queue sat in front of main's own CI (p90
wait 235s, max 404s) and of the deploy image built from it. Under "require branches to be up
to date", only ONE PR can merge per main sha anyway: the moment it merges, every other PR is
BEHIND again. So updating N PRs at once buys one merge and throws away N-1 CI runs.

THE GATE IS UNCHANGED. Branch protection still refuses a PR that is not up to date with main and
green on its own head. This module changes only WHEN the updater makes a PR up to date: at most
`max_in_flight` armed PRs are being tested against current main at a time, oldest-armed first.

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
- BEHIND or UNKNOWN with every present check green: eligible. The free slots go to the oldest
  `autoMergeRequest.enabledAt`.

`required` narrows which checks count (the names branch protection requires); empty = every
check on the head. A required check that is ABSENT counts as neither running nor failing. So a
check that never reports cannot hold a slot forever, and an update gives the PR fresh checks.

`max_in_flight = 0` is the old behaviour: every armed PR is attempted.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ci_kit.redclass import FAIL

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
        if required and name not in required:
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


def _armed_at(p: dict) -> tuple[str, int]:
    return ((p.get("autoMergeRequest") or {}).get("enabledAt") or "", int(p.get("number", 0)))


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
    for p in sorted(armed, key=_armed_at):
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
