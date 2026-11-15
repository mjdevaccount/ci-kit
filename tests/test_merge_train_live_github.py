"""LIVE probe: github.retargeted_at against the REAL GraphQL API (ci-kit#70 round 3).

Hand-written GitHub shapes hid the totalCount bug twice, so this asks GitHub itself. It runs in
ci-kit CI (`timeline-probe` job, MERGE_TRAIN_LIVE=1) with the job's own GITHUB_TOKEN scoped exactly
like the review-gate job (contents: read, pull-requests: write, actions: read) -- so a green run
also answers "can the gate's token read timelineItems". It skips anywhere without that switch.

The CI token reads only ci-kit and public repos, so the always-on pair is public:
astral-sh/uv#23126 (merged, retargeted once) and ci-kit#69 (merged, never retargeted; a merged PR's
base cannot change again). Private PRs run where a token can read them: MERGE_TRAIN_LIVE_PRIVATE is a comma-separated list of
`owner/repo#N@<retargeted-at ISO time, or empty>` (unset = skipped), so no private name lives here.
"""
from __future__ import annotations

import os

import pytest

from ci_kit.merge_train import github

pytestmark = pytest.mark.skipif(os.environ.get("MERGE_TRAIN_LIVE") != "1" or not (
    os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or os.environ.get("MERGE_TRAIN_LIVE_GH_AUTH")),
    reason="live GitHub probe: set MERGE_TRAIN_LIVE=1 with a token (ci-kit CI does)")

PUBLIC = [("astral-sh/uv", 23126, "2026-11-09T10:42:27Z"), ("mjdevaccount/ci-kit", 69, "")]
def _private(spec: str) -> list[tuple[str, int, str]]:
    """`owner/repo#N@when,...` -> [(repo, N, when)]; '' = none."""
    out = []
    for item in filter(None, (x.strip() for x in spec.split(","))):
        pr, _, when = item.partition("@")
        repo, _, number = pr.partition("#")
        if repo and number.isdigit():   # anything else (the old `=1` switch) names no PR
            out.append((repo, int(number), when))
    return out


PRIVATE = _private(os.environ.get("MERGE_TRAIN_LIVE_PRIVATE", ""))


def _raw(repo: str, number: int) -> dict:
    owner, name = repo.split("/", 1)
    q = ("query($o:String!,$n:String!,$pr:Int!){repository(owner:$o,name:$n){pullRequest(number:$pr){"
         "timelineItems(itemTypes:[BASE_REF_CHANGED_EVENT]){totalCount filteredCount}}}}")
    got = github.gh_json("api", "graphql", "-f", f"query={q}", "-f", f"o={owner}", "-f", f"n={name}",
                         "-F", f"pr={number}")
    return got["data"]["repository"]["pullRequest"]["timelineItems"]  # type: ignore[index]


@pytest.mark.parametrize(("repo", "number", "when"), PUBLIC)
def test_live_timeline_answers_and_the_total_count_trap_is_real(repo: str, number: int, when: str) -> None:
    assert github.retargeted_at(repo, number) == when
    raw = _raw(repo, number)
    assert raw["totalCount"] > raw["filteredCount"], raw     # totalCount ignores itemTypes: never decide on it
    assert raw["filteredCount"] == (1 if when else 0)


@pytest.mark.skipif(not PRIVATE, reason="private PRs: set MERGE_TRAIN_LIVE_PRIVATE with a token that reads them")
@pytest.mark.parametrize(("repo", "number", "when"), PRIVATE)
def test_live_timeline_on_private_prs(repo: str, number: int, when: str) -> None:
    assert github.retargeted_at(repo, number) == when
