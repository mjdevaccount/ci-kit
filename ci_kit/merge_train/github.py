"""The GitHub reads the train and the review gate's train path share: a PR, and its review verdict.

REVIEW PASSED AT THIS EXACT HEAD = the caller's review-gate workflow (the file the gate itself
runs as: `github.workflow_ref` in the gate, `--review-workflow` for the train) has a COMPLETED
`pull_request` run whose head is that sha and whose PR is that number, and the NEWEST such run
concluded `success`. That is every pass the gate can give -- a model SHIP / SHIP WITH NOTED RISK,
a carried verdict, a docs-only or version-bump skip -- and no block or BLIND (the Gate step fails
the job, so the run fails). A newer run at the same head that failed (a re-run that blocked)
outvotes an older pass; a run still in progress at that head is not a pass yet.

Why runs and not the `review-verdict-<pr>` artifact the carry reads: the record job writes one
only for a full or carried review, never for a docs-only or bump skip, so a train of a docs PR
would read as unreviewed. A workflow run can only be created by GitHub Actions; a PR comment can
be edited by any token holder, which is why the comment is never read. The residual is the
gate's own: a PR that edits the caller's review-gate stanza is graded by its edit
(ci-kit review-gate.yml header).
"""
from __future__ import annotations

import json
import subprocess
import urllib.parse


def gh(*args: str, check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], check=check, capture_output=True, text=True, encoding="utf-8",
                          input=input_text)


def gh_json(*args: str) -> object:
    return json.loads(gh(*args).stdout or "null")


def pull(repo: str, number: int) -> dict:
    """The REST pull request (state, head.sha/ref/repo, title, body, merged, labels)."""
    got = gh_json("api", f"repos/{repo}/pulls/{number}")
    if not isinstance(got, dict):
        raise ValueError(f"#{number}: no pull request answer")
    return got


def runs_at(repo: str, workflow: str, head: str) -> list[dict]:
    q = urllib.parse.urlencode({"head_sha": head, "event": "pull_request", "per_page": 50})
    got = gh_json("api", f"repos/{repo}/actions/workflows/{urllib.parse.quote(workflow, safe='')}/runs?{q}")
    return list((got or {}).get("workflow_runs") or []) if isinstance(got, dict) else []


def verdict(runs: list[dict], number: int, head: str, head_ref: str) -> tuple[bool, str]:
    """PURE: the policy above over the runs GitHub returned for `head`. A run's `pull_requests` is
    empty in some GitHub answers; then the run must be on the PR's own head branch."""
    mine = []
    for r in runs:
        if r.get("head_sha") != head or r.get("event") != "pull_request":
            continue
        prs = r.get("pull_requests") or []
        if prs:
            if not any(p.get("number") == number and (p.get("head") or {}).get("sha") == head for p in prs):
                continue
        elif not head_ref or r.get("head_branch") != head_ref:
            continue
        mine.append(r)
    if not mine:
        return False, "no review-gate run at this head"
    newest = max(mine, key=lambda r: (str(r.get("created_at") or ""), int(r.get("id") or 0)))
    url = newest.get("html_url") or f"run {newest.get('id')}"
    if newest.get("status") != "completed":
        return False, f"the review gate is still {newest.get('status')} at this head ({url})"
    if newest.get("conclusion") != "success":
        return False, f"the review gate concluded {newest.get('conclusion')} at this head ({url})"
    return True, f"review gate passed at this head ({url})"


def review_passed(repo: str, workflow: str, number: int, head: str,
                  pr: dict | None = None) -> tuple[bool, str]:
    """(passed, why) for PR `number` at `head`; never raises for an answer, only for no answer."""
    pr = pr if pr is not None else pull(repo, number)
    if ((pr.get("head") or {}).get("repo") or {}).get("full_name") != repo:
        return False, "not a same-repository PR"
    if pr.get("state") != "open":
        return False, f"the PR is {pr.get('state')}"
    return verdict(runs_at(repo, workflow, head), number, head, str((pr.get("head") or {}).get("ref") or ""))
