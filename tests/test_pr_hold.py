"""The queue hold (ci_kit/pr_hold.py): `queue/position` success on the HEAD only, so an armed PR
behind it cannot auto-merge out of turn. Each guard shown firing on its case, and not on the others."""
from __future__ import annotations

import json
import pathlib
import subprocess

import pytest
import yaml

from ci_kit import pr_autoupdate, pr_hold


def _pr(n: int, *, armed: bool = True, draft: bool = False, branch: str = "", fork: bool = False) -> dict:
    return {"number": n, "headRefOid": f"{n:040d}", "headRefName": branch or f"b{n}", "isDraft": draft,
            "isCrossRepository": fork, "autoMergeRequest": {"enabledAt": "x"} if armed else None,
            "mergeStateStatus": "CLEAN", "url": f"https://x/pull/{n}", "labels": []}


class Gh:
    """`statuses` = sha -> newest-first list of {context, state}; records every write."""

    def __init__(self, statuses: dict[str, list[dict]] | None = None, refuse: tuple[str, ...] = (),
                 read_fails: bool = False) -> None:
        self.statuses, self.refuse, self.read_fails, self.writes = statuses or {}, refuse, read_fails, []

    def read(self, *args: str) -> subprocess.CompletedProcess:
        if self.read_fails:
            raise subprocess.CalledProcessError(1, "gh")
        path = next(a for a in args if a.startswith("repos/"))
        sha = path.split("/commits/")[1].split("/")[0]
        return subprocess.CompletedProcess(args, 0, json.dumps(self.statuses.get(sha, [])), "")

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


def _pass(gh: Gh, prs: list[dict], comments: dict[int, list[str]] | None = None) -> tuple[int, list[str]]:
    summary: list[str] = []
    return pr_hold.hold("o/r", prs, gh.read, gh.write, summary, lambda repo, n: (comments or {}).get(n, [])), summary


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
    assert _pass(gh, [_pr(3082), _pr(3083)])[0] == 0 and gh.posted()[3082] == "success"


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
