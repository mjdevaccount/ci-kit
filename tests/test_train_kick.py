"""train_kick: pr-autoupdate wakes the merge train only when a pass would find work -- both ways."""
from __future__ import annotations

import json

import pytest

from ci_kit import pr_autoupdate as upd
from ci_kit import train_kick
from ci_kit.merge_train import EJECTED
from ci_kit.pr_autoupdate import Options

REQ = ("unit tests", "review gate")


def chk(name: str, done: str = "2026-11-14T10:00:00Z", conclusion: str = "SUCCESS") -> dict:
    return {"__typename": "CheckRun", "name": name, "status": "COMPLETED", "conclusion": conclusion,
            "startedAt": done, "completedAt": done}


def pr(n: int, done: str = "2026-11-14T10:00:00Z", **k) -> dict:
    base = {"number": n, "title": f"t{n}", "url": f"u{n}", "headRefName": f"b{n}", "headRefOid": f"{n:040x}",
            "isCrossRepository": False, "baseRefName": "main", "isDraft": False, "mergeable": "MERGEABLE",
            "autoMergeRequest": {"enabledAt": "2026-11-14T09:00:00Z"}, "labels": [],
            "statusCheckRollup": [chk("unit tests", done), chk("review gate", done)], "mergeStateStatus": "BEHIND"}
    base.update(k)
    return base


def runs(*created: str, status: str = "completed", event: str = "schedule", conclusion: str | None = None) -> list[dict]:
    if conclusion is None:
        conclusion = "success" if status == "completed" else ""
    return [{"id": i, "status": status, "created_at": c, "event": event, "conclusion": conclusion or None}
            for i, c in enumerate(created, 1)]


# ---- decide(): pure, both directions


def test_two_ready_behind_prs_and_no_newer_pass_wake_the_train() -> None:
    go, why = train_kick.decide([pr(1), pr(2)], REQ, "main", runs("2026-11-14T09:30:00Z"))
    assert go and "#1, #2" in why


def test_never_a_pass_before_is_a_wake() -> None:
    assert train_kick.decide([pr(1), pr(2)], REQ, "main", [])[0]


@pytest.mark.parametrize("prs,want", [
    ([pr(1)], "fewer than 2"),                                                      # one PR: auto-merge's
    ([pr(1), pr(2, statusCheckRollup=[chk("unit tests")])], "fewer than 2"),        # a required check absent
    ([pr(1), pr(2, statusCheckRollup=[chk("unit tests"), chk("review gate", conclusion="FAILURE")])],
     "fewer than 2"),
    ([pr(1), pr(2, labels=[{"name": EJECTED}])], "fewer than 2"),                    # the train's own rule
    ([pr(1), pr(2, autoMergeRequest=None)], "fewer than 2"),
    ([pr(1), pr(2), pr(5, headRefName="train/77")], "train #5 is open"),
    ([pr(1, mergeStateStatus="CLEAN"), pr(2)], "#1 is landing on its own"),
])
def test_no_work_for_a_train_is_no_wake(prs: list[dict], want: str) -> None:
    go, why = train_kick.decide(prs, REQ, "main", [])
    assert not go and want in why


def test_a_pass_already_queued_or_running_is_not_stacked() -> None:
    for st in ("queued", "in_progress"):
        go, why = train_kick.decide([pr(1), pr(2)], REQ, "main", runs("2026-11-14T09:00:00Z", status=st))
        assert not go and st in why


def test_a_pass_created_after_the_newest_ready_already_judged_this_set() -> None:
    go, why = train_kick.decide([pr(1), pr(2)], REQ, "main", runs("2026-11-14T10:05:00Z"))
    assert not go and "unchanged since the last pass" in why
    # ...until one candidate becomes ready AFTER that pass (a new head's checks, a re-arm)
    later = [pr(1), pr(2, done="2026-11-14T10:20:00Z")]
    assert train_kick.decide(later, REQ, "main", runs("2026-11-14T10:05:00Z"))[0]
    rearmed = [pr(1), pr(2, autoMergeRequest={"enabledAt": "2026-11-14T10:30:00Z"})]
    assert train_kick.decide(rearmed, REQ, "main", runs("2026-11-14T10:05:00Z"))[0]


def test_a_skipped_run_judged_nothing_and_never_dedups() -> None:
    # the live shape (trade-data 11-14): 9 of the newest 10 runs are workflow_run completions the
    # train job's `if:` skipped. Read as passes, they would stop every wake.
    skipped = runs(*[f"2026-11-14T10:{m:02d}:00Z" for m in range(10, 19)], event="workflow_run",
                   conclusion="skipped")
    real = runs("2026-11-14T09:07:00Z")
    assert train_kick.decide([pr(1), pr(2)], REQ, "main", skipped + real)[0]
    # a skipped run still in flight is not "a pass already running" either
    queued = runs("2026-11-14T10:30:00Z", status="queued", event="workflow_run")
    assert train_kick.decide([pr(1), pr(2)], REQ, "main", queued + real)[0]
    # ...while a real pass after the ready time still dedups
    assert not train_kick.decide([pr(1), pr(2)], REQ, "main", skipped + runs("2026-11-14T10:05:00Z"))[0]


def test_a_cancelled_pass_judged_nothing() -> None:
    gone = runs("2026-11-14T10:05:00Z", event="workflow_dispatch", conclusion="cancelled")
    assert train_kick.decide([pr(1), pr(2)], REQ, "main", gone)[0]
    done = runs("2026-11-14T10:05:00Z", event="workflow_dispatch")
    assert not train_kick.decide([pr(1), pr(2)], REQ, "main", done)[0]


def test_a_pass_is_dated_by_its_start_not_its_creation() -> None:
    # created before the ready time, started (and so READ the PRs) after it: it judged this set
    r = [{"id": 1, "status": "completed", "conclusion": "success", "event": "workflow_dispatch",
          "created_at": "2026-11-14T09:55:00Z", "run_started_at": "2026-11-14T10:02:00Z"}]
    assert not train_kick.decide([pr(1), pr(2)], REQ, "main", r)[0]
    r[0]["run_started_at"] = "2026-11-14T09:58:00Z"
    assert train_kick.decide([pr(1), pr(2)], REQ, "main", r)[0]


def test_the_queue_hold_status_is_never_a_readiness_signal() -> None:
    # `queue/position` is re-posted every pass; read as readiness it would wake the train every pass
    hold = {"__typename": "StatusContext", "context": "queue/position", "state": "PENDING",
            "createdAt": "2026-11-14T11:00:00Z"}
    p2 = pr(2)
    p2["statusCheckRollup"] = [*p2["statusCheckRollup"], hold]
    assert not train_kick.decide([pr(1), p2], REQ, "main", runs("2026-11-14T10:05:00Z"))[0]
    assert train_kick.decide([pr(1), p2], (), "main", runs("2026-11-14T10:05:00Z"))[0] is False


def test_min_batch_is_the_trains_own() -> None:
    assert not train_kick.decide([pr(1), pr(2)], REQ, "main", [], min_batch=3)[0]
    assert train_kick.decide([pr(1), pr(2), pr(3)], REQ, "main", [], min_batch=3)[0]


# ---- kick(): the I/O, both directions


class _Done:
    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, err


def _gh(calls: list[tuple], *, runs_rc: int = 0, run_list: list[dict] | None = None, dispatch_rc: int = 0):
    def fake(*a: str, check: bool = True, env: dict | None = None) -> _Done:
        calls.append(a)
        if a[0] == "api" and "/actions/workflows/" in a[1]:
            event = a[1].split("event=")[1].split("&")[0]  # the server filters by event; so does the fake
            rows = [r for r in run_list or [] if r.get("event") == event]
            return _Done(runs_rc, json.dumps({"workflow_runs": rows}), "HTTP 404: Not Found" if runs_rc else "")
        if a[:2] == ("workflow", "run"):
            return _Done(dispatch_rc, "", "HTTP 403: Resource not accessible" if dispatch_rc else "")
        raise AssertionError(f"unexpected gh call {a}")
    return fake


def test_kick_dispatches_on_the_default_branch() -> None:
    calls: list[tuple] = []
    summary: list[str] = []
    assert train_kick.kick("o/r", "merge-train.yml", [pr(1), pr(2)], REQ, "main", _gh(calls), summary) == 0
    assert ("workflow", "run", "merge-train.yml", "-R", "o/r", "--ref", "main") in calls
    assert any("dispatched" in s for s in summary)


def test_kick_without_work_dispatches_nothing() -> None:
    calls: list[tuple] = []
    summary: list[str] = []
    assert train_kick.kick("o/r", "merge-train.yml", [pr(1)], REQ, "main", _gh(calls), summary) == 0
    assert not [c for c in calls if c[:2] == ("workflow", "run")]
    assert any("not woken" in s for s in summary)


def test_an_unreadable_run_list_is_red_and_dispatches_nothing() -> None:
    calls: list[tuple] = []
    summary: list[str] = []
    assert train_kick.kick("o/r", "merge-train.yml", [pr(1), pr(2)], REQ, "main", _gh(calls, runs_rc=1), summary) == 1
    assert not [c for c in calls if c[:2] == ("workflow", "run")]
    assert any("Actions: Read and write" in s for s in summary)


def test_kick_reads_each_pass_event_and_dedups_on_them() -> None:
    calls: list[tuple] = []
    summary: list[str] = []
    rows = runs("2026-11-14T10:05:00Z", event="workflow_dispatch")
    rows += runs(*[f"2026-11-14T10:{m:02d}:00Z" for m in range(20, 40)], event="workflow_run", conclusion="skipped")
    assert train_kick.kick("o/r", "merge-train.yml", [pr(1), pr(2)], REQ, "main",
                           _gh(calls, run_list=rows), summary) == 0
    reads = [c[1] for c in calls if c[0] == "api"]
    assert reads == [f"repos/o/r/actions/workflows/merge-train.yml/runs?event={e}&per_page=5"
                     for e in train_kick.PASS_EVENTS]
    assert not [c for c in calls if c[:2] == ("workflow", "run")]
    assert any("unchanged since the last pass" in s for s in summary)


def test_a_wrong_workflow_name_is_named_in_the_red() -> None:
    summary: list[str] = []
    assert train_kick.kick("o/r", "merge_train.yml", [pr(1), pr(2)], REQ, "main", _gh([], runs_rc=1), summary) == 1
    assert any("train-workflow" in s and "404" in s for s in summary)


def test_a_refused_dispatch_is_red() -> None:
    summary: list[str] = []
    assert train_kick.kick("o/r", "merge-train.yml", [pr(1), pr(2)], REQ, "main",
                           _gh([], dispatch_rc=1), summary) == 1
    assert any("REFUSED" in s for s in summary)


# ---- wiring: run() kicks only when the caller named a train workflow, never counting a PR it just updated


def _wire(monkeypatch: pytest.MonkeyPatch, seen: list) -> None:
    monkeypatch.setattr(upd, "_gh_read", lambda *a: _Done(0, json.dumps({
        "data": {"repository": {"pullRequests": {"nodes": []}}}})) if a[:2] == ("api", "graphql")
        else _Done(0, json.dumps({"default_branch": "trunk"})))
    monkeypatch.setattr(upd.pr_heal, "assess_armed", lambda *a, **k: ({}, 0))
    monkeypatch.setattr(train_kick, "kick", lambda repo, wf, prs, req, base, gh, summary, mb: seen.append((wf, base, mb)) or 0)


def test_run_kicks_the_named_train_workflow_on_the_default_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list = []
    _wire(monkeypatch, seen)
    assert upd.run("o/r", [], [], Options(train_workflow="merge-train.yml", train_min_batch=3)) == 0
    assert seen == [("merge-train.yml", "trunk", 3)]


def test_run_without_a_train_workflow_never_kicks(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list = []
    _wire(monkeypatch, seen)
    assert upd.run("o/r", [], [], Options()) == 0
    assert seen == []


def test_a_train_workflow_never_changes_the_queue_plans_base(monkeypatch: pytest.MonkeyPatch) -> None:
    # with hold/scarce off the plan ran with base=None before v1.32.0; naming a train workflow must
    # not change that (review of 1.32.0, finding 2) -- the default branch is read for the kick only
    reads: list[tuple] = []
    seen: list = []
    _wire(monkeypatch, seen)
    inner = upd._gh_read
    monkeypatch.setattr(upd, "_gh_read", lambda *a: reads.append(a) or inner(*a))
    blocked: list = []
    real = upd.pr_queue.blocked
    monkeypatch.setattr(upd.pr_queue, "blocked", lambda prs, req, skip, base: blocked.append(base) or real(prs, req, skip, base))
    assert upd.run("o/r", [], [], Options(train_workflow="merge-train.yml")) == 0
    assert blocked == [None]
    assert seen == [("merge-train.yml", "trunk", 2)]
    reads.clear()
    assert upd.run("o/r", [], [], Options()) == 0
    assert ("api", "repos/o/r") not in reads


def test_main_reads_the_train_workflow_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    got: list[Options] = []
    monkeypatch.setattr(upd, "run", lambda repo, summary, relock, opts: got.append(opts) or 0)
    monkeypatch.setenv("CI_KIT_TRAIN_WORKFLOW", "merge-train.yml")
    monkeypatch.setenv("CI_KIT_TRAIN_MIN_BATCH", "2")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert upd.main(["--repo", "o/r"]) == 0
    assert got[0].train_workflow == "merge-train.yml" and got[0].train_min_batch == 2
    monkeypatch.setenv("CI_KIT_TRAIN_WORKFLOW", "")
    monkeypatch.setenv("CI_KIT_TRAIN_MIN_BATCH", "")
    upd.main(["--repo", "o/r"])
    assert got[1].train_workflow == "" and got[1].train_min_batch == train_kick.MIN_BATCH
