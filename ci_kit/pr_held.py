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
    arming, which fire on every merge and every check completion -- and LAST in that pass, after
    everything the armed pass does (pr_autoupdate.run);
  * ARMED FIRST: no held PR is touched while ANY armed PR is not yet current with the base
    (BEHIND or UNKNOWN, wherever the queue put it: to update, spare, or held for its running or
    failing checks), nor while a merge train is open (`admit`);
  * THE SAME SLOTS: with a queue (`max_in_flight` > 0) held PRs take only the slots left free by
    the armed PRs in flight AND by held PRs whose own checks are running (`running`: typically
    one this pass updated earlier), lowest PR number first; with none, every eligible held PR.
    The armed queue never counts held PRs: it never waits on one.

ELIGIBLE (`select`): same-repo, not a draft, not a merge-train branch or rider, not a bot's
(`bot`: any Bot account or `[bot]` login, Renovate included -- a bot owns and rebases its own
branch, and stops doing so once someone else pushes to it), BEHIND with `mergeable` MERGEABLE, a
head commit older than YOUNG_S (younger, or of unreadable age: someone may still be pushing), and
no check of ANY name running (an update would cancel it) or failing (the author's push re-runs
it) on its head -- `required-checks` narrows the armed queue, not this. A conflicting PR is
skipped, never attempted. A PR current with the base is counted, not listed. Every other skip is
named with its reason.
"""
from __future__ import annotations

from datetime import datetime

from ci_kit import pr_ladder, pr_queue
from ci_kit.merge_train import RIDING, TRAIN_PREFIX

EVENTS = ("schedule", "workflow_dispatch")
YOUNG_S = 600  # a head younger than this is skipped: a push is likely still in progress
_STALE = ("BEHIND", "UNKNOWN")


def _state(p: dict) -> str:
    return (p.get("mergeStateStatus") or "UNKNOWN").upper()


def bot(p: dict) -> str:
    """The bot's login when this PR is a bot's ('' = a person's). Pure."""
    a = p.get("author") or {}
    login = str(a.get("login") or "")
    if a.get("__typename") == "Bot" or login.endswith("[bot]") or pr_ladder.is_renovate(p):
        return login or "renovate"
    return ""


def checks(p: dict) -> tuple[list[str], list[str]]:
    """(failing, running) check names on the head -- EVERY check, whatever `required-checks` says.
    Each check is read by pr_queue.check_state, so one check means what it means to the queue (the
    queue hold's own `queue/position` status is order, not a check, there too). Pure."""
    failing: list[str] = []
    running: list[str] = []
    for c in p.get("statusCheckRollup") or []:
        state = pr_queue.check_state([c])
        name = str(c.get("name") or c.get("context") or "?")
        (failing if state == "failing" else running if state == "running" else []).append(name)
    return failing, running


def _scope(p: dict) -> str:
    """Why this unarmed PR is never a held-pass PR at all ('' = it is one). Pure."""
    return ("a fork" if p.get("isCrossRepository") else
            "a draft" if p.get("isDraft") else
            "a merge train branch or rider" if str(p.get("headRefName") or "").startswith(TRAIN_PREFIX)
            or any(lb.get("name") == RIDING for lb in p.get("labels") or []) else
            f"a bot's PR ({bot(p)})" if bot(p) else "")


def _age(p: dict, now: float) -> float | None:
    try:
        return now - datetime.fromisoformat(str(p.get("headCommittedDate")).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _why(p: dict, now: float) -> str:
    merge, mergeable = _state(p), (p.get("mergeable") or "UNKNOWN").upper()
    if out := _scope(p):
        return out
    if merge == "DIRTY" or mergeable == "CONFLICTING":
        return "conflicts with the base (not attempted)"
    if merge not in _STALE:
        return ""
    if merge == "UNKNOWN" or mergeable != "MERGEABLE":
        return "merge state not computed yet"
    age = _age(p, now)
    if age is None:
        return "head commit date unreadable"
    if age < YOUNG_S:
        return f"head commit is {max(0, int(age // 60))} min old (someone may be pushing; waits {YOUNG_S // 60} min)"
    failing, running = checks(p)
    if failing:
        return f"check `{failing[0]}` is failing on its head (the author's push re-runs it)"
    if running:
        return f"check `{running[0]}` still running (an update would cancel it)"
    return "eligible"


def select(prs: list[dict], now: float) -> tuple[list[dict], list[tuple[dict, str]]]:
    """(eligible in landing order, [(pr, why skipped)]) over the open UNARMED PRs."""
    eligible: list[dict] = []
    skipped: list[tuple[dict, str]] = []
    for p in sorted((p for p in prs if not p.get("autoMergeRequest")), key=pr_queue.landing_order):
        why = _why(p, now)
        if why == "eligible":
            eligible.append(p)
        elif why:
            skipped.append((p, why))
    return eligible, skipped


def running(prs: list[dict]) -> list[dict]:
    """The held-pass PRs whose own checks are running now: each holds a slot of the held budget."""
    return [p for p in prs if not p.get("autoMergeRequest") and not _scope(p) and checks(p)[1]]


def admit(eligible: list[dict], prs: list[dict], q: pr_queue.Plan,
          max_in_flight: int) -> tuple[list[dict], list[tuple[dict, str]]]:
    """(update now, [(pr, why not)]): every armed PR current first, then the free slots."""
    trains = [p for p in prs if str(p.get("headRefName") or "").startswith(TRAIN_PREFIX)
              and not p.get("isCrossRepository")]
    if trains:
        return [], [(p, f"merge train #{trains[-1]['number']} is open") for p in eligible]
    if any(_state(p) in _STALE for p in q.update + q.spare + [p for p, _ in q.held]):
        return [], [(p, "an armed PR needed an update this pass (armed PRs go first)") for p in eligible]
    if max_in_flight <= 0:
        return list(eligible), []
    held = len(running(prs))
    slots = max(0, max_in_flight - len(q.in_flight) - held)
    why = f"no free queue slot (in flight: {len(q.in_flight)} armed + {held} held of {max_in_flight})"
    return eligible[:slots], [(p, why) for p in eligible[slots:]]
