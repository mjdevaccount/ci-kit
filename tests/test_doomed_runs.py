"""doomed_runs: cancel only the `pull_request` runs that can never merge -- and nothing else."""
from __future__ import annotations

import json

import pytest

from ci_kit import doomed_runs as dr
from ci_kit import pr_autoupdate as upd
from ci_kit.merge_train import RIDING
from ci_kit.pr_autoupdate import Options
from ci_kit.pr_heal import PARKED

SHA = "a" * 40


def pr(n: int, state: str = "BEHIND", **k) -> dict:
    base = {"number": n, "headRefName": f"b{n}", "headRefOid": SHA if n == 1 else f"{n:040x}",
            "isCrossRepository": False, "mergeStateStatus": state, "labels": [],
            "autoMergeRequest": {"enabledAt": "2026-11-14T09:00:00Z"}}
    base.update(k)
    return base


def run(i: int, sha: str = SHA, event: str = "pull_request", status: str = "in_progress",
        path: str = ".github/workflows/ci.yml") -> dict:
    return {"id": i, "head_sha": sha, "event": event, "status": status, "path": path, "name": path.rsplit("/", 1)[-1]}


class _Done:
    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, err


def fake_gh(calls: list[tuple], *, classic: tuple[int, str, str] = (0, '{"strict": true}', ""),
            rules: tuple[int, str] = (0, "[]"), runs: list[dict] | None = None, cancel: tuple[int, str] = (0, ""),
            workflows: tuple[int, list] = (0, []), behind_by: int | None = 3):
    def gh(*a: str, check: bool = True) -> _Done:
        calls.append(a)
        if a[1].endswith("/protection/required_status_checks"):
            return _Done(*classic)
        if "/rules/branches/" in a[1]:
            return _Done(rules[0], rules[1], "" if not rules[0] else "HTTP 403")
        if a[1].startswith("repos/o/r/actions/workflows"):
            return _Done(workflows[0], json.dumps({"workflows": workflows[1]}), "HTTP 502" if workflows[0] else "")
        if "/compare/" in a[1]:
            if behind_by is None:
                return _Done(1, "", "HTTP 502")
            return _Done(0, json.dumps({"behind_by": behind_by}))
        if a[1].startswith("repos/o/r/actions/runs?head_sha="):
            sha = a[1].split("head_sha=")[1].split("&")[0]
            return _Done(0, json.dumps({"workflow_runs": [r for r in runs or [] if r["head_sha"] == sha]}))
        if a[:3] == ("api", "-X", "POST") and a[3].endswith("/cancel"):
            return _Done(cancel[0], "", cancel[1])
        raise AssertionError(f"unexpected gh call {a}")
    return gh


def sweep(gh, prs: list[dict], summary: list[str] | None = None, train: bool = False, review: str = "") -> int:
    c = dr.Canceller("o/r", review, gh, summary if summary is not None else [])
    c.sweep(prs, "main", train)
    return c.errors


def cancelled(calls: list[tuple]) -> list[str]:
    return [c[3].split("/")[-2] for c in calls if c[:3] == ("api", "-X", "POST")]


# ---- strict(): read, never assumed


@pytest.mark.parametrize("classic,rules,want", [
    ((0, '{"strict": true}', ""), (0, "[]"), True),
    ((0, '{"strict": false}', ""), (0, "[]"), False),
    ((1, "", "HTTP 404: Branch not protected"), (0, "[]"), False),
    ((1, "", "HTTP 404: Branch not protected"),
     (0, json.dumps([{"type": "required_status_checks", "parameters": {"strict_required_status_checks_policy": True}}])),
     True),
    ((1, "", "HTTP 403: Resource not accessible by personal access token"), (0, "[]"), None),  # cannot tell
])
def test_strict_is_read_and_unknown_is_never_false(classic, rules, want) -> None:
    assert dr.strict("o/r", "main", fake_gh([], classic=classic, rules=rules)) is want


# ---- doomed(): pure, both directions


def test_only_an_armed_dirty_head_or_a_trainless_behind_one_is_doomed() -> None:
    prs = [pr(1), pr(2, "DIRTY"), pr(3, "CLEAN"), pr(4, "BLOCKED"), pr(5, "UNKNOWN"), pr(6, "UNSTABLE"),
           pr(7, autoMergeRequest=None), pr(8, headRefName="train/9"), pr(9, labels=[{"name": RIDING}]),
           pr(10, labels=[{"name": PARKED}]), pr(11, isCrossRepository=True)]
    assert [p["number"] for p, _ in dr.doomed(prs, behind=True)] == [1, 2]
    assert [p["number"] for p, _ in dr.doomed(prs, behind=False)] == [2]


def test_only_active_pull_request_runs_of_this_pr_at_that_head_and_never_the_review() -> None:
    runs = [run(1), run(2, status="queued"), run(3, status="completed"), run(4, event="pull_request_target"),
            run(5, event="push"), run(6, event="schedule"), run(7, sha="b" * 40),
            run(8, path=".github/workflows/review-gate.yml"),
            {**run(9), "pull_requests": [{"number": 2}]}, {**run(10), "pull_requests": [{"number": 1}]}]
    assert [r["id"] for r in dr.cancellable(runs, SHA, 1, "review-gate.yml")] == [1, 2, 10]
    assert [r["id"] for r in dr.cancellable(runs, SHA, 1, "")] == [1, 2, 8, 10]


# ---- sweep(): the I/O, both directions


def test_a_behind_pr_on_a_strict_trainless_base_has_its_runs_cancelled() -> None:
    calls: list[tuple] = []
    summary: list[str] = []
    gh = fake_gh(calls, runs=[run(11), run(12, event="push"), run(13, path=".github/workflows/review-gate.yml")])
    assert sweep(gh, [pr(1)], summary, review="review-gate.yml") == 0
    assert cancelled(calls) == ["11"]
    assert any("o/r#1: cancelled run 11" in s and "behind the base" in s for s in summary)


def test_a_merge_train_reads_behind_heads_so_they_are_never_cancelled() -> None:
    # finding 1 of the session review: a train takes a BEHIND PR whose checks are green at its head
    for kw in ({"workflows": (0, [{"name": "merge-train", "path": ".github/workflows/merge-train.yml"}])},
               {"workflows": (1, [])}):  # unreadable = a train, never a guess
        calls: list[tuple] = []
        assert sweep(fake_gh(calls, runs=[run(11)], **kw), [pr(1)]) == 0
        assert cancelled(calls) == []
    calls = []
    assert sweep(fake_gh(calls, runs=[run(11)]), [pr(1)], train=True) == 0
    assert cancelled(calls) == []
    # ...while a DIRTY head on the same trained repo is (the train never takes a conflicting PR)
    calls = []
    trained = fake_gh(calls, runs=[run(11)], workflows=(0, [{"name": "merge-train", "path": "x"}]))
    assert sweep(trained, [pr(1, "DIRTY")]) == 0
    assert cancelled(calls) == ["11"]


def test_behind_is_re_proven_by_git_before_a_cancel() -> None:
    calls: list[tuple] = []
    summary: list[str] = []
    assert sweep(fake_gh(calls, runs=[run(11)], behind_by=0), [pr(1)], summary) == 0
    assert cancelled(calls) == [] and any("already contains" in s for s in summary)
    calls = []
    assert sweep(fake_gh(calls, runs=[run(11)], behind_by=None), [pr(1)]) == 1  # could not compare: red, no cancel
    assert cancelled(calls) == []


def test_a_clean_head_is_never_touched() -> None:
    calls: list[tuple] = []
    assert sweep(fake_gh(calls, runs=[run(11)]), [pr(1, "CLEAN")]) == 0
    assert calls == []  # not even the protection read


def test_a_non_strict_base_cancels_nothing() -> None:
    calls: list[tuple] = []
    assert sweep(fake_gh(calls, classic=(1, "", "HTTP 404: Branch not protected"), runs=[run(11)]), [pr(1)]) == 0
    assert cancelled(calls) == []


def test_unreadable_protection_cancels_nothing_and_names_the_grant() -> None:
    calls: list[tuple] = []
    summary: list[str] = []
    gh = fake_gh(calls, classic=(1, "", "HTTP 403: Resource not accessible"), rules=(1, ""), runs=[run(11)])
    assert sweep(gh, [pr(1)], summary) == 0
    assert cancelled(calls) == []
    assert any("Administration: read" in s for s in summary)


def test_a_refused_cancel_is_red_and_a_finished_run_is_not() -> None:
    summary: list[str] = []
    assert sweep(fake_gh([], runs=[run(11)], cancel=(1, "HTTP 500: boom")), [pr(1)], summary) == 1
    assert any("REFUSED" in s for s in summary)
    assert sweep(fake_gh([], runs=[run(11)], cancel=(1, "HTTP 409: Cannot cancel a completed run")), [pr(1)]) == 0
    # a rate-limit 403 is a refusal, not a missing grant
    assert sweep(fake_gh([], runs=[run(11)], cancel=(1, "HTTP 403: API rate limit exceeded")), [pr(1)]) == 1


def test_no_actions_grant_is_one_warning_and_keeps_earlier_errors() -> None:
    calls: list[tuple] = []
    summary: list[str] = []
    gh = fake_gh(calls, runs=[run(11), run(12)], cancel=(1, "HTTP 403: Resource not accessible by personal access token"))
    c = dr.Canceller("o/r", "", gh, summary)
    c.errors = 1  # an earlier PR's failed read (finding 4: a refusal must not erase it)
    c.sweep([pr(1)], "main", False)
    c.superseded(pr(1, "CLEAN"))
    assert cancelled(calls) == ["11"]  # stops at the first refusal, for the rest of the pass
    assert c.errors == 1
    assert sum("OFF for this pass" in s for s in summary) == 1


def test_the_head_the_queue_replaced_is_cancelled_strict_or_not() -> None:
    calls: list[tuple] = []
    summary: list[str] = []
    c = dr.Canceller("o/r", "", fake_gh(calls, classic=(1, "", "HTTP 404"), runs=[run(11)]), summary)
    c.superseded(pr(1, "CLEAN"))
    assert c.errors == 0 and cancelled(calls) == ["11"]
    assert any("the queue updated this PR" in s for s in summary)


# ---- wiring: run() sweeps before any update and cancels each replaced head; main() defaults it ON


def _wire(monkeypatch: pytest.MonkeyPatch, prs: list[dict], seen: list) -> None:
    nodes = [{**p, "labels": {"nodes": p["labels"]}, "title": "t", "url": "u", "baseRefName": "trunk",
              "isDraft": False, "mergeable": "MERGEABLE",
              "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {
                  "nodes": p.get("statusCheckRollup") or []}}}}]}} for p in prs]
    monkeypatch.setattr(upd, "_gh_read", lambda *a: _Done(0, json.dumps({
        "data": {"repository": {"pullRequests": {"nodes": nodes}}}})) if a[:2] == ("api", "graphql")
        else _Done(0, json.dumps({"default_branch": "trunk"})))
    monkeypatch.setattr(upd.pr_heal, "assess_armed", lambda *a, **k: ({}, 0))
    monkeypatch.setattr(upd.pr_heal, "act", lambda *a, **k: 0)
    monkeypatch.setattr(upd, "unlabel_resolved", lambda *a, **k: 0)
    monkeypatch.setattr(upd, "reviewing", lambda *a: False)
    monkeypatch.setattr(upd, "_update_one", lambda repo, p, *a: seen.append(("update", p["number"])) or ("updated", ""))
    monkeypatch.setattr(dr.Canceller, "sweep", lambda self, prs, base, train: seen.append(("sweep", base, train)))
    monkeypatch.setattr(dr.Canceller, "superseded", lambda self, p: seen.append(("superseded", p["number"])))


def test_run_sweeps_before_the_update_and_cancels_the_replaced_head(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list = []
    _wire(monkeypatch, [pr(1)], seen)
    assert upd.run("o/r", [], [], Options(cancel_doomed=True)) == 0
    assert seen == [("sweep", "trunk", False), ("update", 1), ("superseded", 1)]


def test_run_with_the_rule_off_cancels_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list = []
    _wire(monkeypatch, [pr(1)], seen)
    assert upd.run("o/r", [], [], Options()) == 0
    assert seen == [("update", 1)]


@pytest.mark.parametrize("env,want", [(None, True), ("true", True), ("false", False)])
def test_main_turns_the_rule_on_by_default(monkeypatch: pytest.MonkeyPatch, env, want) -> None:
    got: list[Options] = []
    monkeypatch.setattr(upd, "run", lambda repo, summary, relock, opts, *_: got.append(opts) or 0)
    if env is None:
        monkeypatch.delenv("CI_KIT_CANCEL_DOOMED", raising=False)
    else:
        monkeypatch.setenv("CI_KIT_CANCEL_DOOMED", env)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert upd.main(["--repo", "o/r"]) == 0
    assert got[0].cancel_doomed is want


def test_a_cancel_error_turns_the_pass_red(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list = []
    _wire(monkeypatch, [pr(1)], seen)
    monkeypatch.setattr(dr.Canceller, "sweep", lambda self, prs, base, train: setattr(self, "errors", 1))
    assert upd.run("o/r", [], [], Options(cancel_doomed=True)) == 1


# ---- round 2: a BEHIND PR whose running checks were cancelled is UPDATED this pass, never held


def _running(p: dict) -> dict:
    return {**p, "statusCheckRollup": [{"__typename": "CheckRun", "name": "unit", "status": "IN_PROGRESS",
                                        "conclusion": None}]}


def test_the_plan_takes_a_restarted_behind_pr_and_still_holds_the_rest() -> None:
    from ci_kit import pr_queue
    a, b = _running(pr(1)), _running(pr(2))
    q = pr_queue.plan([a, b], 1, restarted=frozenset({1}))
    assert [p["number"] for p in q.update] == [1]
    assert any(p["number"] == 2 and "checks still running" in why for p, why in q.held)
    q = pr_queue.plan([a, b], 1)
    assert q.update == []


def test_run_updates_the_pr_whose_running_checks_it_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list = []
    _wire(monkeypatch, [_running(pr(1))], seen)
    monkeypatch.setattr(dr.Canceller, "sweep", DR_SWEEP)
    monkeypatch.setattr(dr.Canceller, "superseded", DR_SUPERSEDED)
    calls: list[tuple] = []
    monkeypatch.setattr(upd, "_gh", fake_gh(calls, runs=[run(11)]))
    summary: list[str] = []
    assert upd.run("o/r", summary, [], Options(cancel_doomed=True, max_in_flight=1)) == 0
    assert cancelled(calls) == ["11"]
    assert ("update", 1) in seen, summary
    # ...and without the cancel (a merge train repo) it is held for its running checks, as before
    seen.clear()
    calls.clear()
    monkeypatch.setattr(upd, "_gh", fake_gh(calls, runs=[run(11)], workflows=(0, [{"name": "merge-train", "path": "x"}])))
    s2: list[str] = []
    assert upd.run("o/r", s2, [], Options(cancel_doomed=True, max_in_flight=1)) == 0
    assert cancelled(calls) == [] and ("update", 1) not in seen, (seen, s2)


def test_a_stacked_pr_is_judged_against_its_own_base_elsewhere() -> None:
    calls: list[tuple] = []
    assert sweep(fake_gh(calls, runs=[run(11)]), [pr(1, baseRefName="feature")]) == 0
    assert calls == []


def test_an_unreadable_workflow_list_says_the_behind_rule_was_skipped() -> None:
    summary: list[str] = []
    assert sweep(fake_gh([], runs=[run(11)], workflows=(1, [])), [pr(1)], summary) == 0
    assert any("BEHIND rule skipped" in s for s in summary)


DR_SWEEP = dr.Canceller.sweep
DR_SUPERSEDED = dr.Canceller.superseded
