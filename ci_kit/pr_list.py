"""The open-PR list the updater needs, read with a query that asks only for what it uses.

`gh pr list --json statusCheckRollup` selects each check run's workflow run too, which a token
without `actions: read` is refused -- and GitHub refuses a fine-grained PAT the check runs at all
("Resource not accessible by personal access token", live 2026-10-24 on every caller with an
open PR). This query asks a check run for its name/status/conclusion and a commit status for its
context/state/description (the description: the queue hold's lane handoff, v1.35.0), and the head
commit its `committedDate` (pr_held's young-head guard), nothing more,
and returns rows shaped like `gh pr list --json` so pr_queue,
pr_autoupdate and the merge train (ci_kit/merge_train) read them unchanged.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable

# UNKNOWN IS NOT BEHIND (2026-11-23). GitHub computes mergeStateStatus lazily: right after a push
# to the base every open PR reads UNKNOWN until something asks, and a read is the ask. The updater
# fires ON that push, and pr_queue treats UNKNOWN like BEHIND (eligible for an update) -- right
# under strict protection, where it IS behind, but in a repo without strict GitHub never reports
# BEHIND at all (probed live: plans#134, behind_by=2 with every required check green read CLEAN),
# so an UNKNOWN read there merged main into a PR that could already land and threw its CI away.
# settle() re-reads, bounded, while an ARMED PR is still UNKNOWN; whatever is left is said in the
# summary and planned as before. UNKNOWN_TRIES = 0 is off (the tests' default, conftest).
UNKNOWN_TRIES = int(os.environ.get("CI_KIT_UNKNOWN_TRIES", "2"))
UNKNOWN_WAIT_S = float(os.environ.get("CI_KIT_UNKNOWN_WAIT_S", "5"))

QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 100, orderBy: {field: CREATED_AT, direction: ASC}) {
      nodes {
        id number title url isCrossRepository headRefOid headRefName mergeStateStatus
        isDraft mergeable baseRefName createdAt
        author { __typename login }
        autoMergeRequest { enabledAt }
        body
        labels(first: 30) { nodes { name } }
        commits(last: 1) { nodes { commit { committedDate statusCheckRollup { contexts(first: 100) { nodes {
          __typename
          ... on CheckRun { name status conclusion startedAt completedAt }
          ... on StatusContext { context state description createdAt }
        } } } } } }
      }
    }
  }
}
"""


def gh_args(repo: str) -> list[str]:
    owner, name = repo.split("/", 1)
    return ["api", "graphql", "-f", f"query={QUERY}", "-f", f"owner={owner}", "-f", f"name={name}"]


def rows(answer: dict) -> list[dict]:
    """GraphQL answer -> `gh pr list --json`-shaped rows. Pure: the shape the tests pin."""
    from ci_kit.pr_renovate import automerge_enabled  # here: an import cycle at load
    if answer.get("errors"):
        raise ValueError(f"GraphQL errors: {answer['errors']}")
    out = []
    for n in answer["data"]["repository"]["pullRequests"]["nodes"]:
        commits = (n.get("commits") or {}).get("nodes") or []
        rollup = (commits[0]["commit"].get("statusCheckRollup") or {}) if commits else {}
        committed = commits[0]["commit"].get("committedDate") if commits else None
        out.append({
            **{k: n.get(k) for k in ("id", "number", "title", "url", "isCrossRepository", "headRefOid",
                                     "headRefName", "mergeStateStatus", "autoMergeRequest",
                                     "isDraft", "mergeable", "baseRefName", "createdAt", "author")},
            "labels": (n.get("labels") or {}).get("nodes") or [],
            "author": n.get("author") or {},
            # Renovate merges its own PRs (platformAutomerge off), so in a repo with no queue they are
            # never ARMED; its config block says whether it will (pr_renovate's reader, never a first
            # match: changelogs come first). Only the flag travels: bodies stay out of the rows.
            "renovateAutomerge": automerge_enabled(n.get("body") or ""),
            "statusCheckRollup": ((rollup.get("contexts") or {}).get("nodes")) or [],
            # the head commit's date: pr_held skips a head too young to be finished (someone pushing)
            "headCommittedDate": committed,
        })
    return out


def unknown(prs: list[dict]) -> list[int]:
    """Armed same-repo PRs whose merge state GitHub has not computed (absent reads as UNKNOWN)."""
    return [int(p["number"]) for p in prs if p.get("autoMergeRequest") and not p.get("isCrossRepository")
            and (p.get("mergeStateStatus") or "UNKNOWN").upper() == "UNKNOWN"]


def settle(read: Callable[[], list[dict]], tries: int | None = None, wait_s: float | None = None,
           sleep: Callable[[float], None] = time.sleep) -> tuple[list[dict], list[int]]:
    """read(), then re-read up to `tries` times, `wait_s` apart, while an armed PR is UNKNOWN.
    Returns (rows, the armed PR numbers still UNKNOWN after the last read)."""
    tries = UNKNOWN_TRIES if tries is None else tries
    wait_s = UNKNOWN_WAIT_S if wait_s is None else wait_s
    prs = read()
    for _ in range(max(0, tries)):
        if not unknown(prs):
            break
        sleep(wait_s)
        prs = read()
    return prs, unknown(prs)
