"""The narrow open-PR query's answer is reshaped like `gh pr list --json` (what pr_queue reads)."""
from __future__ import annotations

import pytest

from ci_kit import pr_list, pr_queue


def _answer(contexts: list[dict]) -> dict:
    return {"data": {"repository": {"pullRequests": {"nodes": [{
        "number": 7, "title": "t", "url": "u", "isCrossRepository": False, "headRefOid": "abc",
        "headRefName": "b", "mergeStateStatus": "BEHIND", "autoMergeRequest": {"enabledAt": "2026-10-24T00:00:00Z"},
        "labels": {"nodes": [{"name": "conflict"}]},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {"nodes": contexts}}}}]},
    }]}}}}


def test_rows_carry_what_the_queue_reads() -> None:
    row = pr_list.rows(_answer([
        {"__typename": "CheckRun", "name": "unit tests", "status": "COMPLETED", "conclusion": "FAILURE"},
        {"__typename": "StatusContext", "context": "ci/x", "state": "SUCCESS"},
    ]))[0]
    assert row["labels"] == [{"name": "conflict"}]
    assert row["autoMergeRequest"]["enabledAt"]
    assert pr_queue.check_state(row["statusCheckRollup"]) == "failing"
    assert pr_queue.check_state(row["statusCheckRollup"], ("ci/x",)) == "green"


def test_a_head_with_no_checks_has_an_empty_rollup() -> None:
    a = _answer([])
    a["data"]["repository"]["pullRequests"]["nodes"][0]["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = None
    assert pr_list.rows(a)[0]["statusCheckRollup"] == []


def test_graphql_errors_are_not_an_empty_list() -> None:
    # an answer with errors and no PRs must not read as "0 open": that is "did not look"
    with pytest.raises(ValueError):
        pr_list.rows({"errors": [{"message": "Resource not accessible"}], "data": None})


def test_the_query_never_asks_for_a_workflow_run() -> None:
    # workflowRun / checkSuite need `actions: read`; the whole point is not to ask
    assert "workflowRun" not in pr_list.QUERY and "checkSuite" not in pr_list.QUERY
