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
current main at once, oldest-armed first; 0 = every armed PR. The policy and the measurement
behind it: ci_kit/pr_queue.py).

CONFLICT = TRANSITION-ONLY PAGE. The conflict label is the dedup state: a PR that conflicts and
is not labelled gets the label AND one ntfy push (env `NTFY_URL`; unset = no push) naming the PR
and its tracker row; one already labelled gets neither again. A PR that updates cleanly loses the
label. Each NEWLY labelled PR is emitted as output `relock` for a caller that can relock a
lockfile-only conflict. Not a `labeled` trigger: GitHub runs no `pull_request` workflow on a PR
with a merge conflict, so that trigger could never fire. A label that cannot be added is an error
and NO page (the label is the dedup; without it every merge to main would page again).

Exit 0 = every armed PR handled; 1 = at least one update failed for a reason other than a
conflict (named in the log, red run); 2 = could not list the PRs at all (did not finish looking).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass

from ci_kit import pr_list, pr_queue

LABEL = "conflict"


@dataclass(frozen=True)
class Options:
    review_workflow: str = ""  # a workflow file whose queued/running run defers the update
    tracker: int = 0  # the issue whose row for a PR is quoted in the page; 0 = none
    relock: bool = False  # the caller relocks lockfile-only conflicts
    label: str = LABEL  # the conflict label (the page dedup)
    max_in_flight: int = 0  # PRs tested against current main at once (pr_queue); 0 = update every armed PR
    required: tuple[str, ...] = ()  # the check names that count for pr_queue; () = every check


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


def page(title: str, body: str, click: str) -> str:
    url = os.environ.get("NTFY_URL", "")
    if not url:
        return "NTFY_URL unset: NOT pushed"
    req = urllib.request.Request(url, data=body.encode(), method="POST", headers={
        # priority 3: a PR is never a reason to wake the operator
        "Title": title, "Priority": "3", "Tags": "twisted_rightwards_arrows", "Click": click})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return f"published ({r.status})"
    except OSError as e:
        return f"push FAILED: {e}"


def run(repo: str, summary: list[str], relock: list[int], opts: Options) -> int:
    prs = pr_list.rows(json.loads(_gh_read(*pr_list.gh_args(repo)).stdout))
    armed = [p for p in prs if p.get("autoMergeRequest") and not p.get("isCrossRepository")]
    summary.append(f"{len(armed)} armed PR(s) of {len(prs)} open")
    q = pr_queue.plan(armed, opts.max_in_flight, opts.required)
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
    errors = 0
    for p in q.update:
        n = p["number"]
        labelled = any(lb.get("name") == opts.label for lb in p.get("labels") or [])
        if reviewing(repo, p["headRefName"], opts.review_workflow):
            summary.append(f"#{n}: skipped, review gate running (retried when it completes)")
            continue
        r = _gh("api", "-X", "PUT", f"repos/{repo}/pulls/{n}/update-branch",
                "-f", f"expected_head_sha={p['headRefOid']}", check=False)
        o = classify(r.returncode, r.stdout or r.stderr)
        if o.kind in ("conflict", "error"):
            # live proof 2026-09-26: a PR merged between `pr list` and this PUT, and GitHub
            # answered "merge conflict" for a PR that no longer existed as open
            state = _gh("pr", "view", str(n), "-R", repo, "--json", "state", "-q", ".state", check=False)
            if state.returncode == 0 and state.stdout.strip() not in ("", "OPEN"):
                summary.append(f"#{n}: {state.stdout.strip().lower()} during the update, skipped")
                continue
        add, do_page, remove = actions_for(o, labelled)
        line = f"#{n}: {o.kind}" + (f" ({o.detail})" if o.detail else "")
        if add:
            if _gh("pr", "edit", str(n), "-R", repo, "--add-label", opts.label, check=False).returncode:
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
            errors += 1
            print(f"::error::#{n}: {o.detail}")
        summary.append(line)
    return 1 if errors else 0


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
                    help="armed PRs tested against current main at once, oldest-armed first (0: every armed PR)")
    ap.add_argument("--required-checks", default=os.environ.get("CI_KIT_REQUIRED_CHECKS", ""),
                    help="comma-separated check names that count for the queue ('' : every check on the head;"
                         " default: env CI_KIT_REQUIRED_CHECKS, since names carry spaces)")
    args = ap.parse_args(argv)
    opts = Options(review_workflow=args.review_workflow, tracker=args.tracker, relock=args.relock,
                   label=args.label, max_in_flight=args.max_in_flight,
                   required=tuple(s.strip() for s in args.required_checks.split(",") if s.strip()))
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
