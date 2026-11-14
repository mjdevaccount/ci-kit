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
            rules: tuple[int, str] = (0, "[]"), runs: list[dict] | None = None, cancel: tuple[int, str] = (0, "")):
    def gh(*a: str, check: bool = True) -> _Done:
        calls.append(a)
        if a[1].endswith("/protection/required_status_checks"):
            return _Done(*classic)
        if "/rules/branches/" in a[1]:
            return _Done(rules[0], rules[1], "" if not rules[0] else "HTTP 403")
        if a[1].startswith("repos/o/r/actions/runs?head_sha="):
            sha = a[1].split("head_sha=")[1].split("&")[0]
            return _Done(0, json.dumps({"workflow_runs": [r for r in runs or [] if r["head_sha"] == sha]}))
        if a[:3] == ("api", "-X", "POST") and a[3].endswith("/cancel"):
            return _Done(cancel[0], "", cancel[1])
        raise AssertionError(f"unexpected gh call {a}")
    return gh


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


def test_only_an_armed_behind_or_dirty_head_is_doomed() -> None:
    prs = [pr(1), pr(2, "DIRTY"), pr(3, "CLEAN"), pr(4, "BLOCKED"), pr(5, "UNKNOWN"), pr(6, "UNSTABLE"),
           pr(7, autoMergeRequest=None), pr(8, headRefName="train/9"), pr(9, labels=[{"name": RIDING}]),
           pr(10, labels=[{"name": PARKED}]), pr(11, isCrossRepository=True)]
    assert [p["number"] for p, _ in dr.doomed(prs)] == [1, 2]


def test_only_active_pull_request_runs_at_that_head_and_never_the_review() -> None:
    runs = [run(1), run(2, status="queued"), run(3, status="completed"), run(4, event="pull_request_target"),
            run(5, event="push"), run(6, event="schedule"), run(7, sha="b" * 40),
            run(8, path=".github/workflows/review-gate.yml")]
    assert [r["id"] for r in dr.cancellable(runs, SHA, "review-gate.yml")] == [1, 2]
    assert [r["id"] for r in dr.cancellable(runs, SHA, "")] == [1, 2, 8]


# ---- sweep(): the I/O, both directions


def test_a_behind_pr_on_a_strict_base_has_its_runs_cancelled() -> None:
    calls: list[tuple] = []
    summary: list[str] = []
    assert dr.sweep("o/r", [pr(1)], "main", "review-gate.yml", fake_gh(calls, runs=[run(11), run(12, event="push")]),
                    summary) == 0
    assert cancelled(calls) == ["11"]
    assert any("o/r#1: cancelled run 11" in s and "behind the base" in s for s in summary)


def test_a_clean_head_is_never_touched() -> None:
    calls: list[tuple] = []
    assert dr.sweep("o/r", [pr(1, "CLEAN")], "main", "", fake_gh(calls, runs=[run(11)]), []) == 0
    assert calls == []  # not even the protection read


def test_a_non_strict_base_cancels_nothing() -> None:
    calls: list[tuple] = []
    gh = fake_gh(calls, classic=(1, "", "HTTP 404: Branch not protected"), runs=[run(11)])
    assert dr.sweep("o/r", [pr(1)], "main", "", gh, []) == 0
    assert cancelled(calls) == []


def test_unreadable_protection_cancels_nothing_and_names_the_grant() -> None:
    calls: list[tuple] = []
    summary: list[str] = []
    gh = fake_gh(calls, classic=(1, "", "HTTP 403: Resource not accessible"), rules=(1, ""), runs=[run(11)])
    assert dr.sweep("o/r", [pr(1)], "main", "", gh, summary) == 0
    assert cancelled(calls) == []
    assert any("Administration: read" in s for s in summary)


def test_a_refused_cancel_is_red_and_a_finished_run_is_not() -> None:
    summary: list[str] = []
    assert dr.sweep("o/r", [pr(1)], "main", "", fake_gh([], runs=[run(11)], cancel=(1, "HTTP 500: boom")), summary) == 1
    assert any("REFUSED" in s for s in summary)
    assert dr.sweep("o/r", [pr(1)], "main", "", fake_gh([], runs=[run(11)],
                                                        cancel=(1, "HTTP 409: Cannot cancel a completed run")), []) == 0


def test_no_actions_grant_is_one_warning_not_a_red() -> None:
    calls: list[tuple] = []
    summary: list[str] = []
    gh = fake_gh(calls, runs=[run(11), run(12)], cancel=(1, "HTTP 403: Resource not accessible by personal access token"))
    assert dr.sweep("o/r", [pr(1)], "main", "", gh, summary) == 0
    assert cancelled(calls) == ["11"]  # stops at the first refusal
    assert any("OFF for this pass" in s and "Actions: Read and write" in s for s in summary)


def test_the_head_the_queue_replaced_is_cancelled_strict_or_not() -> None:
    calls: list[tuple] = []
    summary: list[str] = []
    assert dr.superseded("o/r", pr(1, "CLEAN"), "", fake_gh(calls, classic=(1, "", "HTTP 404"), runs=[run(11)]),
                         summary) == 0
    assert cancelled(calls) == ["11"]
    assert any("the queue updated this PR" in s for s in summary)


# ---- wiring: run() sweeps before any update and cancels each replaced head; main() defaults it ON


def _wire(monkeypatch: pytest.MonkeyPatch, prs: list[dict], seen: list) -> None:
    nodes = [{**p, "labels": {"nodes": p["labels"]}, "title": "t", "url": "u", "baseRefName": "main",
              "isDraft": False, "mergeable": "MERGEABLE", "statusCheckRollup": []} for p in prs]
    monkeypatch.setattr(upd, "_gh_read", lambda *a: _Done(0, json.dumps({
        "data": {"repository": {"pullRequests": {"nodes": nodes}}}})) if a[:2] == ("api", "graphql")
        else _Done(0, json.dumps({"default_branch": "trunk"})))
    monkeypatch.setattr(upd.pr_heal, "assess_armed", lambda *a, **k: ({}, 0))
    monkeypatch.setattr(upd.pr_heal, "act", lambda *a, **k: 0)
    monkeypatch.setattr(upd, "unlabel_resolved", lambda *a, **k: 0)
    monkeypatch.setattr(upd, "reviewing", lambda *a: False)
    monkeypatch.setattr(upd, "_update_one", lambda repo, p, *a: seen.append(("update", p["number"])) or ("updated", ""))
    monkeypatch.setattr(dr, "sweep", lambda repo, prs, base, rw, gh, s: seen.append(("sweep", base)) or 0)
    monkeypatch.setattr(dr, "superseded", lambda repo, p, rw, gh, s: seen.append(("superseded", p["number"])) or 0)


def test_run_sweeps_before_the_update_and_cancels_the_replaced_head(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list = []
    _wire(monkeypatch, [pr(1)], seen)
    assert upd.run("o/r", [], [], Options(cancel_doomed=True)) == 0
    assert seen == [("sweep", "trunk"), ("update", 1), ("superseded", 1)]


def test_run_with_the_rule_off_cancels_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list = []
    _wire(monkeypatch, [pr(1)], seen)
    assert upd.run("o/r", [], [], Options()) == 0
    assert seen == [("update", 1)]


@pytest.mark.parametrize("env,want", [(None, True), ("true", True), ("false", False)])
def test_main_turns_the_rule_on_by_default(monkeypatch: pytest.MonkeyPatch, env, want) -> None:
    got: list[Options] = []
    monkeypatch.setattr(upd, "run", lambda repo, summary, relock, opts: got.append(opts) or 0)
    if env is None:
        monkeypatch.delenv("CI_KIT_CANCEL_DOOMED", raising=False)
    else:
        monkeypatch.setenv("CI_KIT_CANCEL_DOOMED", env)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert upd.main(["--repo", "o/r"]) == 0
    assert got[0].cancel_doomed is want
