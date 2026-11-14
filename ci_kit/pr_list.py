"""The open-PR list the updater needs, read with a query that asks only for what it uses.

`gh pr list --json statusCheckRollup` selects each check run's workflow run too, which a token
without `actions: read` is refused -- and GitHub refuses a fine-grained PAT the check runs at all
("Resource not accessible by personal access token", live 2026-10-24 on every caller with an
open PR). This query asks a check run for its name/status/conclusion and a commit status for its
context/state, nothing more, and returns rows shaped like `gh pr list --json` so pr_queue,
pr_autoupdate and the merge train (ci_kit/merge_train) read them unchanged.
"""
from __future__ import annotations

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
        commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) { nodes {
          __typename
          ... on CheckRun { name status conclusion startedAt completedAt }
          ... on StatusContext { context state createdAt }
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
        })
    return out
