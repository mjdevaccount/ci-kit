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
behind it: ci_kit/pr_queue.py). A PR the queue picked but skipped for a running review hands its
slot to the next eligible PR (`Plan.spare`), so one long review does not idle the queue.

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
import urllib.parse
from dataclasses import dataclass

from ci_kit import pr_heal, pr_hold, pr_list, pr_queue, pr_sched, train_kick
from ci_kit.merge_train import EJECTED, NEEDS_UPDATE, RIDING, TRAIN_PREFIX
from ci_kit.ntfy import page  # noqa: F401 -- re-exported: _update_one pages through this name

LABEL = "conflict"


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
    return subprocess.run(["gh", *args], check=check, capture_output=True, text=True, env=env)


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

    Live 2026-10-30: home-host had no `conflict` label, so every add failed and the run went red
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
                machinery: frozenset[int] = frozenset()) -> pr_queue.Plan:
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
    with no train sees exactly the old plan."""
    trains = [p for p in prs if str(p.get("headRefName") or "").startswith(TRAIN_PREFIX)
              and not p.get("isCrossRepository")]
    riding = {p["number"] for p in armed if any(lb.get("name") == RIDING for lb in p.get("labels") or [])}
    if riding:
        summary.append(f"riding a merge train, not updated: {', '.join('#' + str(n) for n in sorted(riding))}")
    q = pr_queue.plan([p for p in armed if p not in trains and p["number"] not in riding],
                      opts.max_in_flight, opts.required, machinery)
    if trains:
        held = [p for p in q.update if (p.get("mergeStateStatus") or "").upper() != "DIRTY"
                and not any(lb.get("name") in (EJECTED, NEEDS_UPDATE) for lb in p.get("labels") or [])]
        q.update = [p for p in q.update if p not in held]
        # spare holds only eligible PRs, never DIRTY: with a train open only an EJECTED or
        # train-needs-update one may take a slot. Each slot the hold just emptied goes to the next
        # of those, so an ordinary lower-numbered PR held for the train cannot idle the slot an
        # ejected PR needs (td#2980's idle slot, train edition -- review of 1.26.1, finding 1).
        q.spare = [p for p in q.spare
                   if any(lb.get("name") in (EJECTED, NEEDS_UPDATE) for lb in p.get("labels") or [])]
        promoted, q.spare = q.spare[:len(held)], q.spare[len(held):]
        q.update += promoted
        q.held = [(p, why) for p, why in q.held if p not in promoted]
        q.held += [(p, f"merge train #{trains[-1]['number']} is open") for p in held]
        summary.append(f"merge train #{trains[-1]['number']} open: {len(held)} update(s) held, conflicts still checked")
    return q


def run(repo: str, summary: list[str], relock: list[int], opts: Options) -> int:
    prs = pr_list.rows(json.loads(_gh_read(*pr_list.gh_args(repo)).stdout))
    armed = [p for p in prs if p.get("autoMergeRequest") and not p.get("isCrossRepository")]
    summary.append(f"{len(armed)} armed PR(s) of {len(prs)} open")
    # a PARKED PR (the scheduler cancelled or deferred its run) is eligible like a machinery red:
    # nothing in it is the author's to fix, and the update is its fresh run
    parked = frozenset(int(p["number"]) for p in prs if _has(p, pr_sched.PARKED))
    base = pr_hold.default_branch(repo, _gh_read) if opts.hold or opts.scarce else None
    try:  # reads only; the hold (the head's `queue/position` success) runs whatever happens here
        heal, errors = pr_heal.assess_armed(repo, armed, opts.required, opts.rerun_limit, _gh_read,
                                            pr_heal.comments_of, summary)
    except Exception:  # noqa: BLE001 -- whatever it was, the head gets its success, then it re-raises
        if opts.hold:
            pr_hold.hold(repo, prs, _gh_read, pr_heal.write, summary, pr_heal.comments_of,
                         pr_queue.blocked(prs, opts.required, parked, base), base=base,
                         stale_s=opts.head_stall_min * 120, required=opts.required)
        raise
    machinery = pr_heal.machinery(heal)
    # ONE head for the hold, the updater and the scheduler: the lowest armed PR that can land
    skip = pr_queue.blocked(prs, opts.required, machinery | parked, base)
    if opts.hold:
        errors += pr_hold.hold(repo, prs, _gh_read, pr_heal.write, summary, pr_heal.comments_of, skip,
                               base=base, stale_s=opts.head_stall_min * 120, required=opts.required)
    q = _train_plan(prs, armed, summary, opts, machinery | parked)
    head = _head_first(repo, prs, q, opts, summary, skip)
    if opts.max_in_flight > 0:
        summary.append(f"queue: {len(q.in_flight)}/{opts.max_in_flight} in flight "
                       f"[{', '.join('#' + str(p['number']) for p in q.in_flight)}], {len(q.update)} to attempt, "
                       f"{len(q.held)} held")
        summary += [f"#{p['number']}: in flight against current main" for p in q.in_flight]
        summary += [f"#{p['number']}: held, {why}" for p, why in q.held]
    tracker = ""
    if opts.tracker:
        tracker = _gh("issue", "view", str(opts.tracker), "-R", repo, "--json", "body", "-q",
                      ".body", check=False).stdout
    todo, spare, attempted, updated = list(q.update), list(q.spare), set(), set()
    while todo:
        p = todo.pop(0)
        n = p["number"]
        if n != head and reviewing(repo, p["headRefName"], opts.review_workflow):
            summary.append(f"#{n}: skipped, review gate running (retried when it completes)")
            if (p.get("mergeStateStatus") or "").upper() != "DIRTY" and spare:
                # the slot it would have taken is free: the next PR in line takes it, so one PR's
                # running review does not idle the queue (td#2980, 2026-11-11: skipped 21:10-23:32Z
                # with 0/1 in flight while five armed PRs waited behind it)
                nxt = spare.pop(0)
                summary.append(f"#{nxt['number']}: takes the slot #{n}'s running review left free")
                todo.append(nxt)
            continue
        attempted.add(n)
        kind, line = _update_one(repo, p, opts, tracker, relock)
        updated |= {n} if kind == "updated" else set()
        errors += kind == "error"
        if line:
            summary.append(line)
    errors += pr_heal.act(repo, heal, updated, opts.rerun_limit, pr_heal.write, summary)
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
        train_base = base or pr_hold.default_branch(repo, _gh_read) or "main"
        errors += train_kick.kick(repo, opts.train_workflow, [p for p in prs if p["number"] not in updated],
                                  opts.required, train_base, _gh, summary, opts.train_min_batch)
    return 1 if errors else 0


def _has(p: dict, label: str) -> bool:
    return any(lb.get("name") == label for lb in p.get("labels") or [])


def _head_first(repo: str, prs: list[dict], q: pr_queue.Plan, opts: Options, summary: list[str],
                skip: frozenset[int] = frozenset()) -> int | None:
    """Update the queue HEAD the moment it is safe, ahead of the slot count: BEHIND main, armed, not
    red on its own checks, and no review run still owes a verdict (`pr_sched.review_settled`) -- the
    gate's tail is not a reason to wait (td#3073, 03:58Z 11-14: verdict recorded, update held for the
    tail job, then the sealed runner went to another PR). Returns the head's number when it was moved
    up (so the loop does not skip it for that tail), else None. Off unless the scheduler is on."""
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
    if not pr_sched.review_settled(repo, str(h["headRefName"]), _gh_read):
        summary.append(f"#{h['number']}: queue head, behind main -- its review has not recorded a verdict yet")
        return None
    if not queued:
        q.update.insert(0, h)
        q.held = [(p, w) for p, w in q.held if p is not h]
        q.spare = [p for p in q.spare if p is not h]
    summary.append(f"#{h['number']}: queue head, behind main, verdict settled -> updated first")
    return int(h["number"])


def _update_one(repo: str, p: dict, opts: Options, tracker: str, relock: list[int]) -> tuple[str, str]:
    """PUT update-branch for one PR and act on the answer: (outcome kind | 'gone', summary line)."""
    n = p["number"]
    labelled = any(lb.get("name") == opts.label for lb in p.get("labels") or [])
    r = _gh("api", "-X", "PUT", f"repos/{repo}/pulls/{n}/update-branch",
            "-f", f"expected_head_sha={p['headRefOid']}", check=False)
    o = classify(r.returncode, r.stdout or r.stderr)
    if o.kind in ("conflict", "error"):
        # live proof 2026-09-26: a PR merged between `pr list` and this PUT, and GitHub
        # answered "merge conflict" for a PR that no longer existed as open
        state = _gh("pr", "view", str(n), "-R", repo, "--json", "state", "-q", ".state", check=False)
        if state.returncode == 0 and state.stdout.strip() not in ("", "OPEN"):
            return "gone", f"#{n}: {state.stdout.strip().lower()} during the update, skipped"
    add, do_page, remove = actions_for(o, labelled)
    line = f"#{n}: {o.kind}" + (f" ({o.detail})" if o.detail else "")
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
    the label (td#2983, 2026-11-11: labelled 23:05Z, resolved 23:32Z, still labelled). `mergeable`
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
    opts = Options(review_workflow=args.review_workflow, tracker=args.tracker, relock=args.relock,
                   label=args.label, max_in_flight=args.max_in_flight,
                   required=tuple(s.strip() for s in args.required_checks.split(",") if s.strip()),
                   rerun_limit=args.rerun_limit,
                   scarce=scarce,
                   preempt_after_min=args.preempt_after_min, head_stall_min=args.head_stall_min,
                   before=os.environ.get("CI_KIT_BEFORE", ""),
                   sha=os.environ.get("CI_KIT_SHA", "") if os.environ.get("CI_KIT_EVENT") == "push" else "",
                   hold=os.environ.get("CI_KIT_QUEUE_HOLD", "").strip().lower() in ("1", "true"),
                   # env vars, never flags: an older @v1 tool would refuse a flag it does not know
                   train_workflow=os.environ.get("CI_KIT_TRAIN_WORKFLOW", "").strip(),
                   train_min_batch=int(os.environ.get("CI_KIT_TRAIN_MIN_BATCH", "").strip() or train_kick.MIN_BATCH))
    summary: list[str] = []
    relock: list[int] = []
    try:
        rc = run(args.repo, summary, relock, opts)
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
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write("### PR auto-update\n\n" + text + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())
