"""Cancel the PR runs that can never be the ones that merge -- and whose result nothing reads (v1.32.0).

WHY (operator, 2026-11-14: "if its not gonna merge with 100% certainty... DO THE SMART AND
EFFICIENT MOVE"). local-mcp#499 started its full CI at 19:17:48Z; main moved 54s later. Under
STRICT protection ("require branches to be up to date") a PR whose head does not contain main
cannot merge at that head: GitHub needs a new head first -- the queue's update, or a conflict
fix -- and that new head runs every required job again.

THE BAR IS TWO-SIDED: the run cannot merge, AND nothing else reads its result. Three cases pass it.
  * SUPERSEDED (`superseded`, strict or not): the queue just updated the PR, so its old head is
    no PR's head any more. Most callers' CI already cancels this itself (a `concurrency` group per
    PR ref with cancel-in-progress); this catches the PR workflows that do not.
  * DIRTY (strict base): a conflicting head can never merge, and the merge train never takes a
    conflicting PR (merge_train.policy: "not conflicting").
  * BEHIND (strict base) ONLY WHERE NO MERGE TRAIN RUNS. A train takes a BEHIND PR whose required
    checks are green at its head -- "being behind main is exactly what the train fixes" -- so a
    BEHIND head's run is how a PR becomes a rider (session review of this module, finding 1: a
    cancel there leaves the train `checks pending` forever on a busy main). A repo has a train
    when the caller set `train-workflow`, or any of its workflows is named/pathed `merge-train`;
    a workflow list that cannot be read counts as a train (skip, never guess). BEHIND is then
    re-proven as a GIT fact before any cancel: `compare/{base}...{head}` must say `behind_by > 0`
    (mergeStateStatus is GitHub's cached view, read at the start of the pass).
Never touched: `pull_request_target` / `push` / `schedule` / `workflow_run` runs (the queue's own
machinery); the REVIEW workflow (a review recorded at the old head is carried to the updated one --
`pr_autoupdate.reviewing`); an unarmed PR (its CI is the author's feedback, nothing re-runs it);
a train PR, a rider, a PARKED PR (the train and the scheduler own them); CLEAN / BLOCKED /
UNSTABLE / UNKNOWN (UNKNOWN is GitHub not having computed it yet -- never a certainty).

A CANCELLED RUN IS NEVER GREEN. A cancelled required check blocks the merge like a missing one;
pr_heal reads it as machinery and, the PR being BEHIND, leaves it to the queue's update. It does
read as a FAILURE rollup to an outside watcher: mcp pr_reconcile learns the same rule (an armed
BEHIND PR whose only red is CANCELLED is waiting for the queue, not failing).

STRICT, READ NOT ASSUMED (`strict`): classic protection's `required_status_checks.strict`, or a
ruleset's `strict_required_status_checks_policy`. Unreadable (the push token lacks
Administration: read) means the DIRTY/BEHIND rule cancels nothing, said once per pass in the
summary and as a ::warning:: naming the grant -- not a red, which the stall watcher would read as
a stalled head.

TELEMETRY. Every cancel is a summary line (repo, PR, run id, workflow, reason); a cancel or a
read the API refuses is an ::error:: and a red pass; 409 (the run finished meanwhile) is not a
refusal. A 403 "resource not accessible" (no Actions: Read and write) is configuration: ONE
summary line + ::warning::, and the pass stops cancelling -- every caller of the reusable workflow
gets this rule, and one that never granted it must not go red on every pass.
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable

from ci_kit.merge_train import RIDING, TRAIN_PREFIX
from ci_kit.pr_heal import PARKED

Gh = Callable[..., subprocess.CompletedProcess]

ACTIVE = ("queued", "in_progress", "waiting", "pending", "requested")
EVENT = "pull_request"
GRANT = "Administration: read"
ACTIONS_GRANT = "Actions: Read and write"
TRAIN_MARK = "merge-train"


def _json(r: subprocess.CompletedProcess, default: object) -> object:
    if r.returncode:
        return None
    try:
        return json.loads(r.stdout or "") if (r.stdout or "").strip() else default
    except ValueError:
        return None


def strict(repo: str, base: str, gh: Gh) -> bool | None:
    """Is `base` protected with "require branches to be up to date"? None = could not tell."""
    r = gh("api", f"repos/{repo}/branches/{base}/protection/required_status_checks", check=False)
    classic: bool | None = None
    if r.returncode == 0:
        got = _json(r, {})
        classic = bool(got.get("strict")) if isinstance(got, dict) else None
    elif "404" in (r.stderr or "") or "not protected" in (r.stderr or "").lower():
        classic = False  # no classic protection at all (a refused read is 403, never this)
    if classic:
        return True
    rules = _json(gh("api", f"repos/{repo}/rules/branches/{base}", check=False), [])
    if isinstance(rules, list) and any(
            x.get("type") == "required_status_checks"
            and (x.get("parameters") or {}).get("strict_required_status_checks_policy") for x in rules):
        return True
    return False if classic is False and isinstance(rules, list) else None


def has_train(repo: str, gh: Gh, summary: list[str] | None = None) -> bool:
    """Does a merge train run here? Unreadable = yes (the BEHIND rule then never fires, said)."""
    got = _json(gh("api", f"repos/{repo}/actions/workflows?per_page=100", check=False), {})
    if not isinstance(got, dict):
        if summary is not None:
            summary.append("doomed runs: BEHIND rule skipped -- could not list the workflows to rule out a merge train")
        return True
    return any(TRAIN_MARK in str(w.get("name") or "").lower() or TRAIN_MARK in str(w.get("path") or "").lower()
               for w in got.get("workflows") or [])


def doomed(prs: list[dict], behind: bool, base: str | None = None) -> list[tuple[dict, str]]:
    """(PR, why) for each armed PR whose current head cannot merge on a strict base: DIRTY always,
    BEHIND only when `behind` (no merge train reads it). Pure."""
    states = {"DIRTY", "BEHIND"} if behind else {"DIRTY"}
    out = []
    for p in prs:
        state = str(p.get("mergeStateStatus") or "").upper()
        labels = {lb.get("name") for lb in p.get("labels") or []}
        if (state not in states or not p.get("autoMergeRequest") or p.get("isCrossRepository")
                or (base and p.get("baseRefName") and p.get("baseRefName") != base)  # stacked: judged elsewhere
                or not p.get("headRefOid")
                or str(p.get("headRefName") or "").startswith(TRAIN_PREFIX) or labels & {RIDING, PARKED}):
            continue
        why = ("behind the base: strict protection needs a new head before it can merge" if state == "BEHIND"
               else "conflicts with the base: only a new head can merge")
        out.append((p, why))
    return out


def cancellable(runs: list[dict], sha: str, n: int, review_workflow: str) -> list[dict]:
    """The active `pull_request` runs at `sha` for PR `n`, never the review workflow's. Pure. A run
    that names its PRs must name this one (GitHub leaves `pull_requests` empty at times)."""
    review = f".github/workflows/{review_workflow}" if review_workflow else None
    return [r for r in runs if r.get("head_sha") == sha and r.get("event") == EVENT
            and r.get("status") in ACTIVE and not (review and str(r.get("path") or "").split("@")[0] == review)
            and (not r.get("pull_requests") or any(x.get("number") == n for x in r["pull_requests"]))]


def _refused(err: str) -> bool:
    """A missing grant -- never a rate limit, which GitHub also answers 403."""
    low = err.lower()
    return "resource not accessible" in low and "rate limit" not in low


class Canceller:
    """One pass's cancels. Holds the pass's one 'no grant' answer, so a refusal is said once."""

    def __init__(self, repo: str, review_workflow: str, gh: Gh, summary: list[str]) -> None:
        self.repo, self.review, self.gh, self.summary = repo, review_workflow, gh, summary
        self.off = ""
        self.errors = 0
        self.restarted: set[int] = set()  # BEHIND PRs whose running checks the sweep cancelled
        self.done: set[int] = set()  # run ids cancelled this pass: the replaced-head cancel never repeats one

    def _error(self, line: str) -> None:
        self.errors += 1
        self.summary.append(f"doomed runs: {line}")
        print(f"::error::doomed runs: {line}")

    def cancel_at(self, n: int, sha: str, why: str) -> bool:
        """True when at least one run at `sha` was cancelled."""
        if self.off:
            return False
        r = self.gh("api", f"repos/{self.repo}/actions/runs?head_sha={sha}&event={EVENT}&per_page=100", check=False)
        got = _json(r, {})
        if not isinstance(got, dict):
            self._error(f"{self.repo}#{n}: could not list the runs at {sha[:12]} "
                        f"({(r.stderr or '').strip()[:200]}); none cancelled")
            return False
        hit = False
        for run in [r for r in cancellable(got.get("workflow_runs") or [], sha, n, self.review)
                    if r.get("id") not in self.done]:
            c = self.gh("api", "-X", "POST", f"repos/{self.repo}/actions/runs/{run['id']}/cancel", check=False)
            wf, err = run.get("name") or run.get("path") or "?", (c.stderr or "").strip()[:200]
            if c.returncode and _refused(err):
                self.off = err
                self.summary.append(f"doomed runs: OFF for this pass -- cancel refused ({err}); the push token "
                                    f"needs {ACTIONS_GRANT}")
                print(f"::warning::doomed runs: cancel refused; the push token needs {ACTIONS_GRANT}")
                return hit
            if c.returncode and "409" not in err:
                self._error(f"{self.repo}#{n}: cancel of run {run['id']} ({wf}) REFUSED ({err})")
                continue
            done = " (had already finished)" if c.returncode else ""
            hit = True
            self.done.add(run.get("id"))
            self.summary.append(f"doomed runs: {self.repo}#{n}: cancelled run {run['id']} ({wf}) at {sha[:12]}"
                                f"{done} -- {why}")
        return hit

    def _behind(self, base: str, sha: str) -> bool | None:
        got = _json(self.gh("api", f"repos/{self.repo}/compare/{base}...{sha}?per_page=1", check=False), {})
        return int(got.get("behind_by") or 0) > 0 if isinstance(got, dict) else None

    def sweep(self, prs: list[dict], base: str, train: bool) -> None:
        """The DIRTY / BEHIND rule over every open PR."""
        found = doomed(prs, behind=True, base=base)
        if any(str(p.get("mergeStateStatus") or "").upper() == "BEHIND" for p, _ in found) and (
                train or has_train(self.repo, self.gh, self.summary)):
            found = doomed(prs, behind=False, base=base)  # a merge train reads BEHIND heads' checks
        if not found:
            return
        is_strict = strict(self.repo, base, self.gh)
        if is_strict is None:
            self.summary.append(f"doomed runs: NOT cancelled on {', '.join('#' + str(p['number']) for p, _ in found)}"
                                f" -- could not read whether {base} is strict (the token needs {GRANT})")
            print(f"::warning::doomed runs: cannot read {base}'s protection; the token needs {GRANT}")
            return
        if not is_strict:
            return
        for p, why in found:
            n, sha = int(p["number"]), str(p["headRefOid"])
            if self.off:
                break
            if str(p.get("mergeStateStatus") or "").upper() == "BEHIND":
                behind = self._behind(base, sha)
                if behind is None:
                    self._error(f"{self.repo}#{n}: could not compare {sha[:12]} with {base}; nothing cancelled")
                    continue
                if not behind:
                    self.summary.append(f"doomed runs: {self.repo}#{n}: GitHub said BEHIND but {sha[:12]} already "
                                        f"contains {base}; nothing cancelled")
                    continue
            if self.cancel_at(n, sha, why) and str(p.get("mergeStateStatus") or "").upper() == "BEHIND":
                self.restarted.add(n)

    def superseded(self, p: dict) -> None:
        """The queue just updated this PR: its old head is no PR's head any more."""
        if p.get("headRefOid"):
            self.cancel_at(int(p["number"]), str(p["headRefOid"]), "the queue updated this PR: that head can never merge")
