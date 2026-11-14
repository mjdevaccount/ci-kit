"""The queue hold (ci_kit/pr_hold.py): `queue/position` success on the HEAD only, so an armed PR
behind it cannot auto-merge out of turn. Each guard shown firing on its case, and not on the others."""
from __future__ import annotations

import json
import pathlib
import subprocess

import pytest
import yaml

from ci_kit import pr_autoupdate, pr_hold, pr_queue

RED = [{"name": "unit tests", "status": "COMPLETED", "conclusion": "FAILURE"}]
RUNNING = [{"name": "unit tests", "status": "IN_PROGRESS", "conclusion": None}]


def _pr(n: int, *, armed: bool = True, draft: bool = False, branch: str = "", fork: bool = False,
        base: str = "main", merge: str = "CLEAN", checks: list[dict] | None = None, labels: tuple[str, ...] = ()) -> dict:
    return {"number": n, "headRefOid": f"{n:040d}", "headRefName": branch or f"b{n}", "isDraft": draft, "baseRefName": base,
            "isCrossRepository": fork, "autoMergeRequest": {"enabledAt": "x"} if armed else None,
            "mergeStateStatus": merge, "url": f"https://x/pull/{n}", "labels": [{"name": lb} for lb in labels],
            "statusCheckRollup": checks or []}


class Gh:
    """`statuses` = sha -> newest-first list of {context, state}; records every write."""

    def __init__(self, statuses: dict[str, list[dict]] | None = None, refuse: tuple[str, ...] = (),
                 read_fails: bool = False, repo_fails: bool = False) -> None:
        self.statuses, self.refuse, self.read_fails, self.writes = statuses or {}, refuse, read_fails, []
        self.repo_fails = repo_fails

    def read(self, *args: str) -> subprocess.CompletedProcess:
        path = next(a for a in args if a.startswith("repos/"))
        if "/commits/" not in path:
            if self.repo_fails:
                raise subprocess.CalledProcessError(1, "gh")
            return subprocess.CompletedProcess(args, 0, json.dumps({"default_branch": "main"}), "")
        if self.read_fails:
            raise subprocess.CalledProcessError(1, "gh")
        assert path.endswith("/status"), path                        # the COMBINED status: newest per context
        sha = path.split("/commits/")[1].split("/")[0]
        return subprocess.CompletedProcess(args, 0, json.dumps({"statuses": self.statuses.get(sha, [])}), "")

    def write(self, *args: str, **_: object) -> subprocess.CompletedProcess:
        line = " ".join(args)
        self.writes.append(line)
        bad = any(r in line for r in self.refuse)
        err = "HTTP 403: Resource not accessible by personal access token" if bad else ""
        return subprocess.CompletedProcess(args, 1 if bad else 0, "", err)

    def posted(self) -> dict[int, str]:
        out = {}
        for w in self.writes:
            if "/statuses/" in w:
                sha = w.split("/statuses/")[1].split(" ")[0]
                out[int(sha)] = w.split("state=")[1].split(" ")[0]
        return out


def _pass(gh: Gh, prs: list[dict], comments: dict[int, list[str]] | None = None,
          eligible: frozenset[int] = frozenset()) -> tuple[int, list[str]]:
    summary: list[str] = []
    skip = pr_queue.blocked(prs, ("unit tests",), eligible)
    return pr_hold.hold("o/r", prs, gh.read, gh.write, summary, lambda repo, n: (comments or {}).get(n, []), skip), summary


def test_replay_td3083_merged_past_head_3082_is_held_must_fire() -> None:
    # 06:19:20Z 11-14: #3083 armed and green while #3082 was head; auto-merge landed #3083 first
    gh = Gh()
    errors, summary = _pass(gh, [_pr(3082), _pr(3083)])
    assert errors == 0 and gh.posted() == {3082: "success", 3083: "pending"}
    assert any("head #3082" in s for s in summary)
    assert "waits its turn: the queue head is #3082" in next(w for w in gh.writes if f"{3083:040d}" in w)


def test_a_pr_that_became_head_clears_its_stale_pending_in_one_pass_must_fire() -> None:
    gh = Gh({f"{3083:040d}": [{"context": pr_hold.CONTEXT, "state": "pending"}]})
    _pass(gh, [_pr(3083), _pr(3084)])                       # #3082 merged: #3083 is head now
    assert gh.posted()[3083] == "success"


def test_a_status_already_right_is_not_reposted_must_not_fire() -> None:
    gh = Gh({f"{3082:040d}": [{"context": pr_hold.CONTEXT, "state": "success"}, {"context": "x", "state": "failure"}],
             f"{3083:040d}": [{"context": pr_hold.CONTEXT, "state": "pending"}]})
    errors, _ = _pass(gh, [_pr(3082), _pr(3083)])
    assert errors == 0 and gh.writes == []


def test_a_former_head_behind_a_newly_armed_lower_pr_goes_pending_must_fire() -> None:
    gh = Gh({f"{3083:040d}": [{"context": pr_hold.CONTEXT, "state": "success"}]})
    _pass(gh, [_pr(3081), _pr(3083)])
    assert gh.posted() == {3081: "success", 3083: "pending"}


def test_a_train_lands_unarmed_drafts_wait_and_forks_are_not_touched() -> None:
    gh = Gh()
    _pass(gh, [_pr(3082), _pr(3090, armed=False, branch="train/7"), _pr(3079, armed=False),
               _pr(3078, draft=True), _pr(3070, fork=True)])
    assert gh.posted() == {3082: "success", 3090: "success", 3079: "pending", 3078: "pending"}
    assert "arm auto-merge to join it" in next(w for w in gh.writes if f"{3079:040d}" in w)


def test_the_head_is_marked_even_when_its_status_cannot_be_read_must_fire() -> None:
    gh = Gh(read_fails=True)
    errors, summary = _pass(gh, [_pr(3082), _pr(3083)])
    assert errors == 0 and gh.posted() == {3082: "success", 3083: "pending"}
    assert any("2 status read(s) failed" in s for s in summary)


def test_a_refused_head_comments_first_then_pages_once_must_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    pages: list[str] = []
    monkeypatch.setattr(pr_hold, "page", lambda title, body, click: pages.append(title) or "published (200)")
    head = f"repos/o/r/statuses/{3082:040d}"
    gh = Gh(refuse=(head,))
    errors, summary = _pass(gh, [_pr(3082), _pr(3083)])                       # pass 1: red + marker
    assert errors == 1 and pages == [] and any("HEAD HELD" in s for s in summary)
    marker = next(w for w in gh.writes if w.startswith("pr comment 3082"))
    assert f"queue:head-held sha={3082:040d} -->" in marker
    mark1 = pr_hold.HELD.format(sha=f"{3082:040d}", paged="")
    errors, summary = _pass(Gh(refuse=(head,)), [_pr(3082), _pr(3083)], {3082: [mark1]})   # pass 2: page
    assert errors == 1 and len(pages) == 1 and "#3082" in pages[0]
    mark2 = pr_hold.HELD.format(sha=f"{3082:040d}", paged=" paged")
    _pass(Gh(refuse=(head,)), [_pr(3082), _pr(3083)], {3082: [mark1, mark2]})               # pass 3: no re-page
    assert len(pages) == 1


def test_a_refused_non_head_is_red_but_never_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr_hold, "page", lambda *a: pytest.fail("paged for a non-head"))
    gh = Gh(refuse=(f"statuses/{3083:040d}",))
    errors, _ = _pass(gh, [_pr(3082), _pr(3083)])
    assert errors == 1 and not any(w.startswith("pr comment") for w in gh.writes)


def test_the_hold_runs_before_anything_else_can_fail_must_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(pr_autoupdate, "_gh_read", lambda *a: subprocess.CompletedProcess(a, 0, "{}", ""))
    monkeypatch.setattr(pr_autoupdate.pr_list, "rows", lambda raw: [_pr(3082)])
    monkeypatch.setattr(pr_hold, "hold", lambda *a, **k: order.append("hold") or 0)

    def heal(*a, **k):
        order.append("heal")
        raise subprocess.CalledProcessError(1, "gh")
    monkeypatch.setattr(pr_autoupdate.pr_heal, "assess_armed", heal)
    with pytest.raises(subprocess.CalledProcessError):
        pr_autoupdate.run("o/r", [], [], pr_autoupdate.Options(hold=True))
    assert order == ["hold", "heal"]
    order.clear()
    with pytest.raises(subprocess.CalledProcessError):
        pr_autoupdate.run("o/r", [], [], pr_autoupdate.Options())
    assert order == ["heal"]                                             # off unless the caller turns it on


def test_the_workflow_hands_queue_hold_to_the_tool_as_env() -> None:
    wf = yaml.safe_load((pathlib.Path(__file__).resolve().parent.parent / ".github/workflows/pr-autoupdate.yml")
                        .read_text(encoding="utf-8"))
    on = wf.get("on") or wf[True]
    assert on["workflow_call"]["inputs"]["queue-hold"]["default"] is False
    assert wf["jobs"]["update"]["env"]["CI_KIT_QUEUE_HOLD"] == "${{ inputs.queue-hold }}"


def test_a_stacked_pr_is_neither_head_nor_held_must_not_fire() -> None:
    # #3080 targets #3079's branch: as head it would hold every PR into main behind one that cannot land there
    gh = Gh()
    _pass(gh, [_pr(3080, base="b3079"), _pr(3082), _pr(3083)])
    assert gh.posted() == {3082: "success", 3083: "pending"}


def test_replay_td3079_renovate_merged_at_0649_while_3084_ran_must_fire() -> None:
    # 06:49:24Z 11-14: Renovate's #3079 (armed 06:35 by pr_reconcile) landed while #3084 ran unit tests.
    # Renovate PRs are ordered like any other: lowest number first. Armed, #3079 IS the head
    # (the 06:40Z scheduler run said so) and #3084 waits; its run against the old main is the cost
    # of being second, not an out-of-turn merge.
    renovate = dict(branch="renovate/npm-(non-major)")
    gh = Gh()
    _pass(gh, [_pr(3079, **renovate), _pr(3084, checks=RUNNING)])
    assert gh.posted() == {3079: "success", 3084: "pending"}
    # UNARMED, Renovate's own automerge (platformAutomerge off) goes through protection: held pending
    # until it is armed and its turn comes, so it can never land past the head #3084 again
    gh = Gh()
    _pass(gh, [_pr(3079, armed=False, **renovate), _pr(3084, checks=RUNNING)])
    assert gh.posted() == {3079: "pending", 3084: "success"}
    assert "arm auto-merge to join it" in next(w for w in gh.writes if f"{3079:040d}" in w)


def test_a_red_or_conflicting_head_is_skipped_so_main_does_not_freeze_must_fire() -> None:
    for blocked in (_pr(3082, checks=RED), _pr(3082, merge="DIRTY")):
        gh = Gh({f"{3082:040d}": [{"context": pr_hold.CONTEXT, "state": "success"}]})
        errors, summary = _pass(gh, [blocked, _pr(3083)])
        assert errors == 0 and gh.posted() == {3082: "pending", 3083: "success"}
        assert pr_hold.SKIPPED in next(w for w in gh.writes if f"{3082:040d}" in w)
        assert any("hold skips (cannot land as they stand): #3082" in s for s in summary)


def test_a_head_still_running_or_parked_or_red_off_the_required_list_keeps_the_head_must_not_fire() -> None:
    other_red = [{"name": "optional lint", "status": "COMPLETED", "conclusion": "FAILURE"}]
    for head, eligible in ((_pr(3082, checks=RUNNING), frozenset()), (_pr(3082, checks=RED), frozenset({3082})),
                           (_pr(3082, checks=other_red), frozenset())):
        gh = Gh()
        _pass(gh, [head, _pr(3083)], eligible=eligible)
        assert gh.posted() == {3082: "success", 3083: "pending"}


def test_a_fixed_head_takes_success_back_from_the_pr_that_stood_in_must_fire() -> None:
    gh = Gh({f"{3082:040d}": [{"context": pr_hold.CONTEXT, "state": "pending"}],
             f"{3083:040d}": [{"context": pr_hold.CONTEXT, "state": "success"}]})
    _pass(gh, [_pr(3082, checks=RUNNING), _pr(3083)])               # #3082's fix pushed: running again
    assert gh.posted() == {3082: "success", 3083: "pending"}


def test_default_branch_unread_marks_every_base_head_holds_nothing_and_goes_red_must_fire() -> None:
    gh = Gh(repo_fails=True)
    errors, summary = _pass(gh, [_pr(3080, base="b3079"), _pr(3082), _pr(3083), _pr(3090, armed=False, branch="train/1")])
    assert errors == 1 and gh.posted() == {3080: "success", 3082: "success", 3090: "success"}
    assert any("default branch UNREAD" in s for s in summary)


def test_a_malformed_ntfy_url_does_not_crash_the_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    def bad(*a):
        raise ValueError("unknown url type: 'x'")
    monkeypatch.setattr(pr_hold, "page", bad)
    head = f"repos/o/r/statuses/{3082:040d}"
    mark1 = pr_hold.HELD.format(sha=f"{3082:040d}", paged="")
    errors, summary = _pass(Gh(refuse=(head,)), [_pr(3082)], {3082: [mark1]})
    assert errors == 1 and any("push FAILED: unknown url type" in s for s in summary)


def test_hold_errors_turn_the_run_red_must_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr_autoupdate, "_gh_read", lambda *a: subprocess.CompletedProcess(a, 0, "{}", ""))
    monkeypatch.setattr(pr_autoupdate.pr_list, "rows", lambda raw: [])
    monkeypatch.setattr(pr_autoupdate.pr_heal, "assess_armed", lambda *a, **k: ({}, 0))
    monkeypatch.setattr(pr_autoupdate.pr_heal, "act", lambda *a, **k: 0)
    monkeypatch.setattr(pr_autoupdate, "unlabel_resolved", lambda *a, **k: 0)
    for held, rc in ((2, 1), (0, 0)):
        monkeypatch.setattr(pr_hold, "hold", lambda *a, held=held, **k: held)
        assert pr_autoupdate.run("o/r", [], [], pr_autoupdate.Options(hold=True)) == rc


@pytest.mark.parametrize(("env", "on"), [("true", True), ("1", True), (" True ", True), ("false", False), ("", False)])
def test_main_reads_queue_hold_from_env(monkeypatch: pytest.MonkeyPatch, env: str, on: bool) -> None:
    seen: list[bool] = []
    monkeypatch.setenv("CI_KIT_QUEUE_HOLD", env)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(pr_autoupdate, "run", lambda repo, summary, relock, opts: seen.append(opts.hold) or 0)
    pr_autoupdate.main(["--repo", "o/r"])
    assert seen == [on]
