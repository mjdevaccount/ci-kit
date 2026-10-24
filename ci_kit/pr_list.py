"""The open-PR list the updater needs, read with a query that asks only for what it uses.

`gh pr list --json statusCheckRollup` selects each check run's workflow run too, which a token
without `actions: read` is refused -- and GitHub refuses a fine-grained PAT the check runs at all
("Resource not accessible by personal access token", live 2026-10-24 on every caller with an
open PR). This query asks a check run for its name/status/conclusion and a commit status for its
context/state, nothing more, and returns rows shaped like `gh pr list --json` so pr_queue and
pr_autoupdate read them unchanged.
"""
from __future__ import annotations

QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 100, orderBy: {field: CREATED_AT, direction: ASC}) {
      nodes {
        number title url isCrossRepository headRefOid headRefName mergeStateStatus
        autoMergeRequest { enabledAt }
        labels(first: 30) { nodes { name } }
        commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) { nodes {
          __typename
          ... on CheckRun { name status conclusion }
          ... on StatusContext { context state }
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
    if answer.get("errors"):
        raise ValueError(f"GraphQL errors: {answer['errors']}")
    out = []
    for n in answer["data"]["repository"]["pullRequests"]["nodes"]:
        commits = (n.get("commits") or {}).get("nodes") or []
        rollup = (commits[0]["commit"].get("statusCheckRollup") or {}) if commits else {}
        out.append({
            **{k: n.get(k) for k in ("number", "title", "url", "isCrossRepository", "headRefOid",
                                     "headRefName", "mergeStateStatus", "autoMergeRequest")},
            "labels": (n.get("labels") or {}).get("nodes") or [],
            "statusCheckRollup": ((rollup.get("contexts") or {}).get("nodes")) or [],
        })
    return out
