"""pr-autoupdate `update-held`: keep HELD (open, not armed) PRs current with the base, opt-in.

WHY. A PR deliberately held for review (not armed, often `autoland-off`) was never updated: the
pass selects ARMED PRs only, so it rotted BEHIND the base until a human merged the base in by hand.
MUST FIRE: on + a scheduled / dispatched pass updates a held BEHIND, cleanly mergeable PR, inside
the queue's own free slots, `autoland-off` included (that label opts out of LANDING, not of being
kept current). MUST NOT FIRE: off (the default) changes nothing; a push / workflow_run / arming pass
never touches a held PR; an armed PR that needed an update this pass outranks every held one; a
draft, fork, conflicting, bot (Renovate included) or merge-train PR is never touched -- each skip is named.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

import ci_kit.pr_autoupdate as upd
from ci_kit import pr_held


class _Done:
    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, err


def _pr(n: int, armed: bool = False, **k) -> dict:
    return {"number": n, "title": "t", "url": "u", "headRefOid": "a" * 40, "headRefName": f"b{n}", "labels": [],
            "isCrossRepository": False, "isDraft": False, "mergeStateStatus": "BEHIND", "mergeable": "MERGEABLE",
            "author": {"login": "someone"}, "headCommittedDate": "2026-11-01T00:00:00Z",
            "autoMergeRequest": {"enabledAt": "2026-11-15T00:00:00Z"} if armed else None,
            "statusCheckRollup": [], **k}


def _world(monkeypatch: pytest.MonkeyPatch, rows: list[dict]) -> list[tuple]:
    calls: list[tuple] = []
    monkeypatch.setattr(upd.pr_list, "rows", lambda _: rows)
    monkeypatch.setattr(upd, "_gh_read", lambda *a: _Done(out="{}"))
    monkeypatch.setattr(upd, "reviewing", lambda repo, branch, wf: False)

    def gh(*a: str, **k) -> _Done:
        calls.append(a)
        if a[:2] == ("pr", "view"):
            return _Done(out="OPEN")
        return _Done()
    monkeypatch.setattr(upd, "_gh", gh)
    return calls


def _puts(calls: list[tuple]) -> list[int]:
    return [int(a[3].split("/")[4]) for a in calls if a[:3] == ("api", "-X", "PUT")]


def _run(monkeypatch: pytest.MonkeyPatch, rows: list[dict], **opts) -> tuple[list[int], list[str]]:
    calls = _world(monkeypatch, rows)
    summary: list[str] = []
    assert upd.run("o/r", summary, [], upd.Options(**opts)) == 0
    return _puts(calls), summary


def test_off_by_default_a_held_pr_is_never_updated_must_not_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    assert upd.Options().update_held is False
    puts, summary = _run(monkeypatch, [_pr(1, armed=True), _pr(2)], event="schedule")
    assert puts == [1]                                                  # the armed PR only, as before
    assert not any("held" in s for s in summary)                        # not one new summary line


def test_on_and_scheduled_updates_a_held_behind_pr_must_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    puts, summary = _run(monkeypatch, [_pr(2)], update_held=True, event="schedule")
    assert puts == [2]
    assert "#2: updated" in summary
    assert "update-held: 1 held PR(s) updated of 1 eligible, 0 skipped, 0 error(s)" in summary


def test_on_and_dispatched_updates_too_must_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    puts, _ = _run(monkeypatch, [_pr(2)], update_held=True, event="workflow_dispatch")
    assert puts == [2]


@pytest.mark.parametrize("event", ["push", "workflow_run", "pull_request_target", ""])
def test_on_but_not_a_cron_or_dispatch_pass_touches_no_held_pr_must_not_fire(
        monkeypatch: pytest.MonkeyPatch, event: str) -> None:
    puts, summary = _run(monkeypatch, [_pr(2)], update_held=True, event=event)
    assert puts == []
    assert any(s.startswith("update-held: not this pass") for s in summary)   # named, never silent


def test_autoland_off_opts_out_of_landing_not_of_being_kept_current_must_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    puts, _ = _run(monkeypatch, [_pr(2, labels=[{"name": "autoland-off"}])], update_held=True, event="schedule")
    assert puts == [2]


def test_an_armed_pr_that_needed_an_update_outranks_every_held_one(monkeypatch: pytest.MonkeyPatch) -> None:
    for mif in (0, 1, 3):
        puts, summary = _run(monkeypatch, [_pr(1, armed=True), _pr(2)], update_held=True, event="schedule",
                             max_in_flight=mif)
        assert puts == [1], mif
        assert "#2: held, not updated: armed PRs go first: #1 (picked by the queue this pass)" in summary


def test_a_current_armed_pr_does_not_block_the_held_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    # no queue: a CLEAN armed PR is attempted ("current") but needed no update
    puts, _ = _run(monkeypatch, [_pr(1, armed=True, mergeStateStatus="CLEAN"), _pr(2)], update_held=True,
                   event="schedule")
    assert puts == [1, 2]


def test_held_prs_use_the_queues_free_slots_never_a_second_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    running = [{"name": "unit", "status": "IN_PROGRESS"}]
    puts, summary = _run(monkeypatch, [_pr(1, armed=True, mergeStateStatus="BLOCKED", statusCheckRollup=running),
                                       _pr(2)], update_held=True, event="schedule", max_in_flight=1)
    assert puts == []                                                   # #1 holds the one slot
    assert "#2: held, not updated: no free queue slot (1 of 1 taken: armed in flight #1)" in summary
    puts, summary = _run(monkeypatch, [_pr(2), _pr(3)], update_held=True, event="schedule", max_in_flight=1)
    assert puts == [2]                                                  # one slot, lowest number first
    assert "#3: held, not updated: no free queue slot (1 of 1 taken: held, picked this pass #2)" in summary


def test_draft_fork_conflict_renovate_and_train_prs_are_untouched_and_named(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_pr(2, isDraft=True), _pr(3, isCrossRepository=True), _pr(4, mergeStateStatus="DIRTY"),
            _pr(5, mergeable="CONFLICTING"), _pr(6, author={"login": "renovate"}), _pr(7, headRefName="train/x"),
            _pr(8, mergeStateStatus="UNKNOWN"), _pr(9, statusCheckRollup=[{"name": "unit", "status": "IN_PROGRESS"}]),
            _pr(10, mergeStateStatus="CLEAN")]
    puts, summary = _run(monkeypatch, rows, update_held=True, event="schedule")
    assert puts == []
    held = {s.split(":", 1)[0]: s for s in summary if ": held, not updated: " in s}
    assert set(held) == {"#2", "#3", "#4", "#5", "#6", "#7", "#8", "#9"}  # #10 is current: nothing to do
    assert "a draft" in held["#2"] and "a fork" in held["#3"] and "conflicts" in held["#4"] and "conflicts" in held["#5"]
    assert "a bot's PR (renovate)" in held["#6"] and "merge train" in held["#7"] and "not computed" in held["#8"]
    assert "running" in held["#9"]
    assert not any(a for a in summary if "label" in a)                  # conflict reporting untouched
    assert "update-held: 0 held PR(s) updated of 0 eligible, 8 skipped, 0 error(s)" in summary


def test_an_open_merge_train_holds_the_held_pass_too() -> None:
    eligible = [_pr(2)]
    todo, skipped = pr_held.admit(eligible, [_pr(1, armed=True, headRefName="train/a")], upd.pr_queue.Plan(), 0)
    assert todo == [] and skipped[0][1].startswith("merge train #1 is open")


def test_main_reads_the_switch_and_the_event_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[upd.Options] = []
    monkeypatch.setattr(upd, "run", lambda repo, s, r, opts, *rest: seen.append(opts) or 0)
    monkeypatch.setenv("CI_KIT_UPDATE_HELD", "true")
    monkeypatch.setenv("CI_KIT_EVENT", "schedule")
    upd.main(["--repo", "o/r"])
    monkeypatch.delenv("CI_KIT_UPDATE_HELD")
    upd.main(["--repo", "o/r"])
    assert [(o.update_held, o.event) for o in seen] == [(True, "schedule"), (False, "schedule")]


def test_the_workflow_input_is_opt_in_and_reaches_the_tool_as_env() -> None:
    wf = yaml.safe_load((pathlib.Path(__file__).resolve().parent.parent / ".github/workflows/pr-autoupdate.yml")
                        .read_text(encoding="utf-8"))
    on = wf.get("on") or wf[True]
    assert on["workflow_call"]["inputs"]["update-held"]["default"] is False
    assert on["workflow_call"]["inputs"]["update-held"]["type"] == "boolean"
    assert wf["jobs"]["update"]["env"]["CI_KIT_UPDATE_HELD"] == "${{ inputs.update-held }}"
    assert wf["jobs"]["update"]["env"]["CI_KIT_EVENT"] == "${{ github.event_name }}"
