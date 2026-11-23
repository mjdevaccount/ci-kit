"""pr_list.settle: UNKNOWN is re-read before it is planned as behind -- both directions."""
from __future__ import annotations

from ci_kit import pr_list


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
