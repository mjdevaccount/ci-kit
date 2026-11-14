"""Cancel the PR runs that can never be the ones that merge (v1.32.0).

WHY (operator, 2026-11-14: "if its not gonna merge with 100% certainty... DO THE SMART AND
EFFICIENT MOVE"). local-mcp#499 started its full CI at 19:17:48Z; main moved 54s later. Under
STRICT protection ("require branches to be up to date") a PR whose head does not contain main
cannot merge at that head: GitHub needs a new head first -- the queue's update, or a conflict
fix -- and that new head runs every required job again. Every `pull_request` run still going at
the old head is a self-hosted job whose result cannot land anything.

WHICH RUNS, pure (`doomed`). On every pass, for each ARMED PR GitHub reports BEHIND (out of date
with the base) or DIRTY (conflicts with it), on a base whose protection is strict: every queued
or running run with event `pull_request` at that PR's CURRENT head sha. And after the queue
updates a PR, the runs at the head it just replaced (`superseded`): that head can never merge,
strict or not. Left alone:
  * `pull_request_target`, `push`, `schedule`, `workflow_run` runs -- the queue's own machinery;
  * the REVIEW workflow: a review recorded at the old head is carried to the updated one, which
    is why the updater already waits for a running review (`pr_autoupdate.reviewing`);
  * a PR NOT armed: its CI is its author's feedback, and nothing would re-run it after a cancel;
  * a train PR, a PR riding one, a PARKED PR: the merge train and the scheduler own their runs;
  * a CLEAN / BLOCKED / UNSTABLE / UNKNOWN PR: it may still merge at this head (UNKNOWN is
    GitHub not having computed it yet -- never a certainty).

A CANCELLED RUN IS NEVER GREEN. A cancelled required check blocks the merge exactly as a missing
one does; pr_heal reads it as machinery and, the PR being BEHIND, leaves it to the queue's update
(`pr_heal.decide`: "machinery red and behind main"). Nothing here can make a check read passed.

STRICT, READ NOT ASSUMED (`strict`). Classic protection's `required_status_checks.strict`, or a
ruleset's `strict_required_status_checks_policy`. Unreadable (the token lacks Administration:
read) means NOTHING is cancelled on the BEHIND/DIRTY rule, said in the summary and as a
::warning:: naming the grant -- not a red, which the stall watcher would read as a stalled head.

TELEMETRY. Every cancel is a summary line (repo, PR, run id, workflow, reason); a cancel the API
refuses is an ::error:: and a red pass. 409 (the run finished meanwhile) is not a refusal. A 403
("resource not accessible": the push token lacks Actions: Read and write) is configuration, not a
failed call: ONE summary line + ::warning:: naming the grant, and the pass stops trying -- every
caller of the reusable workflow gets this rule, and a repo that never granted it must not go red
on every pass (the stall watcher reads a red streak as a stalled head).
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable

from ci_kit.merge_train import RIDING, TRAIN_PREFIX
from ci_kit.pr_heal import PARKED

Gh = Callable[..., subprocess.CompletedProcess]

ACTIVE = ("queued", "in_progress", "waiting", "pending", "requested")
DOOMED_STATES = frozenset({"BEHIND", "DIRTY"})
EVENT = "pull_request"
GRANT = "Administration: read"
ACTIONS_GRANT = "Actions: Read and write"


class NoGrant(Exception):
    """The push token may not cancel runs in this repo (403)."""


def strict(repo: str, base: str, gh: Gh) -> bool | None:
    """Is `base` protected with "require branches to be up to date"? None = could not tell."""
    r = gh("api", f"repos/{repo}/branches/{base}/protection/required_status_checks", check=False)
    classic: bool | None = None
    if r.returncode == 0:
        try:
            classic = bool(json.loads(r.stdout or "{}").get("strict"))
        except ValueError:
            classic = None
    elif "404" in (r.stderr or "") or "not protected" in (r.stderr or "").lower():
        classic = False  # no classic protection at all (a refused read is 403, never this)
    if classic:
        return True
    rs = gh("api", f"repos/{repo}/rules/branches/{base}", check=False)
    try:
        rules = json.loads(rs.stdout or "[]") if rs.returncode == 0 else None
    except ValueError:
        rules = None
    if rules is not None and any(
            (x.get("type") == "required_status_checks"
             and (x.get("parameters") or {}).get("strict_required_status_checks_policy")) for x in rules):
        return True
    return False if classic is False and rules is not None else None


def doomed(prs: list[dict]) -> list[tuple[dict, str]]:
    """(PR, why) for each armed PR whose current head cannot merge under strict protection. Pure."""
    out = []
    for p in prs:
        state = str(p.get("mergeStateStatus") or "").upper()
        labels = {lb.get("name") for lb in p.get("labels") or []}
        if (state not in DOOMED_STATES or not p.get("autoMergeRequest") or p.get("isCrossRepository")
                or str(p.get("headRefName") or "").startswith(TRAIN_PREFIX) or labels & {RIDING, PARKED}):
            continue
        why = ("behind the base: strict protection needs a new head before it can merge" if state == "BEHIND"
               else "conflicts with the base: only a new head can merge")
        out.append((p, why))
    return out


def cancellable(runs: list[dict], sha: str, review_workflow: str) -> list[dict]:
    """The active `pull_request` runs at `sha`, never the review workflow's. Pure."""
    review = f".github/workflows/{review_workflow}" if review_workflow else None
    return [r for r in runs if r.get("head_sha") == sha and r.get("event") == EVENT
            and r.get("status") in ACTIVE and not (review and str(r.get("path") or "").split("@")[0] == review)]


def cancel_at(repo: str, n: int, sha: str, why: str, review_workflow: str, gh: Gh, summary: list[str]) -> int:
    """Cancel every cancellable run at one head. Returns the errors."""
    r = gh("api", f"repos/{repo}/actions/runs?head_sha={sha}&event={EVENT}&per_page=100", check=False)
    try:
        if r.returncode:
            raise ValueError((r.stderr or "").strip()[:200] or f"exit {r.returncode}")
        runs = json.loads(r.stdout or "{}").get("workflow_runs") or []
    except (ValueError, AttributeError) as e:
        summary.append(f"doomed runs: {repo}#{n}: could not list the runs at {sha[:12]} ({e}); none cancelled")
        print(f"::error::doomed runs: {repo}#{n}: could not list the runs at {sha[:12]}: {e}")
        return 1
    errors = 0
    for run in cancellable(runs, sha, review_workflow):
        c = gh("api", "-X", "POST", f"repos/{repo}/actions/runs/{run['id']}/cancel", check=False)
        wf = run.get("name") or run.get("path") or "?"
        if c.returncode and _refused(c.stderr or ""):
            raise NoGrant((c.stderr or "").strip()[:200])
        if c.returncode and "409" not in (c.stderr or ""):
            errors += 1
            err = (c.stderr or "").strip()[:200]
            summary.append(f"doomed runs: {repo}#{n}: cancel of run {run['id']} ({wf}) REFUSED ({err}) -- "
                           "the token needs Actions: Read and write")
            print(f"::error::doomed runs: {repo}#{n}: cancel of run {run['id']} refused: {err}")
            continue
        done = " (had already finished)" if c.returncode else ""
        summary.append(f"doomed runs: {repo}#{n}: cancelled run {run['id']} ({wf}) at {sha[:12]}{done} -- {why}")
    return errors


def _refused(err: str) -> bool:
    return "resource not accessible" in err.lower() or "403" in err


def no_grant(e: NoGrant, summary: list[str]) -> int:
    summary.append(f"doomed runs: OFF for this pass -- cancel refused ({e}); the push token needs {ACTIONS_GRANT}")
    print(f"::warning::doomed runs: cancel refused; the push token needs {ACTIONS_GRANT}")
    return 0


def sweep(repo: str, prs: list[dict], base: str, review_workflow: str, gh: Gh, summary: list[str]) -> int:
    """The BEHIND/DIRTY rule over every open PR. Returns the errors."""
    found = doomed(prs)
    if not found:
        return 0
    is_strict = strict(repo, base, gh)
    if is_strict is None:
        summary.append(f"doomed runs: NOT cancelled on {', '.join('#' + str(p['number']) for p, _ in found)} -- "
                       f"could not read whether {base} is strict (the token needs {GRANT})")
        print(f"::warning::doomed runs: cannot read {base}'s protection; the token needs {GRANT}")
        return 0
    if not is_strict:
        return 0
    try:
        return sum(cancel_at(repo, int(p["number"]), str(p.get("headRefOid") or ""), why, review_workflow, gh,
                             summary) for p, why in found if p.get("headRefOid"))
    except NoGrant as e:
        return no_grant(e, summary)


def superseded(repo: str, p: dict, review_workflow: str, gh: Gh, summary: list[str]) -> int:
    """The queue just updated this PR: its old head can never merge. Returns the errors."""
    sha = str(p.get("headRefOid") or "")
    if not sha:
        return 0
    try:
        return cancel_at(repo, int(p["number"]), sha, "the queue updated this PR: that head can never merge",
                         review_workflow, gh, summary)
    except NoGrant as e:
        return no_grant(e, summary)
