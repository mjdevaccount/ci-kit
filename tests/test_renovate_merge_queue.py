"""In a repo with a merge queue Renovate never merges by itself, and `autoland-off` stops it everywhere.

trade-data#2995 (2026-11-12 03:21:47Z) was merged BY RENOVATE two minutes after a session disarmed
it (`gh pr merge --disable-auto`) and labelled it `autoland-off`: Renovate's own merge ignores
GitHub's auto-merge state, the label and the queue's landing order. `platformAutomerge` does not
stop it (Renovate still calls mergePr itself when green), so the merge step there is `pr-comment`.
"""
from __future__ import annotations

import pathlib

import json5

PRESET = pathlib.Path(__file__).resolve().parent.parent / "renovate" / "default.json5"
# every repo whose default branch runs ci-kit's pr-autoupdate with max-in-flight (the serial queue)
QUEUE_REPOS = {f"mjdevaccount/{r}" for r in (
    "trade-data", "trade-infra", "trade-ui", "edge-host", "home-host", "TradingEdge.Scraper",
    "claude-plugins", "local-mcp", "mattpc-llm-stack", "cred-broker", "edge-ci", "ci-kit")}


def _cfg() -> dict:
    return json5.loads(PRESET.read_text(encoding="utf-8"))


def test_autoland_off_is_renovates_stop_label_must_fire() -> None:
    assert _cfg()["stopUpdatingLabel"] == "autoland-off"


def test_renovate_never_merges_in_a_merge_queue_repo_must_fire() -> None:
    rules = _cfg()["packageRules"]
    queue = [(i, r) for i, r in enumerate(rules) if r.get("matchRepositories") and "automergeType" in r]
    assert len(queue) == 1, "ONE merge-queue rule"
    at, rule = queue[0]
    assert set(rule["matchRepositories"]) == QUEUE_REPOS
    # the merge step only comments; automerge itself is NOT turned off, so the PR body still says
    # "**Automerge**: Enabled", which is what ci_kit/pr_renovate.py reads before it arms the PR
    assert rule["automergeType"] == "pr-comment" and rule["automergeComment"].strip()
    assert "automerge" not in rule
    # Renovate is not a second updater: a branch made current with main by Renovate's own rebase
    # landed past the queue's in-flight PR (trade-data#3072, 2026-11-14). Only a conflict rebases.
    assert rule["rebaseWhen"] == "conflicted"
    # repository is its ONLY matcher, and nothing after it sets the merge step back
    assert not [k for k in rule if k.startswith("match") and k != "matchRepositories"]
    later = [r for r in rules[at + 1:] if "automergeType" in r or "platformAutomerge" in r or "rebaseWhen" in r]
    assert later == [_kit_rule()], "only the kit rule may follow, and it narrows rebaseWhen to never"


def _kit_rule() -> dict:
    kit = [r for r in _cfg()["packageRules"] if r.get("rebaseWhen") == "never"]
    assert len(kit) == 1, "ONE never-rebase rule"
    return kit[0]


def test_a_kit_branch_in_a_queue_repo_is_pushed_once_must_fire() -> None:
    # trade-data renovate/ci-kit, 11-13: 194 CI runs / 469 job-min, ~5/6 of it Renovate re-pushing an
    # already-pushed version over the relock commit and the relock re-running. `never` is the only
    # rebaseWhen that skips Renovate's content check on an existing branch. Same repos as the queue
    # rule (no other updater elsewhere), first-party sources only (a third-party bump keeps
    # `conflicted`), and it sets nothing else. A conflict is healed by ci_kit/pr_renovate.py ticking
    # the PR's rebase box (tests/test_pr_renovate_must_fire.py).
    rule = _kit_rule()
    assert set(rule["matchRepositories"]) == QUEUE_REPOS
    assert rule["matchSourceUrls"] == ["https://github.com/mjdevaccount/**"]
    assert set(rule) == {"description", "matchRepositories", "matchSourceUrls", "rebaseWhen"}
    assert _cfg()["packageRules"][-1] == rule, "last, so no later rule widens it back"


def test_python_updates_group_by_datasource_not_manager_must_fire() -> None:
    # trade-portfolio 2026-11-14: a lock-less `[project]` pyproject is parsed by `poetry`, so a group
    # named by manager (pep621) never formed -- 12 PRs at once. renovate 44 dry run: 6 branches -> 2.
    group = [r for r in _cfg()["packageRules"] if r.get("groupName") == "python (non-major)"]
    assert len(group) == 1 and group[0]["matchDatasources"] == ["pypi"]
    assert "matchManagers" not in group[0] and group[0]["matchUpdateTypes"] == ["minor", "patch"]
    assert group[0]["groupSlug"] == "python-non-major"


def test_every_other_repo_keeps_the_every_check_merge() -> None:
    # the header's rule: GitHub's own auto-merge waits on REQUIRED checks only, so elsewhere
    # Renovate merges itself once EVERY check is green
    cfg = _cfg()
    assert cfg["platformAutomerge"] is False
    assert not [r for r in cfg["packageRules"] if "platformAutomerge" in r]
