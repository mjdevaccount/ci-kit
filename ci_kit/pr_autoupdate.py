#!/usr/bin/env python3
"""Merge main into every open PR that has auto-merge ARMED; label + page the ones that conflict.

WHY. Many agent sessions open PRs at once. With "require branches up to date" OFF, two PRs each
green against an old main can merge and break main together (a flag renamed in one, read by its
old name in the other). With it ON and nothing updating branches, every armed PR stalls BEHIND.
This is the updater that makes strict affordable: each push to main re-bases (by merge) every
armed PR, so CI re-runs against the new main and a semantic conflict goes RED instead of shipping.

WHY NOT tibdex/auto-update. Checked 2026-09-26: last push 2024-02, last release 2022-11,
`runs.using: node16` -- unmaintained, on a runtime GitHub has retired, and it only logs a conflict
(no label, no output a later step could page on). This script is the same one API call
(`PUT /pulls/{n}/update-branch`, GitHub's own "Update branch" button) through the `gh` CLI, plus
the conflict handling the action never had.

THE TOKEN MUST NOT BE GITHUB_TOKEN. A push made with the workflow's own token fires no workflow,
so the updated head would get no checks and auto-merge would wait forever -- a stall that looks
like "CI pending". The workflow passes a fine-grained PAT (contents + pull requests + workflows RW,
actions read; workflows because an update that brings in a main commit touching
.github/workflows/ is refused without it) as `GH_TOKEN`.

OPTIONS. `--review-workflow` (skip a PR whose review gate is running; empty = the repo has none),
`--tracker` (the issue whose row a page quotes; 0 = none), `--relock` (the caller relocks
lockfile-only conflicts, so the page says so), `--label` (the conflict label; `conflict`), and
`--max-in-flight N` + `--required-checks a,b` (a serial queue: at most N armed PRs tested against
current main at once, lowest PR number first; 0 = every armed PR. The policy and the measurement
behind it: ci_kit/pr_queue.py). With a queue, every pass first ARMS each Renovate PR whose preset
verdict is automerge (ci_kit/pr_renovate.py, v1.32.0): Renovate never arms or merges in a queue
repo -- and, with the queue IDLE, MERGES the lowest CLEAN one (every check green), which GitHub refuses
to arm (v1.60.0, pr_renovate.land_idle). A PR the queue picked but skipped for a running review hands its slot to the next eligible PR
(`Plan.spare`), so one long review does not idle the queue. `update_held` (env CI_KIT_UPDATE_HELD,
default off) also updates HELD PRs -- open, not armed -- on a scheduled or dispatched pass only,
LAST (after this pass's outputs and summary are written), once no armed PR is on its way to
current, inside the queue's free slots; it never labels, pages or
relocks one, and nothing it raises changes this run's exit code (ci_kit/pr_held.py).

CONFLICT = TRANSITION-ONLY PAGE. The conflict label is the dedup state: a PR that conflicts and
is not labelled gets the label AND one ntfy push (env `NTFY_URL`; unset = no push) naming the PR
and its tracker row; one already labelled gets neither again. A PR that updates cleanly loses the
label, and so does ANY open PR carrying it that GitHub now reports MERGEABLE (stale_labels): a
conflict a session resolved leaves the PR current with main, so it is never attempted again.
Each NEWLY labelled PR is emitted as output `relock` for a caller that can relock a lockfile-only
conflict. Not a `labeled` trigger: GitHub runs no `pull_request` workflow on a PR
with a merge conflict, so that trigger could never fire. A repo with no such label refuses the
add, so a failed add looks the label up, CREATES it when missing (a `::notice::` names repo and
label: the create is itself an event) and retries the add ONCE. Only a second failure is an
error, with NO page (the label is the dedup; without it every merge to main would page again).

Exit 0 = every armed PR handled; 1 = at least one update failed for a reason other than a
conflict (named in the log, red run); 2 = could not list the PRs at all (did not finish looking).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass

from ci_kit import doomed_runs, pr_baseretry, pr_heal, pr_held, pr_hold, pr_ladder, pr_list, pr_queue, pr_renovate, pr_sched, train_kick
from ci_kit.merge_train import EJECTED, NEEDS_UPDATE, RIDING, TRAIN_PREFIX
from ci_kit.ntfy import page  # noqa: F401 -- re-exported: _update_one pages through this name

LABEL = "conflict"

HEAD_WAIT_S = 240.0  # the tail's wait for the head's verdict: one verdict job on a pool with a 200 s p90 wait
HEAD_POLL_S = 15.0  # between two reads of that review
HEAD_WAIT_MAX_S = 300.0  # the wait's ceiling: the job is timeout-minutes 10 and the pass itself has taken 4.5


@dataclass(frozen=True)
class Options:
    review_workflow: str = ""  # a workflow file whose queued/running run defers the update
    tracker: int = 0  # the issue whose row for a PR is quoted in the page; 0 = none
    relock: bool = False  # the caller relocks lockfile-only conflicts
    label: str = LABEL  # the conflict label (the page dedup)
    max_in_flight: int = 0  # PRs tested against current main at once (pr_queue); 0 = update every armed PR
    required: tuple[str, ...] = ()  # the check names that count for pr_queue; () = every check
    rerun_limit: int = pr_heal.LIMIT  # machinery reruns per head (pr_heal); 0 = no smart auto-merge
    # the PR scheduler (ci_kit/pr_sched.py): scarce pool label -> capacity; {} = off
    scarce: tuple[tuple[str, int], ...] = ()
    preempt_after_min: float = 3.0
    head_stall_min: float = 15.0
    before: str = ""  # a push to main's previous sha (the out-of-queue merge check)
    sha: str = ""
    hold: bool = False  # the queue hold: required status `queue/position`, success on the head only (pr_hold)
    # the caller's merge-train workflow file, dispatched when this pass sees work for it (train_kick); '' = off
    train_workflow: str = ""
    train_min_batch: int = train_kick.MIN_BATCH
    # the merge train's own `stale-minutes`: an open train holds the queue head at most this long (pr_hold)
    train_stale_min: float = 90.0
    # cancel the `pull_request` runs at heads that cannot merge under strict protection (doomed_runs)
    cancel_doomed: bool = False  # main() turns it ON (env CI_KIT_CANCEL_DOOMED, default true)
    ladder: pr_ladder.Config = pr_ladder.Config()  # the heal ladder (pr_ladder): off unless the caller runs pr-heal.yml
    # consecutive base-driven retries of a red BEHIND PR main has gone green on (pr_baseretry); 0 = off.
    # main() turns it ON (env CI_KIT_BASE_RETRY_CAP, default pr_baseretry.CAP)
    base_retry_cap: int = 0
    # the NO-AUTHOR case (pr_baseretry.bot_reason): exact author logins, empty = off. Shares the cap above.
    base_retry_bots: tuple[str, ...] = ()
    base_retry_per_pass: int = pr_baseretry.PER_PASS  # candidates whose main runs are read per pass
    base_retry_error: str = ""  # an unreadable cap: the feature is refused, the pass goes on, the run is red
    # keep HELD (unarmed) PRs current too, on a schedule/workflow_dispatch pass only (ci_kit/pr_held.py)
    update_held: bool = False
    event: str = ""  # the caller's github.event_name
    # HEAD FAIL-FAST (v1.45.0): a BEHIND queue head whose review is still YOUNG is updated at once
    # (pr_sched.review_young), not when the verdict records. main() turns it ON (env CI_KIT_HEAD_FAIL_FAST)
    head_fail_fast: bool = False
    # THE HEAD'S VERDICT IS WAITED FOR (v1.45.0): seconds the pass's tail waits for a head past that
    # bound to record its verdict, then updates it (`_await_head`); 0 = off. main() turns it ON
    # (env CI_KIT_HEAD_VERDICT_WAIT_S, default HEAD_WAIT_S) with head fail-fast
    head_wait_s: float = 0.0


@dataclass(frozen=True)
class Outcome:
    kind: str  # updated | current | conflict | error
    detail: str = ""


def classify(returncode: int, body: str) -> Outcome:
    """Read `gh api -X PUT .../update-branch`'s exit code and response body."""
    if returncode == 0:
        return Outcome("updated")
    try:
        msg = str(json.loads(body).get("message", ""))
    except (ValueError, AttributeError):
        msg = body.strip()[:300]
    low = msg.lower()
    if "conflict" in low:
        return Outcome("conflict", msg)
    if "no new commits" in low or "up to date" in low or "up-to-date" in low:
        return Outcome("current", msg)
    # a moved head (expected_head_sha mismatch) is a race with the author's push: the author's
    # push already re-runs CI, so it is not a failure of this run
    if "expected head sha" in low or "head sha" in low:
        return Outcome("current", f"head moved during update: {msg}")
    return Outcome("error", msg or f"exit {returncode}")


def actions_for(outcome: Outcome, labelled: bool) -> tuple[bool, bool, bool]:
    """(add_label, page, remove_label). Pure: the policy the tests pin."""
    if outcome.kind == "conflict":
        return (not labelled, not labelled, False)
    if outcome.kind in ("updated", "current"):
        return (False, False, labelled)
    return (False, False, False)


def reviewing(repo: str, branch: str, workflow: str) -> bool:
    """A review-gate run for this branch is queued or running.

    Updating now would push a new head, and the gate's `cancel-in-progress` would kill a full
    review that has recorded nothing yet -- so the next run could not carry either, and a PR
    under a busy main would never get a verdict. Skip it; the workflow re-runs this updater when
    a review-gate run completes (`workflow_run`).

    A repo with no review gate passes an empty `workflow`: asking `gh run list` about a workflow
    file that does not exist is an error, which would turn every run into exit 2.
    """
    if not workflow:
        return False
    for status in ("in_progress", "queued"):
        r = _gh("run", "list", "-R", repo, "--workflow", workflow, "--branch", branch,
                "--status", status, "--limit", "1", "--json", "databaseId", check=False)
        if r.returncode:
            raise subprocess.CalledProcessError(r.returncode, "gh run list", r.stdout, r.stderr)
        if json.loads(r.stdout or "[]"):
            return True
    return False


def tracker_row(body: str, pr: int) -> str:
    for line in body.splitlines():
        if f"#{pr}" in line and not line.lstrip().startswith("#"):
            return " ".join(line.split())[:200]
    return ""


def page_text(repo: str, pr: dict, row: str, opts: Options) -> tuple[str, str]:
    """(title, body) of the one conflict page. Pure: the wording the tests pin."""
    nl = chr(10)
    body = f"{pr['title']}{nl}Conflicts with main; auto-merge is stalled until it is resolved."
    if opts.relock:
        body += " A lockfile-only conflict is relocked automatically."
    if opts.tracker:
        body += nl + (f"Tracker #{opts.tracker}: {row}" if row else f"No row in tracker #{opts.tracker}.")
    return f"{repo.rsplit('/', 1)[-1]} #{pr['number']}: conflicts with main", body


def _gh(*args: str, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    # utf-8, never the locale: a Renovate body's emoji in cp1252 made its verdict unreadable (review 2026-11-14)
    return subprocess.run(["gh", *args], check=check, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)


READ_TOKEN = "CI_KIT_READ_TOKEN"


def _gh_read(*args: str) -> subprocess.CompletedProcess[str]:
    """A read-only gh call, made with CI_KIT_READ_TOKEN (the job's GITHUB_TOKEN) when set.

    The push token is a fine-grained PAT, and GitHub refuses a fine-grained PAT the check runs
    inside `statusCheckRollup` ("Resource not accessible by personal access token", live
    2026-10-24 on every repo with an open PR once v1.6.0 asked for it). GITHUB_TOKEN with
    `checks: read` + `statuses: read` can read them. Falls back to the push token when the read
    token is unset or refused, so a caller that has not granted those permissions fails exactly
    as before rather than differently.
    """
    read = os.environ.get(READ_TOKEN, "")
    first = ""
    if read:
        r = _gh(*args, check=False, env={**os.environ, "GH_TOKEN": read})
        if r.returncode == 0:
            return r
        first = (r.stderr or "").strip()
    r = _gh(*args, check=False)
    if r.returncode:
        # name BOTH refusals: the fallback's alone hid why the read token failed (2026-10-24)
        err = (r.stderr or "").strip() + (f" | read token said: {first[-300:]}" if first else "")
        raise subprocess.CalledProcessError(r.returncode, ["gh", *args[:2]], r.stdout, err)
    return r


LABEL_COLOR = "d93f0b"
LABEL_DESCRIPTION = "pr-autoupdate: conflicts with main (dedup state for the page)"


def add_label(repo: str, n: int, label: str, color: str = LABEL_COLOR,
              description: str = LABEL_DESCRIPTION) -> tuple[bool, bool]:
    """(added, created). A failed add creates the label when the repo lacks it, then retries ONCE.

    Live 2026-10-30: a consumer had no `conflict` label, so every add failed and the run went red
    six times in a row while armed PRs sat behind main; ten more repos had the same hole.
    """
    edit = ("pr", "edit", str(n), "-R", repo, "--add-label", label)
    if _gh(*edit, check=False).returncode == 0:
        return True, False
    created = False
    if _gh("api", f"repos/{repo}/labels/{urllib.parse.quote(label, safe='')}", check=False).returncode:
        if _gh("label", "create", label, "-R", repo, "--color", color,
               "--description", description, check=False).returncode:
            return False, False
        created = True
        print(f"::notice::{repo}: created the missing `{label}` label (pr-autoupdate's conflict dedup)")
    return _gh(*edit, check=False).returncode == 0, created


def _train_plan(prs: list[dict], armed: list[dict], summary: list[str], opts: Options,
                machinery: frozenset[int] = frozenset(), restarted: frozenset[int] = frozenset(),
                base_retry: frozenset[int] = frozenset()) -> pr_queue.Plan:
    """pr_queue's plan with the MERGE TRAIN's PRs taken out (ci_kit/merge_train). A train PR is
    never updated (a merge commit from main would break its one-commit-per-PR chain; a stale train
    is recomposed instead), a PR riding one is left alone, and while a train is open nothing else is
    updated either -- a single PR landing beside it would put the whole train BEHIND and throw its
    CI away. A DIRTY PR is still attempted: that PUT pushes nothing, and it is how the conflict
    label and page fire. An EJECTED PR is updated anyway: it rides no train, and its own update is
    the only lane it has left -- holding it while trains keep forming would starve it (session
    review of ci-kit#70, finding 7). So is a PR a train SKIPPED because the lines around its change
    moved since its review (`train-needs-update`): no train takes it until its own update and
    review, which this hold would otherwise keep from ever happening (round 2, finding 3). A repo
    with no train sees exactly the old plan. A train that can no longer land (red,
    BEHIND/DIRTY, or open past the train's own stale-minutes; pr_hold.dead) holds nothing (v1.35.0): the lane the
    queue head needs is released with the hold's, never half of it."""
    trains = [p for p in prs if str(p.get("headRefName") or "").startswith(TRAIN_PREFIX)
              and not p.get("isCrossRepository")]
    riding = {p["number"] for p in armed if any(lb.get("name") == RIDING for lb in p.get("labels") or [])}
    if riding:
        summary.append(f"riding a merge train, not updated: {', '.join('#' + str(n) for n in sorted(riding))}")
    q = pr_queue.plan([p for p in armed if p not in trains and p["number"] not in riding],
                      opts.max_in_flight, opts.required, machinery, restarted, base_retry)
    why = pr_hold.dead(trains[-1], opts.required, time.time(), opts.train_stale_min * 60) if trains else ""
    if why:  # a dead train (red, main moved, past its bound) holds nothing: pr_hold.dead, ONE rule with the hold
        summary.append(f"merge train #{trains[-1]['number']} open but not live, updates not held "
                       f"(lane RELEASED: it is {why})")
        trains = []
    if trains:
        held = [p for p in q.update if (p.get("mergeStateStatus") or "").upper() != "DIRTY"
                and not any(lb.get("name") in (EJECTED, NEEDS_UPDATE) for lb in p.get("labels") or [])]
        q.update = [p for p in q.update if p not in held]
        # spare holds only eligible PRs, never DIRTY: with a train open only an EJECTED or
        # train-needs-update one may take a slot. Each slot the hold just emptied goes to the next
        # of those, so an ordinary lower-numbered PR held for the train cannot idle the slot an
        # ejected PR needs (#2980's idle slot, train edition -- review of 1.26.1, finding 1).
        q.spare = [p for p in q.spare
                   if any(lb.get("name") in (EJECTED, NEEDS_UPDATE) for lb in p.get("labels") or [])]
        promoted, q.spare = q.spare[:len(held)], q.spare[len(held):]
        q.update += promoted
        q.held = [(p, why) for p, why in q.held if p not in promoted]
        q.held += [(p, f"merge train #{trains[-1]['number']} is open") for p in held]
        summary.append(f"merge train #{trains[-1]['number']} open: {len(held)} update(s) held, conflicts still checked")
    return q


def _read_prs(repo: str, summary: list[str]) -> list[dict]:
    """The open PRs, an UNKNOWN merge state re-read before anything is planned on it
    (pr_list.settle: not computed is not behind -- a non-strict base never reports BEHIND)."""
    errors: list[str] = []
    prs, unknown = pr_list.settle(lambda: pr_list.rows(json.loads(_gh_read(*pr_list.gh_args(repo)).stdout)),
                                  errors=errors)
    if errors:
        summary.append("re-reading an UNKNOWN merge state failed (planned on the last good read): "
                       + "; ".join(errors))
    if unknown:
        summary.append("merge state still UNKNOWN after re-reading (planned as behind): "
                       + ", ".join(f"#{n}" for n in unknown))
    return prs


def run(repo: str, summary: list[str], relock: list[int], opts: Options, healing: list[dict] | None = None,
        later: list[Callable[[list[str]], None]] | None = None) -> int:
    # One pass. The held pass (`update_held`) is LAST: with `later` (main) it is handed back to run
    # after this pass's outputs and summary are written, so it can never delay or lose them;
    # without (a direct caller) it runs here, at the end (`_held_last`).
    prs = _read_prs(repo, summary)
    # Renovate's automerge PRs JOIN the serial queue (ci_kit/pr_renovate.py): armed here, so no
    # session arms them by hand. Only with a queue: without one Renovate merges itself, on EVERY check
    joined = pr_renovate.join(repo, prs, _gh_read, pr_heal.write, summary) if opts.max_in_flight > 0 else 0
    armed = [p for p in prs if p.get("autoMergeRequest") and not p.get("isCrossRepository")]
    summary.append(f"{len(armed)} armed PR(s) of {len(prs)} open")
    # a PARKED PR (the scheduler cancelled or deferred its run) is eligible like a machinery red:
    # nothing in it is the author's to fix, and the update is its fresh run
    parked = frozenset(int(p["number"]) for p in prs if _has(p, pr_sched.PARKED))
    base = pr_hold.default_branch(repo, _gh_read) if opts.hold or opts.scarce else None
    try:  # reads only; the hold (the head's `queue/position` success) runs whatever happens here
        heal, errors = pr_heal.assess_armed(repo, armed + renovate_pool(prs, opts), opts.required, opts.rerun_limit, _gh_read,
                                            pr_heal.comments_of, summary, ladder=opts.ladder.on)
    except Exception:  # noqa: BLE001 -- whatever it was, the head gets its success, then it re-raises
        if opts.hold:
            pr_hold.hold(repo, prs, _gh_read, pr_heal.write, summary, pr_heal.comments_of,
                         pr_queue.blocked(prs, opts.required, parked, base), base=base,
                         stale_s=opts.head_stall_min * 120, required=opts.required,
                         train_stale_s=opts.train_stale_min * 60)
        raise
    errors += joined
    machinery = pr_heal.machinery(heal)
    default = _lazy_default(repo, base)
    cancel = doomed_runs.Canceller(repo, opts.review_workflow, _gh, summary) if opts.cancel_doomed else None
    if cancel and doomed_runs.doomed(prs, behind=True):
        # BEFORE the plan: a head that cannot merge and whose result nothing reads. A BEHIND PR whose
        # running checks it cancels is updated THIS pass (`restarted`), never held for a completion
        # that will not come (session review, round 2: a caller skipping cancelled runs never woke)
        cancel.sweep(prs, default(), bool(opts.train_workflow))
    # ONE head for the hold, the updater and the scheduler: the lowest armed PR that can land
    skip = pr_queue.blocked(prs, opts.required, machinery | parked, base)
    if opts.hold:
        errors += pr_hold.hold(repo, prs, _gh_read, pr_heal.write, summary, pr_heal.comments_of, skip,
                               base=base, stale_s=opts.head_stall_min * 120, required=opts.required,
                               train_stale_s=opts.train_stale_min * 60)
    retry: dict[int, str] = {}
    if opts.base_retry_error:
        errors += 1
        print(f"::error::{opts.base_retry_error}")
        summary.append(f"base-driven retry OFF this pass: {opts.base_retry_error}")
    if opts.max_in_flight > 0 and opts.base_retry_cap > 0:  # with no queue every armed PR is updated anyway
        retry, e = pr_baseretry.assess(repo, armed, opts.required, opts.label, machinery | parked,
                                       lambda p: str(p.get("baseRefName") or default()), opts.base_retry_cap,
                                       _gh_read, pr_heal.comments_by, pr_heal.write, summary,
                                       frozenset(opts.base_retry_bots), opts.base_retry_per_pass)
        errors += e
    q = _train_plan(prs, armed, summary, opts, machinery | parked,
                    frozenset(cancel.restarted) if cancel else frozenset(), frozenset(retry))
    # the head first, then its slot: (moved up this pass, holding a slot it is not yet in flight in)
    head, keep = _head_first(repo, prs, q, opts, summary, skip), _reserve(prs, q, opts, summary, skip)
    if opts.max_in_flight > 0:
        summary.append(f"queue: {len(q.in_flight)}/{opts.max_in_flight} in flight "
                       f"[{', '.join('#' + str(p['number']) for p in q.in_flight)}], {len(q.update)} to attempt, "
                       f"{len(q.held)} held")
        summary += [f"#{p['number']}: in flight against current main" for p in q.in_flight]
        summary += [f"#{p['number']}: held, {why}" for p, why in q.held]
    attempted, updated, conflicts, err = _drive(repo, q, head, opts, relock, summary, cancel, retry, keep)
    # an IDLE queue (nothing in flight, nothing attempted) lands a CLEAN Renovate PR itself, which GitHub
    # refuses to arm (pr_renovate.land_idle)
    errors += err + (cancel.errors if cancel else 0) + (
        pr_renovate.land_idle(repo, prs, q.in_flight, q.update, _gh_read, pr_heal.write, summary)
        if opts.max_in_flight > 0 else 0)
    # ONE ACTOR PER HEAD: a PR this pass base-retried has a new head on its way; its old head goes to
    # neither the ladder (whose dispatch marker would spend a tier-3 slot on a head already gone) nor pr_heal
    retried = set(retry) & updated
    if retried:
        heal = {n: v for n, v in heal.items() if n not in retried}
        summary.append("base-retried this pass, not offered to the heal ladder or smart auto-merge: "
                       + ", ".join(f"#{n}" for n in sorted(retried)))
    e, taken = _ladder(repo, heal, conflicts + [p for p in renovate_pool(prs, opts) if _dirty(p)], opts, summary, healing)
    errors += e
    errors += pr_heal.act(repo, heal, updated, opts.rerun_limit, pr_heal.write, summary, frozenset(taken))
    if opts.scarce or parked:  # parked work is re-run even with the scheduler switched off: never strand
        errors += pr_sched.pass_(
            repo, prs, dict(opts.scarce), _gh_read, pr_heal.write, summary,
            preempt_after_s=opts.preempt_after_min * 60, stall_s=opts.head_stall_min * 60,
            behind=lambda p: (p.get("mergeStateStatus") or "UNKNOWN").upper() in ("BEHIND", "UNKNOWN"),
            before=opts.before, sha=opts.sha, updated=frozenset(int(n) for n in updated), skip=skip)
    errors += unlabel_resolved(repo, prs, attempted, opts, summary)
    if opts.train_workflow:
        # a PR this pass just updated is no longer green at its new head: never count it ready. The
        # default branch is read HERE only, so a caller without hold/scarce sees exactly the old plan.
        errors += train_kick.kick(repo, opts.train_workflow, [p for p in prs if p["number"] not in updated],
                                  opts.required, default(), _gh, summary, opts.train_min_batch)
    # LAST, contained: it never reaches `errors` (pr_held.py)
    _held_last(repo, prs, q, opts, summary, later, skip, bool(relock or healing))
    return 1 if errors else 0


def _held_last(repo: str, prs: list[dict], q: pr_queue.Plan, opts: Options, summary: list[str],
               later: list[Callable[[list[str]], None]] | None, skip: frozenset[int] = frozenset(),
               dispatch: bool = False) -> None:
    """The pass's TAIL. Off: nothing. On: the held pass, then the head's verdict wait (`_await_head`,
    which can take minutes, so it is last of all), each handed to `later` (main runs them after
    `relock=` / `heal=` are written), or run now when the caller passed none. `dispatch` = this pass
    has a relock or a heal to hand on: a JOB's outputs reach the job that needs them only when this
    job ENDS, so the wait would hold that dispatch for its whole length -- such a pass does not wait
    (review of v1.45.0, R4), and the verdict's own completion starts the pass that updates the head."""
    steps: list[Callable[[list[str]], None]] = []
    if opts.update_held:
        steps.append(lambda tail: _held_boundary(repo, prs, q, opts, tail))
    if q.awaiting is not None and opts.head_wait_s > 0 and dispatch:
        summary.append(f"#{q.awaiting['number']}: queue head, its verdict is NOT waited for -- this pass hands on a "
                       "relock or a heal, which a waiting job would hold back")
    elif q.awaiting is not None and opts.head_wait_s > 0:
        steps.append(lambda tail: _await_head(repo, q.awaiting, skip, opts, tail))
    for step in steps:
        if later is None:
            step(summary)
        else:
            later.append(step)


def _await_head(repo: str, h: dict, skip: frozenset[int], opts: Options, summary: list[str]) -> None:
    """THE HEAD'S VERDICT IS WAITED FOR (v1.45.0, `opts.head_wait_s`), after everything else the pass
    does and after `relock=` / `heal=` are written (a pass that has either does not wait at all:
    `_held_last`). A head left BEHIND because its review is past the young
    bound is one recorded verdict from being updatable, and the pass that would do it is a NEW job
    on a pool that takes minutes to hand out a runner. A consumer's #3209, 2026-11-26: main moved
    at 06:19:54Z; the pass that push started got its runner 229 s later and decided at about
    06:24:05Z with the model review finished (06:22:50Z); the verdict was on record at 06:24:12Z;
    the next pass could not start before the gate's tail job ended at 06:26:04Z, and the branch was
    updated by hand at 06:25:20Z while `unit tests` ran on the stale head. So THIS pass reads the
    review again every HEAD_POLL_S, up to head_wait_s, and updates the head the moment the verdict
    is on record -- on a FRESH read of the PR (`_not_now`). One PUT (`_put`) and one line. A
    conflict is not labelled or paged from here: the next pass does that, once. Never raises and
    never touches the exit code (the boundary `_held_boundary` keeps): a failure is one line and
    one WARNING annotation."""
    n, branch, t0 = int(h["number"]), str(h["headRefName"]), time.time()
    try:
        while True:
            # a NEW head ends the wait at once: its push starts its own pass, which would sit
            # pending behind this job for the rest of the bound (review of v1.45.0, R6)
            if pr_sched.moved(repo, branch, str(h.get("headRefOid") or ""), _gh_read):
                summary.append(f"#{n}: queue head, a new head was pushed {time.time() - t0:.0f}s into the wait -- "
                               "the wait ends, the pass that push starts decides")
                return
            if pr_sched.review_settled(repo, branch, _gh_read, str(h.get("headRefOid") or "")):
                break
            if time.time() - t0 + HEAD_POLL_S > opts.head_wait_s:
                summary.append(f"#{n}: queue head, still behind main -- no verdict within {opts.head_wait_s:g}s of "
                               "waiting; the review's completion starts the pass that updates it")
                return
            time.sleep(HEAD_POLL_S)
        waited = time.time() - t0
        fresh = _read_prs(repo, summary)
        now = next((p for p in fresh if p.get("number") == n), None)
        if why := _not_now(now, h, fresh, skip, opts):
            summary.append(f"#{n}: queue head, verdict on record {waited:.0f}s into the wait, not updated: {why}")
            return
        o, gone = _put(repo, now)
        summary.append(f"#{n}: queue head, verdict on record {waited:.0f}s into the wait -> "
                       + (f"{gone} during the update" if gone else _line(n, o).split(": ", 1)[1]))
    except Exception as e:  # noqa: BLE001 -- the boundary: whatever it was, the pass's result stands
        msg = f"head verdict wait: FAILED {type(e).__name__}: {e}"
        summary.append(msg)
        print(f"::warning::{msg}")


def _not_now(now: dict | None, h: dict, fresh: list[dict], skip: frozenset[int], opts: Options) -> str:
    """Why the head is NOT updated after its verdict recorded ('' = update it), on a fresh read: the
    plan this pass made is minutes old by then. A red verdict is a failing check -- the author's."""
    if now is None:
        return "no longer open"
    state = (now.get("mergeStateStatus") or "UNKNOWN").upper()
    reasons = (
        (now.get("headRefOid") != h.get("headRefOid"), "a new head was pushed (the pass that push starts decides)"),
        (not now.get("autoMergeRequest"), "no longer armed"),
        # R7a: `skip` is what THIS pass read as blocked, minutes ago -- said, not hidden
        (pr_queue.head(fresh, skip) is not now, "no longer the queue head (by what this pass read as blocked)"),
        (bool(_train_live(fresh, opts)), "a merge train holds the lane"),
        (state != "BEHIND", f"merge state {state}, not BEHIND"),
        (_has(now, opts.label), f"labelled `{opts.label}`"),
        (pr_queue.check_state(now.get("statusCheckRollup"), opts.required) == "failing",
         "a check is failing on its head (the author's fix re-queues it)"),
    )
    return next((why for hit, why in reasons if hit), "")


def _drive(repo: str, q: pr_queue.Plan, head: int | None, opts: Options, relock: list[int], summary: list[str],
           cancel: doomed_runs.Canceller | None = None,
           retry: dict[int, str] | None = None, keep: int | None = None) -> tuple[set[int], set[int], list[dict], int]:
    """The update loop: (attempted, updated, conflicts, errors). After each update the head it
    replaced is a head that can never merge, so its `pull_request` runs are cancelled (doomed_runs).
    A base-driven retry (`retry`: PR -> its marker) is RECORDED before its PUT; no record, no update.
    `keep` = the stale queue head that holds a slot (`_reserve`): skipped for its running review, its
    slot is NOT handed to the next PR -- that PR could not merge before the head (the queue hold)."""
    errors = 0
    tracker = ""
    if opts.tracker:
        tracker = _gh("issue", "view", str(opts.tracker), "-R", repo, "--json", "body", "-q",
                      ".body", check=False).stdout
    todo, spare, attempted, updated, conflicts = list(q.update), list(q.spare), set(), set(), []
    while todo:
        p = todo.pop(0)
        n = p["number"]
        if n != head and reviewing(repo, p["headRefName"], opts.review_workflow):
            summary.append(f"#{n}: skipped, review gate running (retried when it completes)"
                           + ("; the queue head keeps its slot" if n == keep else ""))
            if (p.get("mergeStateStatus") or "").upper() != "DIRTY" and spare and n != keep:
                # the slot it would have taken is free: the next PR in line takes it, so one PR's
                # running review does not idle the queue (a consumer's #2980, 2026-11-11: skipped 21:10-23:32Z
                # with 0/1 in flight while five armed PRs waited behind it)
                nxt = spare.pop(0)
                summary.append(f"#{nxt['number']}: takes the slot #{n}'s running review left free")
                todo.append(nxt)
            continue
        if n in (retry or {}) and not pr_baseretry.record(repo, n, (retry or {})[n], pr_heal.write):
            errors += 1
            summary.append(f"#{n}: base-driven retry NOT made: its marker comment failed (the record is the budget)")
            continue
        attempted.add(n)
        kind, line = _update_one(repo, p, opts, tracker, relock)
        updated |= {n} if kind == "updated" else set()
        if kind == "updated" and cancel:
            cancel.superseded(p)
        conflicts += [p] if kind == "conflict" else []
        errors += kind == "error"
        if line:
            summary.append(line)
    return attempted, updated, conflicts, errors


def _held_boundary(repo: str, prs: list[dict], q: pr_queue.Plan, opts: Options, summary: list[str]) -> None:
    """The opt-in held-PR pass, run after EVERYTHING the armed pass does. Never raises and never
    touches the run's exit code: a failure is one `update-held: FAILED` line and one WARNING
    annotation, counted in the closing line's errors -- not silent, and not red on every tick.
    Off (the default) = returns at once: not one call, not one line."""
    if not opts.update_held:
        return
    if opts.event not in pr_held.EVENTS:
        summary.append(f"update-held: not this pass ({opts.event or 'unknown event'}; held PRs are updated "
                       f"on {' / '.join(pr_held.EVENTS)} only)")
        return
    tally = {"updated": 0, "eligible": 0, "skipped": 0, "errors": 0}
    try:
        _held_pass(repo, prs, q, opts, summary, tally)
    except Exception as e:  # noqa: BLE001 -- the boundary: whatever it was, the armed result stands
        tally["errors"] += 1
        msg = f"update-held: FAILED {type(e).__name__}: {e}"
        summary.append(msg)
        print(f"::warning::{msg}")
    summary.append(f"update-held: {tally['updated']} held PR(s) updated of {tally['eligible']} eligible, "
                   f"{tally['skipped']} skipped, {tally['errors']} error(s)")


def _held_pass(repo: str, prs: list[dict], q: pr_queue.Plan, opts: Options, summary: list[str],
               tally: dict[str, int]) -> None:
    """Select, admit, update. A held PR is never labelled, paged, put in `relock`, offered to
    doomed_runs or to the heal ladder: each outcome is ONE summary line (`_held_update`)."""
    eligible, skipped = pr_held.select(prs, time.time())
    todo, waiting = pr_held.admit(eligible, prs, q, opts.max_in_flight, opts.required)
    skipped += waiting
    tally["eligible"], tally["skipped"] = len(eligible), len(skipped)
    summary += [f"#{p['number']}: held, not updated: {why}" for p, why in skipped]
    for p in todo:
        if reviewing(repo, p["headRefName"], opts.review_workflow):
            summary.append(f"#{p['number']}: held, not updated: review gate running")
            continue
        kind, line = _held_update(repo, p)
        tally["updated"] += kind == "updated"
        tally["errors"] += kind == "error"
        summary.append(line)


def _held_update(repo: str, p: dict) -> tuple[str, str]:
    """One held PR's update: the shared PUT (`_put`), then a line and nothing else, whatever it said."""
    o, gone = _put(repo, p)
    if gone:
        return "gone", f"#{p['number']}: held, not updated: {gone} during the update"
    if o.kind in ("updated", "current"):
        return o.kind, _line(p["number"], o)
    return o.kind, f"#{p['number']}: held, not updated: " + _line(p["number"], o).split(": ", 1)[1]


def _lazy_default(repo: str, base: str | None) -> Callable[[], str]:
    """The default branch, read at most once and only when asked: the plan's own `base` stays
    exactly what hold/scarce made it (review of 1.34.0, finding 2)."""
    got: list[str] = []

    def read() -> str:
        if not got:
            got.append(base or pr_hold.default_branch(repo, _gh_read) or "main")
        return got[0]
    return read


def _ladder(repo: str, heal: pr_heal.Heal, conflicts: list[dict], opts: Options, summary: list[str],
            healing: list[dict] | None) -> tuple[int, set[int]]:
    """Tiers 1-4 (ci_kit/pr_ladder.py): each dispatched row goes to the `heal` output, which the
    caller's pr-heal.yml call runs. (errors, the PR numbers the ladder took). Off = (0, none)."""
    if not opts.ladder.on:
        return 0, set()
    rows, errors, taken = pr_ladder.dispatch(repo, ladder_candidates(heal, conflicts), opts.ladder,
                                             lambda r, n: heal[n][1] if n in heal else pr_heal.comments_of(r, n),
                                             pr_heal.write, summary, page)
    if healing is not None:
        healing += rows
    return errors, taken


def renovate_pool(prs: list[dict], opts: Options) -> list[dict]:
    """With the heal ladder on, the Renovate PRs Renovate will merge itself: never armed (it merges
    them, `platformAutomerge: false`), so the armed pass never saw them red or conflicted. Their
    machinery reds get pr_heal's rerun, their judgment reds and conflicts the ladder. Pure."""
    if not opts.ladder.on:
        return []
    return [p for p in prs if not p.get("autoMergeRequest") and not p.get("isCrossRepository")
            and pr_ladder.is_renovate(p) and pr_ladder.armed(p)]


def _dirty(p: dict) -> bool:
    return (p.get("mergeStateStatus") or "").upper() == "DIRTY"


def ladder_candidates(heal: pr_heal.Heal, conflicts: list[dict]) -> list[pr_ladder.Candidate]:
    """What the heal ladder is offered: each PR this pass found conflicting, and each armed PR whose
    red is JUDGMENT (a machinery red is pr_heal's rerun; a parked one the scheduler's). Pure."""
    out = [pr_ladder.Candidate(p, "conflict") for p in conflicts]
    for _, (v, _, p) in sorted(heal.items()):
        if v.kind == "judgment":
            checks = ", ".join(c for r in v.reds for c in r.checks) or ", ".join(v.unrunnable) or \
                ", ".join(pr_heal.red_checks(p.get("statusCheckRollup")))
            out.append(pr_ladder.Candidate(p, "red", checks, ",".join(str(r.run) for r in v.reds)))
    return out


def _train_live(prs: list[dict], opts: Options) -> dict | None:
    """The open merge train that still holds the lane, or None: `_train_plan`'s own rule (the newest
    same-repo train, unless pr_hold.dead). `_train_plan` writes "merge train" only on the PRs it took
    OUT of `q.update`; a head held for its running checks keeps that reason, so the head's own
    guards read the train here, not in the reason (review of v1.45.0, R2)."""
    trains = [p for p in prs if str(p.get("headRefName") or "").startswith(TRAIN_PREFIX)
              and not p.get("isCrossRepository")]
    live = trains and not pr_hold.dead(trains[-1], opts.required, time.time(), opts.train_stale_min * 60)
    return trains[-1] if live else None


def _has(p: dict, label: str) -> bool:
    return any(lb.get("name") == label for lb in p.get("labels") or [])


def _head_first(repo: str, prs: list[dict], q: pr_queue.Plan, opts: Options, summary: list[str],
                skip: frozenset[int] = frozenset()) -> int | None:
    """Update the queue HEAD the moment it is safe, ahead of the slot count: BEHIND main, armed, not
    red on its own checks, and no review run still owes a verdict (`pr_sched.review_settled`) -- the
    gate's tail is not a reason to wait (a consumer's #3073, 03:58Z 11-14: verdict recorded, update held for the
    tail job, then the sealed runner went to another PR). Returns the head's number when it was moved
    up (so the loop does not skip it for that tail), else None. Off unless the scheduler is on.

    HEAD FAIL-FAST (v1.45.0, `opts.head_fail_fast`). A verdict still owed no longer always means
    wait: while the review is YOUNG (`pr_sched.review_young`: under `preempt_after_min` of model
    time) the head is updated NOW. A consumer's #3201, 2026-11-26: armed, BEHIND, three fix pushes
    (04:21, 04:35, 04:43Z). The passes at 04:21:44 and 04:36:08Z each read "its review has not
    recorded a verdict yet" while the model job had not started (04:24:44, 04:39:09Z), so CI ran
    9.6 and 8.9 runner-minutes on a head that could not merge, and the third push was updated by
    hand 7 min 41 s later with 14.0 more spent. Waiting protects a review; a review that has
    cost nothing protects nothing. The update's push cancels the stale head's runs (the caller's
    own per-PR concurrency, and doomed_runs.superseded) and the ONE full CI run and the one
    review happen at a head that can merge. It cannot loop: the update makes the head current,
    and under the queue hold main moves only when this head lands."""
    h = pr_queue.head(prs, skip)
    if not opts.scarce or h is None or not h.get("autoMergeRequest"):
        return None
    queued = any(p is h for p in q.update)
    if not queued and not any(p is h for p in q.spare) and not any(p is h for p, _ in q.held):
        return None  # in flight, or not armed: nothing to move
    why = next((w for p, w in q.held if p is h), "")
    if (h.get("mergeStateStatus") or "UNKNOWN").upper() not in ("BEHIND", "UNKNOWN") or "failing" in why \
            or "merge train" in why or _has(h, opts.label):
        return None
    if not queued and (train := _train_live(prs, opts)):
        # the train owns the lane: the head moved up beside it would put the train BEHIND, two full
        # runs on the pool the train needs. A head the plan itself left in `q.update` (EJECTED,
        # train-needs-update: its own update is its only lane) is the plan's call, as before
        summary.append(f"#{h['number']}: queue head, behind main -- merge train #{train['number']} holds the lane, "
                       "not moved up")
        return None
    how = "verdict settled -> updated first"
    at = str(h.get("headRefOid") or "")  # the review reads look at the runs AT this head only
    if not pr_sched.review_settled(repo, str(h["headRefName"]), _gh_read, at):
        young, why_not = pr_sched.review_young(repo, str(h["headRefName"]), _gh_read, time.time(),
                                               opts.preempt_after_min * 60, at) if opts.head_fail_fast else (False, "")
        if not young:
            q.awaiting = h if why_not and opts.head_wait_s > 0 else None
            summary.append(f"#{h['number']}: queue head, behind main -- its review has not recorded a verdict yet"
                           + (f" ({why_not}; updated when it records"
                              f"{', which this pass waits for' if q.awaiting else ''})" if why_not else ""))
            return None
        how = (f"review under {opts.preempt_after_min:g} min of model time -> updated first, NOW (head fail-fast: "
               "the run at the stale head is cancelled, the one full run is at the current head)")
    if not queued:
        q.update.insert(0, h)
        q.held = [(p, w) for p, w in q.held if p is not h]
        q.spare = [p for p in q.spare if p is not h]
    summary.append(f"#{h['number']}: queue head, behind main, {how}")
    return int(h["number"])


def _reserve(prs: list[dict], q: pr_queue.Plan, opts: Options, summary: list[str],
             skip: frozenset[int] = frozenset()) -> int | None:
    """THE HEAD HOLDS A SLOT (pr_queue.reserve, v1.45.0): with the queue hold on, a stale queue head
    that can land takes one of the `max_in_flight` slots whether this pass updates it or it still
    waits for its verdict, and the PRs that slot was planned for go back to the line. Returns the
    head's number while it holds a slot it is not yet in flight in (`_drive` then does not hand
    that slot on), else None. Off without the hold (a non-head PR can then merge on its own, so
    its update is not wasted), and while a merge train is open (the train owns the lane, and an
    EJECTED PR's own update is the only lane it has left: `_train_plan`)."""
    h = pr_queue.head(prs, skip)
    if not opts.hold or opts.max_in_flight <= 0 or h is None or not h.get("autoMergeRequest"):
        return None
    if any(p is h and "merge train" in w for p, w in q.held) or any(p is h for p in q.in_flight) \
            or _train_live(prs, opts):
        return None
    if (h.get("mergeStateStatus") or "UNKNOWN").upper() not in ("BEHIND", "UNKNOWN"):
        return None
    out = pr_queue.reserve(q, h, opts.max_in_flight)
    if out:
        summary.append(f"queue head #{h['number']} is behind main and takes the slot: "
                       + ", ".join(f"#{p['number']}" for p in out) + " not updated this pass (it could not "
                       "merge before the head, so its run would be thrown away)")
    return int(h["number"])


def _put(repo: str, p: dict) -> tuple[Outcome, str]:
    """PUT update-branch for one PR: (what it answered, the PR's new state if it is no longer open
    -- '' while open). Shared by the armed path (`_update_one`) and the held one (`_held_update`)."""
    n = p["number"]
    r = _gh("api", "-X", "PUT", f"repos/{repo}/pulls/{n}/update-branch",
            "-f", f"expected_head_sha={p['headRefOid']}", check=False)
    o = classify(r.returncode, r.stdout or r.stderr)
    if o.kind in ("conflict", "error"):
        # live proof 2026-09-26: a PR merged between `pr list` and this PUT, and GitHub
        # answered "merge conflict" for a PR that no longer existed as open
        state = _gh("pr", "view", str(n), "-R", repo, "--json", "state", "-q", ".state", check=False)
        if state.returncode == 0 and state.stdout.strip() not in ("", "OPEN"):
            return o, state.stdout.strip().lower()
    return o, ""


def _line(n: int, o: Outcome) -> str:
    return f"#{n}: {o.kind}" + (f" ({o.detail})" if o.detail else "")


def _update_one(repo: str, p: dict, opts: Options, tracker: str, relock: list[int]) -> tuple[str, str]:
    """PUT update-branch for one ARMED PR and act on the answer: (outcome kind | 'gone', summary line)."""
    n = p["number"]
    labelled = any(lb.get("name") == opts.label for lb in p.get("labels") or [])
    o, gone = _put(repo, p)
    if gone:
        return "gone", f"#{n}: {gone} during the update, skipped"
    add, do_page, remove = actions_for(o, labelled)
    if do_page and opts.ladder.on and not pr_ladder.blocked(p):
        do_page = False  # the heal ladder takes the conflict; its tier 4 is the page
    line = _line(n, o)
    if add:
        added, created = add_label(repo, n, opts.label)
        if created:
            line += f"; `{opts.label}` label created"
        if not added:
            # the label IS the page dedup: unlabelled, this PR would be paged on every merge
            o = Outcome("error", f"conflict, but the `{opts.label}` label could not be added")
            do_page = False
        else:
            relock.append(n)
    if remove:
        _gh("pr", "edit", str(n), "-R", repo, "--remove-label", opts.label, check=False)
        line += "; conflict label removed"
    if do_page:
        title, body = page_text(repo, p, tracker_row(tracker, n), opts)
        line += "; page " + page(title, body, p["url"])
    if o.kind == "error":
        print(f"::error::#{n}: {o.detail}")
    return o.kind, line


def stale_labels(prs: list[dict], attempted: set[int], label: str) -> list[int]:
    """Open PRs carrying the conflict label that GitHub now reports MERGEABLE, not attempted this
    run. Pure. The label was only ever removed from a PR the queue ATTEMPTED; a conflict resolved
    by a session's push leaves the PR current with main, so it was never attempted again and kept
    the label (a consumer's #2983, 2026-11-11: labelled 23:05Z, resolved 23:32Z, still labelled). `mergeable`
    is GitHub's own test merge against the CURRENT base, recomputed after every push to it;
    UNKNOWN (not computed yet) keeps the label until a run that knows."""
    return sorted(int(p["number"]) for p in prs
                  if int(p["number"]) not in attempted and (p.get("mergeable") or "").upper() == "MERGEABLE"
                  and (p.get("mergeStateStatus") or "").upper() != "DIRTY"
                  and any(lb.get("name") == label for lb in p.get("labels") or []))


def unlabel_resolved(repo: str, prs: list[dict], attempted: set[int], opts: Options, summary: list[str]) -> int:
    errors = 0
    for n in stale_labels(prs, attempted, opts.label):
        r = _gh("pr", "edit", str(n), "-R", repo, "--remove-label", opts.label, check=False)
        if r.returncode:
            errors += 1
            print(f"::error::#{n}: could not remove the stale `{opts.label}` label: {(r.stderr or '').strip()[:200]}")
        summary.append(f"#{n}: `{opts.label}` label {'NOT ' if r.returncode else ''}removed -- GitHub reports it "
                       "mergeable with main now (resolved since it was labelled)")
    return errors


def head_wait_s(fail_fast: bool) -> float:
    """The tail's verdict wait in seconds (env CI_KIT_HEAD_VERDICT_WAIT_S, default HEAD_WAIT_S): 0 with
    head fail-fast off, and 0 -- with a WARNING, never a crashed pass -- for a value that is no number.
    CLAMPED to HEAD_WAIT_MAX_S, with a WARNING: the updater job has `timeout-minutes: 10`, and a wait
    that outlives it is a job killed mid-wait (red, its relock= / heal= consumers skipped), not a longer wait."""
    raw = os.environ.get("CI_KIT_HEAD_VERDICT_WAIT_S", "").strip()
    if not fail_fast:
        return 0.0
    try:
        want = max(0.0, float(raw)) if raw else HEAD_WAIT_S
        if want > HEAD_WAIT_MAX_S:
            print(f"::warning::head-verdict-wait-seconds {raw} is over the {HEAD_WAIT_MAX_S:g} s the updater job's "
                  f"timeout leaves: the head's verdict is waited for {HEAD_WAIT_MAX_S:g} s")
        return min(want, HEAD_WAIT_MAX_S)
    except ValueError:
        print(f"::warning::head-verdict-wait-seconds is not a number ({raw!r}): the head's verdict is not waited for")
        return 0.0


def base_retry_int(raw: str, name: str, default: int, lo: int, hi: int) -> tuple[int, str]:
    """(value, error) for a base-retry input. Unreadable or outside [lo, hi] = (0, why): that ONE
    feature is refused and named, never the whole updater crashed. Pure."""
    raw = (raw or "").strip()
    if not raw:
        return default, ""
    try:
        v = int(raw)
    except ValueError:
        return 0, f"{name} {raw!r} is not an integer: base-driven retry refused"
    return (v, "") if lo <= v <= hi else (0, f"{name} {v} is outside {lo}..{hi}: base-driven retry refused")


def base_retry_cap(raw: str) -> tuple[int, str]:
    """(cap, error) from CI_KIT_BASE_RETRY_CAP. A cap above pr_baseretry.CHAIN is refused: the count
    walks at most CHAIN merges, so a larger cap could never be reached (2nd review of 0df4957)."""
    return base_retry_int(raw, "base-retry-cap", pr_baseretry.CAP, 0, pr_baseretry.CHAIN)


def base_retry_warning(bots: tuple[str, ...], cap: int, max_in_flight: int) -> str:
    """A non-empty bot list that cannot act (cap 0 or no queue) is named, never silently inert. Pure."""
    if not bots or (cap > 0 and max_in_flight > 0):
        return ""
    off = "base-retry-cap is 0" if cap <= 0 else "max-in-flight is 0 (no queue)"
    return f"base-retry-bot-authors is set ({', '.join(bots)}) but {off}: the no-author case is OFF"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ci-pr-autoupdate", description=__doc__.split("\n", 1)[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--review-workflow", default="",
                    help="workflow file whose queued/running run defers a PR's update (empty: none)")
    ap.add_argument("--tracker", type=int, default=0,
                    help="issue number whose row for a PR is quoted in the page (0: none)")
    ap.add_argument("--relock", action="store_true",
                    help="the caller relocks lockfile-only conflicts; say so in the page")
    ap.add_argument("--label", default=LABEL, help="the conflict label (default: %(default)s)")
    ap.add_argument("--max-in-flight", type=int, default=0,
                    help="armed PRs tested against current main at once, lowest PR number first (0: every armed PR)")
    ap.add_argument("--required-checks", default=os.environ.get("CI_KIT_REQUIRED_CHECKS", ""),
                    help="comma-separated check names that count for the queue ('' : every check on the head;"
                         " default: env CI_KIT_REQUIRED_CHECKS, since names carry spaces)")
    ap.add_argument("--scarce-pools", default=os.environ.get("CI_KIT_SCARCE_POOLS", ""),
                    help='the PR scheduler: JSON {"pool-label": capacity} (ci_kit/pr_sched.py; empty: off)')
    ap.add_argument("--preempt-after-min", type=float, default=3.0,
                    help="park a non-head job on a scarce pool only if it started less than this ago")
    ap.add_argument("--head-stall-min", type=float, default=15.0,
                    help="the head waiting this long on a pool a non-head job holds turns the run red")
    ap.add_argument("--rerun-limit", type=int, default=pr_heal.LIMIT,
                    help="smart auto-merge: machinery reruns per PR head (ci_kit/pr_heal.py; 0: off)")
    args = ap.parse_args(argv)
    try:
        scarce = tuple(sorted(pr_sched.capacity_of(args.scarce_pools).items()))
    except ValueError as e:
        ap.error(str(e))
    cap, cap_error = base_retry_cap(os.environ.get("CI_KIT_BASE_RETRY_CAP", ""))
    per_pass, per_error = base_retry_int(os.environ.get("CI_KIT_BASE_RETRY_MAX_PER_PASS", ""),
                                         "base-retry-max-per-pass", pr_baseretry.PER_PASS, 1, 100)
    if per_error:
        cap, cap_error = 0, "; ".join(x for x in (cap_error, per_error) if x)
    bots = tuple(sorted({s.strip() for s in os.environ.get("CI_KIT_BASE_RETRY_BOT_AUTHORS", "").split(",") if s.strip()}))
    if warn := base_retry_warning(bots, cap, args.max_in_flight):
        print(f"::warning::{warn}")
    fail_fast = os.environ.get("CI_KIT_HEAD_FAIL_FAST", "true").strip().lower() not in ("false", "0", "no")
    opts = Options(review_workflow=args.review_workflow, tracker=args.tracker, relock=args.relock,
                   label=args.label, max_in_flight=args.max_in_flight,
                   required=tuple(s.strip() for s in args.required_checks.split(",") if s.strip()),
                   rerun_limit=args.rerun_limit,
                   scarce=scarce,
                   preempt_after_min=args.preempt_after_min, head_stall_min=args.head_stall_min,
                   # both are a PUSH's: another event's `before` is not main's previous sha (a
                   # `synchronize` carries the PR's previous HEAD), whatever the caller's env says
                   before=os.environ.get("CI_KIT_BEFORE", "") if os.environ.get("CI_KIT_EVENT") == "push" else "",
                   sha=os.environ.get("CI_KIT_SHA", "") if os.environ.get("CI_KIT_EVENT") == "push" else "",
                   hold=os.environ.get("CI_KIT_QUEUE_HOLD", "").strip().lower() in ("1", "true"),
                   # env vars, never flags: an older @v1 tool would refuse a flag it does not know
                   train_workflow=os.environ.get("CI_KIT_TRAIN_WORKFLOW", "").strip(),
                   train_min_batch=int(os.environ.get("CI_KIT_TRAIN_MIN_BATCH", "").strip() or train_kick.MIN_BATCH),
                   train_stale_min=float(os.environ.get("CI_KIT_TRAIN_STALE_MIN", "").strip() or 90),
                   cancel_doomed=os.environ.get("CI_KIT_CANCEL_DOOMED", "true").strip().lower() not in ("false", "0", "no"),
                   ladder=pr_ladder.Config.from_env(),
                   base_retry_cap=cap, base_retry_error=cap_error, base_retry_bots=bots,
                   base_retry_per_pass=per_pass,
                   # env vars, never flags: an older @v1 tool would refuse a flag it does not know
                   update_held=os.environ.get("CI_KIT_UPDATE_HELD", "").strip().lower() in ("1", "true"),
                   head_fail_fast=fail_fast, head_wait_s=head_wait_s(fail_fast),
                   event=os.environ.get("CI_KIT_EVENT", "").strip())
    summary: list[str] = []
    relock: list[int] = []
    healing: list[dict] = []
    later: list[Callable[[list[str]], None]] = []
    try:
        # `later` goes to `run` whenever the held pass or the verdict wait is on -- and the wait is on
        # by DEFAULT (head fail-fast), so main hands it to every consumer; a double for `run` takes
        # it. Only with both off is the call the five-argument one
        rc = run(args.repo, summary, relock, opts, healing, later) if opts.update_held or opts.head_wait_s > 0 else \
            run(args.repo, summary, relock, opts, healing)
    except (subprocess.CalledProcessError, ValueError, KeyError) as e:
        # gh's stderr is the only place the cause is named (a token scope, a GraphQL limit);
        # without it the run says only "returned non-zero exit status 1" (2026-10-24)
        err = (getattr(e, "stderr", "") or "").strip()
        summary.append(f"DID NOT FINISH: {e}" + (f" -- gh said: {err[-500:]}" if err else ""))
        rc = 2
    text = "\n".join(f"- {s}" for s in summary)
    print(text)
    if os.environ.get("GITHUB_OUTPUT"):
        # the NEWLY labelled PRs: a caller may relock each (a lockfile-only conflict)
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"relock={json.dumps(relock)}\n")
            # the heal ladder's dispatches (ci_kit/pr_ladder.py): one pr-heal.yml call per row
            fh.write(f"heal={json.dumps(healing)}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write("### PR auto-update\n\n" + text + "\n")
    _run_later(later)
    return rc


def _run_later(later: list[Callable[[list[str]], None]]) -> None:
    """The held pass, AFTER relock= / heal= and the armed summary are written: its lines append to
    stdout and the step summary. Nothing queued (off, the default) = not one byte more."""
    tail: list[str] = []
    for step in later:
        step(tail)
    if not tail:
        return
    text = "\n".join(f"- {s}" for s in tail)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


if __name__ == "__main__":
    sys.exit(main())
