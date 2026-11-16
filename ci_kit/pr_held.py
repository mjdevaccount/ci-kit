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
  * ARMED FIRST (`armed_first`): no held PR is touched while an armed PR is still on its way to
    current -- one the queue PICKED to update this pass, one BEHIND that it holds only because its
    own checks are running, or one whose merge state is not computed (UNKNOWN) -- nor while a merge
    train is open (`admit`). An armed PR held for a FAILING check blocks nothing: nothing updates it
    until its author pushes, so waiting on it would stop the held pass indefinitely. The skip line
    names each blocking PR and its own reason;
  * THE SAME SLOTS: with a queue (`max_in_flight` > 0) held PRs take only the slots left free by
    the armed PRs in flight AND by held PRs whose own checks are running (`running`: typically
    one this pass updated earlier), lowest PR number first; with none, every eligible held PR.
    The "no free queue slot" line names every slot holder. The armed queue never counts held
    PRs: it never waits on one.

ELIGIBLE (`select`): same-repo, not a draft, not a merge-train branch or rider, not a bot's
(`bot`: any Bot account or `[bot]` login, Renovate included -- a bot owns and rebases its own
branch, and stops doing so once someone else pushes to it), BEHIND with `mergeable` MERGEABLE, a
head commit older than YOUNG_S (younger, or of unreadable age: someone may still be pushing), and
no check of ANY name running (an update would cancel it) or failing (the author's push re-runs
it) on its head -- `required-checks` narrows the armed queue, not this. A conflicting PR is
skipped, never attempted. A PR current with the base is neither listed nor counted (nothing to
do), and a PR out of scope (fork, draft, train, bot) never holds a slot, whatever runs on it.
Every other skip is named with its reason.
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
    """The held-pass PRs whose own checks are running now: each holds a slot of the held budget.
    An out-of-scope PR (`_scope`: fork, draft, train, bot) holds none, whatever runs on it."""
    return [p for p in prs if not p.get("autoMergeRequest") and not _scope(p) and checks(p)[1]]


def armed_first(q: pr_queue.Plan, required: tuple[str, ...] = ()) -> list[tuple[dict, str]]:
    """[(armed PR, why the held pass waits for it)] in landing order. Pure. Only an armed PR still
    on its way to current counts: UNKNOWN (not computed), PICKED by the queue this pass (`update`),
    or BEHIND and held only for its own running checks (the queue's own `required` reading). One
    held for a failing check, queued past the free slots, in flight or conflicting does not."""
    picked = {p["number"] for p in q.update}
    seen: dict[int, dict] = {}
    for p in q.update + q.spare + [p for p, _ in q.held] + q.in_flight:
        seen.setdefault(p["number"], p)
    out: list[tuple[dict, str]] = []
    for p in sorted(seen.values(), key=pr_queue.landing_order):
        if _state(p) == "UNKNOWN":
            out.append((p, "merge state not computed yet"))
        elif _state(p) != "BEHIND":
            continue
        elif p["number"] in picked:
            out.append((p, "picked by the queue this pass"))
        elif pr_queue.check_state(p.get("statusCheckRollup"), required) == "running":
            out.append((p, "behind, its checks are running"))
    return out


def _nums(prs: list[dict]) -> str:
    return ", ".join(f"#{p['number']}" for p in prs) or "none"


def admit(eligible: list[dict], prs: list[dict], q: pr_queue.Plan, max_in_flight: int,
          required: tuple[str, ...] = ()) -> tuple[list[dict], list[tuple[dict, str]]]:
    """(update now, [(pr, why not)]): the armed PRs on their way to current first, then the free slots."""
    trains = [p for p in prs if str(p.get("headRefName") or "").startswith(TRAIN_PREFIX)
              and not p.get("isCrossRepository")]
    if trains:
        return [], [(p, f"merge train #{trains[-1]['number']} is open") for p in eligible]
    if first := armed_first(q, required):
        why = "armed PRs go first: " + ", ".join(f"#{p['number']} ({w})" for p, w in first)
        return [], [(p, why) for p in eligible]
    if max_in_flight <= 0:
        return list(eligible), []
    holders = running(prs)
    slots = max(0, max_in_flight - len(q.in_flight) - len(holders))
    todo = eligible[:slots]
    taken = [f"{what} {_nums(group)}" for what, group in (("armed in flight", q.in_flight),
             ("held with checks running", holders), ("held, picked this pass", todo)) if group]
    why = (f"no free queue slot ({len(q.in_flight) + len(holders) + len(todo)} of {max_in_flight} taken: "
           f"{'; '.join(taken) or 'none'})")
    return todo, [(p, why) for p in eligible[slots:]]
