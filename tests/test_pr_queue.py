"""The serial queue in front of strict protection: which armed PRs get updated this run."""
from __future__ import annotations

import json

import pytest

import ci_kit.pr_autoupdate as upd
from ci_kit.pr_autoupdate import Options
from ci_kit.pr_queue import check_state, plan

GREEN = {"__typename": "CheckRun", "name": "unit tests", "status": "COMPLETED", "conclusion": "SUCCESS"}
RUNNING = {"__typename": "CheckRun", "name": "unit tests", "status": "IN_PROGRESS", "conclusion": ""}
RED = {"__typename": "CheckRun", "name": "unit tests", "status": "COMPLETED", "conclusion": "FAILURE"}
OPTIONAL_RED = {"__typename": "CheckRun", "name": "nice to have", "status": "COMPLETED", "conclusion": "FAILURE"}


def _pr(n: int, merge: str, *checks: dict, armed_at: str = "") -> dict:
    return {"number": n, "mergeStateStatus": merge, "statusCheckRollup": list(checks),
            "autoMergeRequest": {"enabledAt": armed_at or f"2026-10-24T00:{n:02d}:00Z"}}


def _nums(prs: list) -> list[int]:
    return [p["number"] if isinstance(p, dict) else p[0]["number"] for p in prs]


def test_check_state() -> None:
    assert check_state([GREEN]) == "green"
    assert check_state([GREEN, RUNNING]) == "running"
    assert check_state([RUNNING, RED]) == "failing"
    assert check_state([{"__typename": "StatusContext", "context": "x", "state": "PENDING"}]) == "running"
    assert check_state([{"__typename": "StatusContext", "context": "x", "state": "ERROR"}]) == "failing"
    assert check_state([]) == "green"   # an ABSENT check neither runs nor fails


def test_required_narrows_the_checks_that_count() -> None:
    assert check_state([GREEN, OPTIONAL_RED]) == "failing"
    assert check_state([GREEN, OPTIONAL_RED], required=("unit tests",)) == "green"


def test_zero_is_the_old_behaviour_every_armed_pr() -> None:
    prs = [_pr(1, "BEHIND", RUNNING), _pr(2, "BEHIND", RED), _pr(3, "CLEAN", GREEN)]
    assert _nums(plan(prs, 0).update) == [1, 2, 3]


def test_one_slot_goes_to_the_oldest_green_behind_pr() -> None:
    prs = [_pr(3, "BEHIND", GREEN), _pr(2, "BEHIND", GREEN, armed_at="2026-10-23T09:00:00Z"), _pr(1, "BEHIND", GREEN)]
    q = plan(prs, 1)
    assert _nums(q.update) == [2]
    assert [why for _, why in q.held] == ["queued, position 1 (in flight: 0/1)", "queued, position 2 (in flight: 0/1)"]


def test_a_pr_in_flight_holds_the_slot() -> None:
    # under strict, the in-flight PR merging makes every other PR BEHIND again: updating one now is a wasted run
    q = plan([_pr(1, "BLOCKED", RUNNING), _pr(2, "BEHIND", GREEN)], 1)
    assert q.update == [] and _nums(q.in_flight) == [1] and _nums(q.held) == [2]
    q = plan([_pr(1, "CLEAN", GREEN), _pr(2, "BEHIND", GREEN)], 1)
    assert q.update == [] and _nums(q.in_flight) == [1]


def test_a_current_pr_that_is_red_does_not_hold_the_slot() -> None:
    q = plan([_pr(1, "BLOCKED", RED), _pr(2, "BEHIND", GREEN)], 1)
    assert _nums(q.update) == [2] and q.in_flight == []


def test_behind_and_red_or_running_is_held_not_updated() -> None:
    q = plan([_pr(1, "BEHIND", RED), _pr(2, "BEHIND", RUNNING), _pr(3, "UNKNOWN", GREEN)], 1)
    assert _nums(q.update) == [3]
    held = dict((p["number"], why) for p, why in q.held)
    assert "failing" in held[1] and "would cancel" in held[2]


def test_a_conflict_is_always_attempted_and_takes_no_slot() -> None:
    # the PUT on a conflicting PR pushes nothing (no CI cost) and is how the label + page fire
    q = plan([_pr(1, "BLOCKED", RUNNING), _pr(2, "DIRTY", RED), _pr(3, "BEHIND", GREEN)], 1)
    assert _nums(q.update) == [2] and _nums(q.held) == [3]


# ---- through run(): a held PR gets no PUT


class _Done:
    def __init__(self, rc: int = 0, out: str = "") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, ""


def test_run_puts_only_the_queue_head(monkeypatch: pytest.MonkeyPatch) -> None:
    base = {"title": "t", "url": "u", "isCrossRepository": False, "labels": [], "headRefOid": "abc"}
    prs = [{**base, **_pr(n, "BEHIND", GREEN), "headRefName": f"b{n}"} for n in (7, 8)]
    calls: list[tuple] = []

    def fake(*a: str, check: bool = True) -> _Done:
        calls.append(a)
        if a[:2] == ("pr", "list"):
            return _Done(0, json.dumps(prs))
        return _Done(0, "")

    monkeypatch.setattr(upd, "_gh", fake)
    summary: list[str] = []
    assert upd.run("o/r", summary, [], Options(max_in_flight=1)) == 0
    puts = [c for c in calls if c[0] == "api"]
    assert [c[3] for c in puts] == ["repos/o/r/pulls/7/update-branch"]
    assert "#8: held, queued, position 1 (in flight: 0/1)" in summary
    assert summary[1].startswith("queue: 0/1 in flight")
