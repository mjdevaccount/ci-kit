"""pr_list.settle: UNKNOWN is re-read before it is planned as behind -- both directions."""
from __future__ import annotations

import json
import subprocess

import pytest

from ci_kit import pr_autoupdate, pr_list, pr_queue


def _pr(n, state, armed=True, fork=False):
    return {"number": n, "mergeStateStatus": state, "isCrossRepository": fork,
            "autoMergeRequest": {"enabledAt": "x"} if armed else None}


def _reader(*answers):
    seq = list(answers)
    calls = []

    def read():
        calls.append(1)
        return seq.pop(0) if len(seq) > 1 else seq[0]
    return read, calls


def test_unknown_armed_pr_is_reread_until_computed():
    read, calls = _reader([_pr(1, "UNKNOWN")], [_pr(1, "CLEAN")])
    slept = []
    prs, left = pr_list.settle(read, tries=2, wait_s=5, sleep=slept.append)
    assert prs[0]["mergeStateStatus"] == "CLEAN" and left == [] and len(calls) == 2 and slept == [5]


def test_computed_states_are_read_once_never_slept():
    read, calls = _reader([_pr(1, "BEHIND"), _pr(2, "CLEAN"), _pr(3, "DIRTY")])
    slept = []
    pr_list.settle(read, tries=2, wait_s=5, sleep=slept.append)
    assert len(calls) == 1 and slept == []


def test_still_unknown_is_bounded_and_reported():
    read, calls = _reader([_pr(7, "UNKNOWN")])
    slept = []
    _prs, left = pr_list.settle(read, tries=2, wait_s=1, sleep=slept.append)
    assert left == [7] and len(calls) == 3 and slept == [1, 1]


def test_unarmed_or_fork_unknown_does_not_trigger_a_reread():
    read, calls = _reader([_pr(1, "UNKNOWN", armed=False), _pr(2, None, fork=True)])
    pr_list.settle(read, tries=2, wait_s=1, sleep=lambda s: None)
    assert len(calls) == 1


def test_absent_state_counts_as_unknown():
    assert pr_list.unknown([_pr(4, None)]) == [4]


def test_tries_zero_is_off():
    read, calls = _reader([_pr(1, "UNKNOWN")])
    _prs, left = pr_list.settle(read, tries=0, wait_s=1, sleep=lambda s: None)
    assert left == [1] and len(calls) == 1


# -- A failed RE-read keeps the last good rows; the first read still raises -----------------

def test_a_failed_reread_keeps_the_last_good_rows_and_names_it():
    calls = []

    def read():
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("gh exit 1")
        return [_pr(1, "UNKNOWN")]
    errors: list[str] = []
    prs, left = pr_list.settle(read, tries=2, wait_s=0, sleep=lambda s: None, errors=errors)
    assert prs == [_pr(1, "UNKNOWN")] and left == [1] and len(calls) == 2
    assert errors == ["RuntimeError: gh exit 1"]


def test_the_first_read_failing_still_raises():
    def read():
        raise RuntimeError("no rows")
    with pytest.raises(RuntimeError):
        pr_list.settle(read, tries=2, wait_s=0, sleep=lambda s: None)


# -- A typo'd knob falls back to the default, never crashes an importer --------------------

@pytest.mark.parametrize("raw, cast, default, want", [
    ("x", int, 2, 2), ("-1", int, 2, 2), ("3", int, 2, 3),
    ("nan", float, 5.0, 5.0), ("-2", float, 5.0, 5.0), ("0.5", float, 5.0, 0.5)])
def test_env_knobs_fall_back_on_garbage(monkeypatch, raw, cast, default, want):
    monkeypatch.setenv("CI_KIT_TEST_KNOB", raw)
    assert pr_list._env_num("CI_KIT_TEST_KNOB", default, cast) == want


# -- The wiring: run() reads through _read_prs, and both directions reach the plan ---------

def _wire(monkeypatch, *answers):
    seq = [list(a) for a in answers]
    monkeypatch.setattr(pr_list, "UNKNOWN_TRIES", 1)
    monkeypatch.setattr(pr_list, "UNKNOWN_WAIT_S", 0.0)
    monkeypatch.setattr(pr_list, "rows", lambda raw: raw)
    monkeypatch.setattr(pr_autoupdate, "_gh_read", lambda *a: subprocess.CompletedProcess(
        a, 0, json.dumps(seq.pop(0) if len(seq) > 1 else seq[0]), ""))


def _green(n, state):
    return _pr(n, state) | {"statusCheckRollup": [{"__typename": "CheckRun", "name": "ci",
                                                  "status": "COMPLETED", "conclusion": "SUCCESS"}]}


def test_settled_to_clean_is_in_flight_never_updated_must_not_fire(monkeypatch):
    _wire(monkeypatch, [_green(5, "UNKNOWN")], [_green(5, "CLEAN")])
    summary: list[str] = []
    q = pr_queue.plan(pr_autoupdate._read_prs("o/r", summary), 1)
    assert [p["number"] for p in q.in_flight] == [5] and q.update == [] and summary == []


def test_still_unknown_is_planned_as_before_and_named_must_fire(monkeypatch):
    _wire(monkeypatch, [_green(6, "UNKNOWN")])
    summary: list[str] = []
    q = pr_queue.plan(pr_autoupdate._read_prs("o/r", summary), 1)
    assert [p["number"] for p in q.update] == [6]
    assert any("still UNKNOWN" in line and "#6" in line for line in summary)


def test_run_reads_the_prs_through_settle(monkeypatch):
    class Sentinel(Exception):
        pass

    def boom(repo, summary):
        raise Sentinel
    monkeypatch.setattr(pr_autoupdate, "_read_prs", boom)
    with pytest.raises(Sentinel):
        pr_autoupdate.run("o/r", [], [], pr_autoupdate.Options())
