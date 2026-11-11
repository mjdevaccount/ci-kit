"""The GitHub reads the train and the review gate's train path share: a PR, its review verdict, and
whether a train PR is one this workflow composed.

REVIEW PASSED AT THIS EXACT HEAD, ON THIS BASE = the caller's review-gate workflow (the file the
gate itself runs as: `github.workflow_ref` in the gate, `--review-workflow` for the train) has a
COMPLETED `pull_request` run whose head is that sha and whose `pull_requests` names exactly that
PR at that head, the NEWEST such run concluded `success`, the PR is based on the train's base
branch NOW, and that run was CREATED AFTER THE PR'S LAST BASE CHANGE.

That last clause is the base binding (session review of ci-kit#70, finding 1, round 2). A run's
`pull_requests[].base` (and `.head`) are the PR's CURRENT state when the API is asked, not what the
run saw -- live: trade-data run 43105767267 (created 21:44:53) reports a base sha committed
21:55:29 -- so no field of the run can say which base was reviewed. A PR reviewed on `feat` and
retargeted to main (`edited` is not a gate trigger, so no new run) would read as reviewed on
main and land `feat`'s unreviewed content. The PR's timeline records each base change when it
HAPPENS (`BaseRefChangedEvent.createdAt`, GraphQL): a run created after the last one reviewed the
current base. A PR never retargeted has none. A timeline GitHub will not answer RAISES -- the gate
reports BLIND, the train's pass does not finish -- never a pass, and never an ejection.

REFUSED, never guessed: a run whose `pull_requests` is EMPTY (GitHub leaves it empty in some
answers; matching it by head branch name instead would accept another PR's run on a reused
branch), and a head shared by two PRs (the run cannot say which PR it reviewed).

Why runs and not the `review-verdict-<pr>` artifact the carry reads: the record job writes one
only for a full or carried review, never for a docs-only or bump skip, so a train of a docs PR
would read as unreviewed. A workflow run can only be created by GitHub Actions; a PR comment can
be edited by any token holder, which is why the comment is never read for a verdict.

COMPOSED BY THE TRAIN. A train PR counts only if its body carries the composition record
`<!-- merge-train:composed run=<id> branch=<head ref> by=<login> -->` AND that record holds against
GitHub: `run` is a run of the caller's merge-train workflow in this repository, the PR's head ref is
`branch` = `train/<run>[-<attempt>]`, the PR was opened while that run was alive, and its author is
`by`. The CLI additionally requires `by` to be its own token's login before it lands anything. A
`train/` PR anyone else opened is refused by both: the CLI never merges it, the gate blocks it.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import urllib.parse

COMPOSED = re.compile(r"<!-- merge-train:composed run=(\d+) branch=(\S+) by=(\S+) -->")
SLACK = dt.timedelta(minutes=2)


def gh(*args: str, check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], check=check, capture_output=True, text=True, encoding="utf-8",
                          input=input_text)


def gh_json(*args: str) -> object:
    return json.loads(gh(*args).stdout or "null")


def pull(repo: str, number: int) -> dict:
    """The REST pull request (state, draft, auto_merge, head/base, user, labels, title, body)."""
    got = gh_json("api", f"repos/{repo}/pulls/{number}")
    if not isinstance(got, dict):
        raise ValueError(f"#{number}: no pull request answer")
    return got


def runs_at(repo: str, workflow: str, head: str) -> list[dict]:
    q = urllib.parse.urlencode({"head_sha": head, "event": "pull_request", "per_page": 50})
    got = gh_json("api", f"repos/{repo}/actions/workflows/{urllib.parse.quote(workflow, safe='')}/runs?{q}")
    return list((got or {}).get("workflow_runs") or []) if isinstance(got, dict) else []


RETARGETS = """query($o:String!,$n:String!,$pr:Int!){repository(owner:$o,name:$n){pullRequest(number:$pr){
 timelineItems(itemTypes:[BASE_REF_CHANGED_EVENT],last:1){totalCount nodes{... on BaseRefChangedEvent{createdAt}}}}}}"""


def retargeted_at(repo: str, number: int) -> str:
    """When PR #number's base branch last changed ('' = never). RAISES when GitHub cannot say."""
    owner, name = repo.split("/", 1)
    got = gh_json("api", "graphql", "-f", f"query={RETARGETS}", "-f", f"o={owner}", "-f", f"n={name}",
                  "-F", f"pr={number}")
    pr = ((((got or {}) if isinstance(got, dict) else {}).get("data") or {}).get("repository") or {}).get("pullRequest")
    items = pr.get("timelineItems") if isinstance(pr, dict) else None
    if not isinstance(items, dict):
        raise ValueError(f"#{number}: GitHub returned no base-change timeline")
    nodes = [x for x in items.get("nodes") or [] if isinstance(x, dict)]
    if items.get("totalCount") and not (nodes and _ts(nodes[-1].get("createdAt"))):
        raise ValueError(f"#{number}: the base-change timeline names a change with no time")
    return str(nodes[-1]["createdAt"]) if items.get("totalCount") else ""


def verdict(runs: list[dict], number: int, head: str, retargeted: str = "") -> tuple[bool, str, str]:
    """PURE: (passed, why, the passing run's created_at) under the policy above for the runs at
    `head`; `retargeted` = when the PR's base last changed ('' = never)."""
    mine: list[dict] = []
    others: set[int] = set()
    for r in runs:
        if r.get("head_sha") != head or r.get("event") != "pull_request":
            continue
        at_head = [p for p in r.get("pull_requests") or [] if (p.get("head") or {}).get("sha") == head]
        others |= {int(p.get("number") or 0) for p in at_head if p.get("number") != number}
        if any(p.get("number") == number for p in at_head):
            mine.append(r)
    if others:
        return False, f"this head is also the head of #{', #'.join(map(str, sorted(others)))}: ambiguous", ""
    if not mine:
        return False, "no review-gate run names this PR at this head", ""
    newest = max(mine, key=lambda r: (str(r.get("created_at") or ""), int(r.get("id") or 0)))
    url = newest.get("html_url") or f"run {newest.get('id')}"
    if newest.get("status") != "completed":
        return False, f"the review gate is still {newest.get('status')} at this head ({url})", ""
    if newest.get("conclusion") != "success":
        return False, f"the review gate concluded {newest.get('conclusion')} at this head ({url})", ""
    made = _ts(newest.get("created_at"))
    if made is None:
        return False, f"the review run records no creation time ({url})", ""
    if retargeted:
        moved = _ts(retargeted)
        if moved is None or made <= moved:
            return False, (f"its base branch was changed at {retargeted}, after its last review run ({url}, "
                           f"{newest.get('created_at')}): that review read another base"), ""
    return True, f"review gate passed at this head ({url})", str(newest.get("created_at"))


def review_passed(repo: str, workflow: str, number: int, head: str, base_ref: str,
                  pr: dict | None = None) -> tuple[bool, str, str]:
    """(passed, why, the passing run's created_at) for PR `number` at `head` on `base_ref`.
    Raises when GitHub cannot answer (runs or timeline)."""
    pr = pr if pr is not None else pull(repo, number)
    if ((pr.get("head") or {}).get("repo") or {}).get("full_name") != repo:
        return False, "not a same-repository PR", ""
    if pr.get("state") != "open":
        return False, f"the PR is {pr.get('state')}", ""
    if (pr.get("base") or {}).get("ref") != base_ref:
        return False, f"the PR is based on `{(pr.get('base') or {}).get('ref')}`, not `{base_ref}`", ""
    return verdict(runs_at(repo, workflow, head), number, head, retargeted_at(repo, number))


def _ts(s: object) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def composed_ok(repo: str, pr: dict, run: dict | None, train_workflow: str) -> str:
    """PURE: '' when REST PR `pr` is a train its record and `run` (the REST run it names) vouch
    for, else why not."""
    m = COMPOSED.search(str(pr.get("body") or ""))
    if not m:
        return "no composition record: not a train this workflow composed"
    run_id, branch, by = m.groups()
    ref = str((pr.get("head") or {}).get("ref") or "")
    if ref != branch or not re.fullmatch(rf"train/{run_id}(-\d+)?", branch):
        return f"head `{ref}` is not the record's train branch for run {run_id}"
    if str((pr.get("user") or {}).get("login") or "") != by:
        return f"opened by {(pr.get('user') or {}).get('login')}, the record says {by}"
    if not isinstance(run, dict) or str(run.get("id")) != run_id:
        return f"run {run_id} does not exist"
    if ((run.get("repository") or {}).get("full_name")) != repo:
        return f"run {run_id} is not this repository's"
    if str(run.get("path") or "").split("@")[0].rsplit("/", 1)[-1] != train_workflow.rsplit("/", 1)[-1]:
        return f"run {run_id} is `{run.get('path')}`, not the merge-train workflow `{train_workflow}`"
    opened, start, end = _ts(pr.get("created_at")), _ts(run.get("created_at")), _ts(run.get("updated_at"))
    if run.get("status") != "completed":
        end = opened  # still running (the gate fires on the PR's open, mid-pass): no upper bound yet
    if not (opened and start and end and start - SLACK <= opened <= end + SLACK):
        return f"opened at {pr.get('created_at')}, outside run {run_id} ({run.get('created_at')}..{run.get('updated_at')})"
    return ""


def composed(repo: str, pr: dict, train_workflow: str) -> str:
    """composed_ok over GitHub's answer for the run the record names."""
    m = COMPOSED.search(str(pr.get("body") or ""))
    run = None
    if m:
        r = gh("api", f"repos/{repo}/actions/runs/{m.group(1)}", check=False)
        run = json.loads(r.stdout or "null") if r.returncode == 0 else None
    return composed_ok(repo, pr, run if isinstance(run, dict) else None, train_workflow)
