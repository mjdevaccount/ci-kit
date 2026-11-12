"""pr-autoupdate's two queue fixes (v1.26.1), from trade-data 2026-11-11, both directions.

td#2980: picked by the serial queue but skipped for a running review, 21:10-23:32Z, with 0/1 in
flight: its slot idled while five armed PRs waited. MUST FIRE: the next PR in line takes it.
td#2983: labelled `conflict` 23:05Z, resolved by a session 23:32Z, still labelled -- the label came
off only a PR the queue attempted. MUST FIRE: a MERGEABLE PR loses it. MUST NOT FIRE: a DIRTY or
not-yet-computed (UNKNOWN) PR keeps it.
"""
from __future__ import annotations

import pytest

import ci_kit.pr_autoupdate as upd
from ci_kit import pr_queue


class _Done:
    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, err


def _pr(n: int, **k) -> dict:
    return {"number": n, "title": "t", "url": "u", "headRefOid": "a" * 40, "headRefName": f"b{n}", "labels": [],
            "isCrossRepository": False, "mergeStateStatus": "BEHIND", "mergeable": "MERGEABLE",
            "autoMergeRequest": {"enabledAt": f"2026-11-11T0{n % 10}:00:00Z"}, "statusCheckRollup": [], **k}


def _world(monkeypatch: pytest.MonkeyPatch, rows: list[dict], reviewing: set[str], edit_rc: int = 0,
           put: _Done | None = None) -> list[tuple]:
    calls: list[tuple] = []
    monkeypatch.setattr(upd.pr_list, "rows", lambda _: rows)
    monkeypatch.setattr(upd, "_gh_read", lambda *a: _Done(out="{}"))
    monkeypatch.setattr(upd, "reviewing", lambda repo, branch, wf: branch in reviewing)

    def gh(*a: str, **k) -> _Done:
        calls.append(a)
        if a[:2] == ("pr", "edit"):
            return _Done(rc=edit_rc)
        if a[:3] == ("api", "-X", "PUT") and put is not None:
            return put
        if a[:2] == ("pr", "view"):
            return _Done(out="OPEN")
        return _Done()
    monkeypatch.setattr(upd, "_gh", gh)
    return calls


def _puts(calls: list[tuple]) -> list[str]:
    return [a[3] for a in calls if a[:3] == ("api", "-X", "PUT")]


def test_plan_keeps_the_eligible_prs_past_the_slots_in_order() -> None:
    q = pr_queue.plan([_pr(1), _pr(2), _pr(3)], 1)
    assert [p["number"] for p in q.update] == [1] and [p["number"] for p in q.spare] == [2, 3]
    assert pr_queue.plan([_pr(1)], 0).spare == []


def test_a_skipped_review_hands_its_slot_to_the_next_in_line_must_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _world(monkeypatch, [_pr(1), _pr(2), _pr(3)], {"b1", "b2"})
    summary: list[str] = []
    assert upd.run("o/r", summary, [], upd.Options(max_in_flight=1, review_workflow="rg.yml")) == 0
    assert _puts(calls) == ["repos/o/r/pulls/3/update-branch"]       # 1 and 2 mid-review: 3 takes it
    assert "#2: takes the slot #1's running review left free" in summary
    assert "#3: takes the slot #2's running review left free" in summary


def test_no_running_review_updates_only_the_queue_head_must_not_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _world(monkeypatch, [_pr(1), _pr(2), _pr(3)], set())
    assert upd.run("o/r", [], [], upd.Options(max_in_flight=1, review_workflow="rg.yml")) == 0
    assert _puts(calls) == ["repos/o/r/pulls/1/update-branch"]


def test_a_skipped_conflict_takes_no_slot_so_hands_none_on(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _world(monkeypatch, [_pr(1, mergeStateStatus="DIRTY"), _pr(2), _pr(3), _pr(4)], {"b1", "b2"})
    summary: list[str] = []
    assert upd.run("o/r", summary, [], upd.Options(max_in_flight=1, review_workflow="rg.yml")) == 0
    # DIRTY #1 is attempted outside the slots, so its skip frees none; #2's skip hands ONE slot on
    assert _puts(calls) == ["repos/o/r/pulls/3/update-branch"]
    assert not any("#1's running review" in s for s in summary)


def test_stale_labels_both_directions() -> None:
    lab = [{"name": "conflict"}]
    prs = [_pr(2983, mergeStateStatus="BLOCKED", labels=lab),                  # resolved: fires
           _pr(1, mergeable="CONFLICTING", mergeStateStatus="DIRTY", labels=lab),
           _pr(2, mergeable="UNKNOWN", mergeStateStatus="UNKNOWN", labels=lab),  # not computed yet
           _pr(3, mergeStateStatus="CLEAN"),                                      # never labelled
           _pr(4, labels=lab),                                                    # attempted: its own path
           _pr(5, mergeStateStatus="DIRTY", labels=lab)]                          # MERGEABLE but DIRTY: kept
    assert upd.stale_labels(prs, attempted={4}, label="conflict") == [2983]


def test_a_resolved_conflict_loses_its_label_even_unarmed_must_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = _pr(2983, mergeStateStatus="BLOCKED", labels=[{"name": "conflict"}], autoMergeRequest=None)
    calls = _world(monkeypatch, [resolved], set())
    summary: list[str] = []
    assert upd.run("o/r", summary, [], upd.Options(max_in_flight=1)) == 0
    assert ("pr", "edit", "2983", "-R", "o/r", "--remove-label", "conflict") in calls
    assert any("#2983: `conflict` label removed" in s for s in summary)


def test_a_label_that_will_not_come_off_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _world(monkeypatch, [_pr(2983, mergeStateStatus="BLOCKED", labels=[{"name": "conflict"}])], set(), edit_rc=1)
    summary: list[str] = []
    assert upd.run("o/r", summary, [], upd.Options(max_in_flight=1)) == 1
    assert any("label NOT removed" in s for s in summary)


def test_a_labelled_pr_whose_update_conflicts_keeps_its_label_must_not_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review finding 1: GitHub said MERGEABLE at list time, main moved, the PUT says conflict. The
    attempted PR is excluded from the stale sweep, so its list-time MERGEABLE cannot strip the label
    (which would re-page it next run)."""
    lab = [{"name": "conflict"}]
    calls = _world(monkeypatch, [_pr(7, labels=lab)], set(),
                   put=_Done(rc=1, out='{"message": "merge conflict between base and head"}'))
    summary: list[str] = []
    upd.run("o/r", summary, [], upd.Options(max_in_flight=1))
    assert _puts(calls) == ["repos/o/r/pulls/7/update-branch"]
    assert not any(a[:2] == ("pr", "edit") and "--remove-label" in a for a in calls)


def test_an_open_train_keeps_an_ordinary_spare_out_of_a_freed_slot_must_not_fire(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Review finding 2: with a train open, an EJECTED PR mid-review must not hand its slot to an
    ordinary PR -- updating that one would put the open train BEHIND (the hold's whole point)."""
    train = _pr(9, headRefName="train/1-1", autoMergeRequest=None)
    ejected = _pr(1, labels=[{"name": upd.EJECTED}])
    calls = _world(monkeypatch, [train, ejected, _pr(2)], {"b1"})
    summary: list[str] = []
    assert upd.run("o/r", summary, [], upd.Options(max_in_flight=1, review_workflow="rg.yml")) == 0
    assert _puts(calls) == []
    assert not any("takes the slot" in s for s in summary)


def test_an_open_train_gives_the_slot_it_holds_to_an_ejected_pr_must_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review of 1.26.1, finding 1: the slot went to ordinary #1, the train hold took it back, and
    ejected #2 waited in spare forever -- 0/1 in flight. The emptied slot goes to #2 now."""
    train = _pr(9, headRefName="train/1-1", autoMergeRequest=None)
    calls = _world(monkeypatch, [train, _pr(1), _pr(2, labels=[{"name": upd.EJECTED}]), _pr(3)], set())
    summary: list[str] = []
    assert upd.run("o/r", summary, [], upd.Options(max_in_flight=1)) == 0
    assert _puts(calls) == ["repos/o/r/pulls/2/update-branch"]                 # never 1 or 3
    assert not any(s.startswith("#2: held") for s in summary)


def test_an_open_train_hands_a_reviewing_ejected_slot_to_the_next_ejected_must_fire(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Review of 1.26.1, finding 2: the positive train-open handoff. Ejected #1 is mid-review;
    ejected #3 takes its slot, ordinary #2 never does."""
    train = _pr(9, headRefName="train/1-1", autoMergeRequest=None)
    ej = [{"name": upd.EJECTED}]
    calls = _world(monkeypatch, [train, _pr(1, labels=ej), _pr(2), _pr(3, labels=ej)], {"b1"})
    assert upd.run("o/r", [], [], upd.Options(max_in_flight=1, review_workflow="rg.yml")) == 0
    assert _puts(calls) == ["repos/o/r/pulls/3/update-branch"]
