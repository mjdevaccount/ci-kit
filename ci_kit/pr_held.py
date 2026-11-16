"""Which HELD PRs pr-autoupdate's opt-in `update-held` pass merges the base into. Pure.

WHY. The update pass selects ARMED PRs only (ci_kit/pr_queue.py). A PR deliberately held for
review -- open, not armed, typically labelled `autoland-off` -- was never updated, so under
"require branches to be up to date" it sat BEHIND the base until a human pressed "Update branch".
`autoland-off` opts a PR out of LANDING (pr_renovate never arms it, the heal ladder never takes
it); it does not opt it out of being kept current, and no label does: the update pass had no
opt-out label of its own, it simply never looked at an unarmed PR.

COST. Every update re-runs the PR's CI, and N stale PRs times M pushes to the base is the bill
the serial queue exists to stop. So, with the switch on:
  * WHEN: only a `schedule` or `workflow_dispatch` pass (EVENTS) -- never push, workflow_run or
    arming, which fire on every merge and every check completion;
  * ARMED FIRST: no held PR is touched in a pass where an armed PR needed an update (a BEHIND or
    UNKNOWN armed PR the queue chose or queued), nor while a merge train is open (`admit`);
  * THE SAME SLOTS: with a queue (`max_in_flight` > 0) held PRs take only the slots armed PRs left
    free, lowest PR number first; with none, every eligible held PR, as for armed PRs.

ELIGIBLE (`select`): same-repo, not a draft, not a merge-train branch or rider, not Renovate's
(Renovate owns and rebases its own branch), BEHIND with `mergeable` MERGEABLE, and no check
running (an update would cancel it) or failing (the author's push re-runs it) on its head. A
conflicting PR is skipped, never attempted: the armed pass owns the conflict label and page. A PR
current with the base is counted, not listed. Every other skip is named with its reason.
"""
from __future__ import annotations

from ci_kit import pr_ladder, pr_queue
from ci_kit.merge_train import RIDING, TRAIN_PREFIX

EVENTS = ("schedule", "workflow_dispatch")
_STALE = ("BEHIND", "UNKNOWN")


def _state(p: dict) -> str:
    return (p.get("mergeStateStatus") or "UNKNOWN").upper()


def select(prs: list[dict], required: tuple[str, ...] = ()) -> tuple[list[dict], list[tuple[dict, str]]]:
    """(eligible in landing order, [(pr, why skipped)]) over the open UNARMED PRs."""
    eligible: list[dict] = []
    skipped: list[tuple[dict, str]] = []
    for p in sorted((p for p in prs if not p.get("autoMergeRequest")), key=pr_queue.landing_order):
        merge, mergeable = _state(p), (p.get("mergeable") or "UNKNOWN").upper()
        checks = pr_queue.check_state(p.get("statusCheckRollup"), required)
        why = ("a fork" if p.get("isCrossRepository") else
               "a draft" if p.get("isDraft") else
               "a merge train branch or rider" if str(p.get("headRefName") or "").startswith(TRAIN_PREFIX)
               or any(lb.get("name") == RIDING for lb in p.get("labels") or []) else
               "Renovate's branch (Renovate rebases it)" if pr_ladder.is_renovate(p) else
               "conflicts with the base (not attempted)" if merge == "DIRTY" or mergeable == "CONFLICTING" else
               "" if merge not in _STALE else
               "merge state not computed yet" if merge == "UNKNOWN" or mergeable != "MERGEABLE" else
               "checks still running (an update would cancel them)" if checks == "running" else
               "a check is failing on its head (the author's push re-runs it)" if checks == "failing" else
               "eligible")
        if why == "eligible":
            eligible.append(p)
        elif why:
            skipped.append((p, why))
    return eligible, skipped


def admit(eligible: list[dict], prs: list[dict], q: pr_queue.Plan,
          max_in_flight: int) -> tuple[list[dict], list[tuple[dict, str]]]:
    """(update now, [(pr, why not)]): armed PRs first, then the queue's free slots."""
    trains = [p for p in prs if str(p.get("headRefName") or "").startswith(TRAIN_PREFIX)
              and not p.get("isCrossRepository")]
    if trains:
        return [], [(p, f"merge train #{trains[-1]['number']} is open") for p in eligible]
    if any(_state(p) in _STALE for p in q.update + q.spare):
        return [], [(p, "an armed PR needed an update this pass (armed PRs go first)") for p in eligible]
    if max_in_flight <= 0:
        return list(eligible), []
    slots = max(0, max_in_flight - len(q.in_flight))
    why = f"no free queue slot (in flight: {len(q.in_flight)}/{max_in_flight})"
    return eligible[:slots], [(p, why) for p in eligible[slots:]]
