"""The merge train's pure decisions, and "did the review gate pass at this exact head"."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ci_kit.merge_train import EJECTED, OPT_OUT, RIDING, github, policy

REQ = ("unit tests", "review gate")


def run(name: str, conclusion: str = "SUCCESS", status: str = "COMPLETED") -> dict:
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


def pr(n: int, **k) -> dict:
    base = {"number": n, "title": f"t{n}", "headRefName": f"b{n}", "headRefOid": f"{n:040x}",
            "isCrossRepository": False, "baseRefName": "main", "isDraft": False, "mergeable": "MERGEABLE",
            "autoMergeRequest": {"enabledAt": f"2026-11-11T00:0{n % 10}:00Z"}, "labels": [],
            "statusCheckRollup": [run("unit tests"), run("review gate")], "mergeStateStatus": "BEHIND"}
    base.update(k)
    return base


def test_checks_are_strict_about_an_absent_required_check() -> None:
    assert policy.checks([run("unit tests")], REQ) == ("pending", ["review gate"])
    assert policy.checks([run("unit tests"), run("review gate")], REQ) == ("green", [])
    assert policy.checks([run("unit tests", "FAILURE"), run("review gate", status="IN_PROGRESS")], REQ)[0] == "red"
    assert policy.checks([run("unit tests", "CANCELLED"), run("review gate")], REQ)[0] == "pending"
    assert policy.checks([], ())[0] == "pending"                          # nothing reported is not green
    assert policy.checks([run("x", "SKIPPED")], ())[0] == "green"
    assert policy.checks([{"__typename": "StatusContext", "context": "unit tests", "state": "ERROR"},
                          run("review gate")], REQ)[0] == "red"


def test_candidates_lowest_number_first_and_every_exclusion_named() -> None:
    # #1 armed LAST (re-armed after a push); it still leads -- the PR number is the landing order
    prs = [pr(3), pr(1, autoMergeRequest={"enabledAt": "2026-11-12T09:00:00Z"}), pr(2),
           pr(4, autoMergeRequest=None), pr(5, isDraft=True), pr(6, labels=[{"name": OPT_OUT}]),
           pr(7, title="wip [autoland-off]"), pr(8, labels=[{"name": EJECTED}]), pr(9, mergeable="CONFLICTING"),
           pr(10, statusCheckRollup=[run("unit tests", "FAILURE"), run("review gate")]),
           pr(11, isCrossRepository=True), pr(12, headRefName="train/42"), pr(13, baseRefName="dev"),
           pr(14, statusCheckRollup=[run("unit tests")])]
    ok, no = policy.candidates(prs, REQ, exclude=frozenset({2}))
    assert [p["number"] for p in ok] == [1, 3]
    why = {p["number"]: w for p, w in no}
    assert why[2] == "ejected from a train this run" and why[4] == "auto-merge not armed"
    assert why[5] == "a draft" and why[6] == why[7] == OPT_OUT and "ejected" in why[8]
    assert why[9] == "conflicts with main" and why[10].startswith("checks red")
    assert why[11] == "a fork" and why[12] == "a train" and "dev" in why[13]
    assert why[14] == "checks pending (review gate)"


def test_phase_of_an_open_train() -> None:
    t = pr(50, headRefName="train/1")
    assert policy.phase(t, "a", "b", 5, 90, REQ)[0] == "stale"
    assert policy.phase(t, "a", "a", 5, 90, REQ) == ("green", [])
    red = pr(50, statusCheckRollup=[run("unit tests", "FAILURE"), run("review gate")])
    assert policy.phase(red, "a", "a", 5, 90, REQ) == ("red", ["unit tests"])
    wait = pr(50, statusCheckRollup=[run("unit tests", status="IN_PROGRESS")])
    assert policy.phase(wait, "a", "a", 5, 90, REQ)[0] == "pending"
    assert policy.phase(wait, "a", "a", 91, 90, REQ)[0] == "stuck"


def test_bisect_split() -> None:
    assert policy.split([1, 2, 3, 4, 5, 6, 7, 8]) == ([1, 2, 3, 4], [5, 6, 7, 8])
    assert policy.split([1, 2, 3, 4, 5]) == ([1, 2, 3], [4, 5])
    assert policy.split([1, 2, 3, 4]) == ([1, 2], [3, 4])
    assert policy.split([1, 2, 3]) == ([], [1, 2, 3])
    assert policy.split([1, 2]) == ([], [1, 2])


def test_stray_riders() -> None:
    prs = [pr(1, labels=[{"name": RIDING}]), pr(2, labels=[{"name": RIDING}]), pr(3)]
    assert policy.stray_riders(prs, {1}) == [2]


# ── review passed at this exact head, against this base ────────────────────────────────────────
H = "a" * 40
B = "b" * 40
T1 = "2026-11-11T00:00:01Z"


def wr(i: int, conclusion: str = "success", status: str = "completed", n: int = 7, head: str = H, prs: bool = True,
       base: str = "main") -> dict:
    """A review-gate run AS GITHUB RETURNS IT: `pull_requests[].base`/`.head` are the PR's CURRENT
    state when asked (live: trade-data run 43105767267 reports a base committed after it ran), so
    `base` here is always the PR's current base, never what the run reviewed."""
    entry = {"number": n, "head": {"sha": head}, "base": {"ref": base, "sha": B}}
    return {"id": i, "created_at": f"2026-11-11T00:00:{i:02d}Z", "head_sha": head, "event": "pull_request",
            "status": status, "conclusion": conclusion, "head_branch": "feat",
            "pull_requests": [entry] if prs else []}


def test_review_verdict_both_directions() -> None:
    assert github.verdict([wr(1)], 7, H) == (True, github.verdict([wr(1)], 7, H)[1], T1)
    assert github.verdict([], 7, H)[:2] == (False, "no review-gate run names this PR at this head")
    assert github.verdict([wr(1), wr(2, "failure")], 7, H)[0] is False     # the newer block wins
    assert github.verdict([wr(1, "failure"), wr(2)], 7, H)[0] is True      # a newer pass wins
    assert "still in_progress" in github.verdict([wr(1), wr(2, None, "in_progress")], 7, H)[1]
    assert github.verdict([wr(1, n=8)], 7, H)[0] is False                  # another PR's run
    assert github.verdict([wr(1, head="b" * 40)], 7, H)[0] is False        # another head
    assert github.verdict([wr(1, prs=False)], 7, H)[0] is False            # empty pull_requests: refused
    two = wr(1)
    two["pull_requests"].append({"number": 9, "head": {"sha": H}, "base": {"ref": "main", "sha": B}})
    ok, why, _ = github.verdict([two], 7, H)
    assert not ok and "ambiguous" in why                                     # one head, two PRs


def test_a_review_run_older_than_the_last_retarget_is_no_pass_must_fire() -> None:
    # round 2, finding 1 (the reviewer's probe2): #7's gate ran while it targeted `feat`; it was
    # then retargeted to main -- the run now READS as base main, so only the timeline can tell
    retargeted_run = wr(1, base="main")
    ok, why, _ = github.verdict([retargeted_run], 7, H, retargeted="2026-11-11T00:05:00Z")
    assert not ok and "base branch was changed at 2026-11-11T00:05:00Z, after its last review run" in why
    assert github.verdict([retargeted_run], 7, H, retargeted=T1)[0] is False              # same second: not after
    after = {**wr(9), "created_at": "2026-11-11T00:06:00Z"}
    assert github.verdict([retargeted_run, after], 7, H, retargeted="2026-11-11T00:05:00Z")[0] is True  # re-reviewed
    assert github.verdict([retargeted_run], 7, H, retargeted="garbage")[0] is False        # unparseable: refused
    assert github.verdict([retargeted_run], 7, H, retargeted="")[0] is True                # never retargeted


def _timeline(monkeypatch, answer) -> list:
    calls: list = []

    def fake(*a):
        calls.append(a)
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(github, "gh_json", fake)
    return calls


REC = Path(__file__).parent / "fixtures" / "github"


def recorded(name: str) -> dict:
    """A REAL GitHub answer to github.RETARGETS, captured with `gh api graphql` (round 3: hand-written
    shapes hid this bug twice -- totalCount ignores the itemTypes filter)."""
    return json.loads((REC / f"timeline_{name}.json").read_text(encoding="utf-8"))


NEVER = ("trade-data_2990_never", "trade-data_2991_never", "ci-kit_69_never")
RETARGETED = {"home-host_199_retargeted": "2026-11-08T18:40:20Z", "uv_23126_retargeted": "2026-11-09T10:42:27Z"}


def test_recorded_timelines_decide_on_the_filtered_count_never_the_total_must_fire() -> None:
    for name in NEVER:  # totalCount 3 / 9 / 6, filteredCount 0: never retargeted
        got = recorded(name)
        assert got["data"]["repository"]["pullRequest"]["timelineItems"]["totalCount"] > 0
        assert github.last_retarget(got, 1) == "", name
    for name, when in RETARGETED.items():
        assert github.last_retarget(recorded(name), 1) == when, name


def test_a_timeline_answer_that_does_not_say_is_unreadable_must_fire() -> None:
    def tl(name: str, **items) -> dict:
        got = recorded(name)
        ti = got["data"]["repository"]["pullRequest"]["timelineItems"]
        for k, v in items.items():
            if v is None:
                ti.pop(k)
            else:
                ti[k] = v
        return got
    bad = [None, {"data": None}, {"errors": [{"message": "Resource not accessible by integration"}], "data": None},
           tl("trade-data_2990_never", filteredCount=None),           # no filtered count: never guess from total
           tl("home-host_199_retargeted", nodes=[]),                   # counted, not named
           tl("home-host_199_retargeted", nodes=[{"__typename": "BaseRefChangedEvent"}]),         # no time
           tl("home-host_199_retargeted", nodes=[{"__typename": "ReferencedEvent", "createdAt": "2026-11-08T18:40:20Z"}]),
           tl("trade-data_2990_never", nodes=recorded("home-host_199_retargeted")["data"]["repository"]
              ["pullRequest"]["timelineItems"]["nodes"])]            # counts none, lists one
    for got in bad:
        with pytest.raises(github.Unreadable):
            github.last_retarget(got, 7)


def test_retargeted_at_asks_the_filtered_count_and_wraps_a_refusal(monkeypatch) -> None:
    calls = _timeline(monkeypatch, recorded("trade-data_2990_never"))
    assert github.retargeted_at("o/r", 7) == ""
    q = calls[0][3]
    assert "BASE_REF_CHANGED_EVENT" in q and "filteredCount" in q and "totalCount" not in q and "pr=7" in calls[0]
    _timeline(monkeypatch, subprocess.CalledProcessError(1, ["gh"], "", "HTTP 403"))
    with pytest.raises(github.Unreadable):
        github.retargeted_at("o/r", 7)


def test_review_passed_refuses_a_closed_forked_or_retargeted_pr(monkeypatch) -> None:
    monkeypatch.setattr(github, "runs_at", lambda *a: [wr(1)])
    monkeypatch.setattr(github, "retargeted_at", lambda *a: "")
    same = {"state": "open", "head": {"ref": "feat", "repo": {"full_name": "o/r"}}, "base": {"ref": "main"}}
    assert github.review_passed("o/r", "review-gate.yml", 7, H, "main", same)[::2] == (True, T1)
    assert github.review_passed("o/r", "review-gate.yml", 7, H, "main", {**same, "state": "closed"})[0] is False
    fork = {**same, "head": {"ref": "feat", "repo": None}}
    assert github.review_passed("o/r", "review-gate.yml", 7, H, "main", fork)[0] is False
    stacked = {**same, "base": {"ref": "feat"}}
    assert github.review_passed("o/r", "review-gate.yml", 7, H, "main", stacked)[0] is False
    monkeypatch.setattr(github, "retargeted_at", lambda *a: "2026-11-11T00:05:00Z")
    assert github.review_passed("o/r", "review-gate.yml", 7, H, "main", same)[0] is False


# ── composed by the train ──────────────────────────────────────────────────────────────────────
def _train_pr(**k) -> dict:
    body = "x\n<!-- merge-train:composed run=55 branch=train/55-1 by=bot -->\n"
    return {"body": body, "head": {"ref": "train/55-1"}, "user": {"login": "bot"},
            "created_at": "2026-11-11T10:05:00Z", **k}


RUN = {"id": 55, "path": ".github/workflows/merge-train.yml", "repository": {"full_name": "o/r"},
       "status": "completed", "created_at": "2026-11-11T10:00:00Z", "updated_at": "2026-11-11T10:06:00Z"}


def test_composed_both_directions_must_fire() -> None:
    assert github.composed_ok("o/r", _train_pr(), RUN, "merge-train.yml") == ""
    assert "no composition record" in github.composed_ok("o/r", _train_pr(body="hand made"), RUN, "merge-train.yml")
    assert "opened by" in github.composed_ok("o/r", _train_pr(user={"login": "someone"}), RUN, "merge-train.yml")
    assert "not the record's train branch" in github.composed_ok("o/r", _train_pr(head={"ref": "train/56"}), RUN,
                                                                 "merge-train.yml")
    assert "does not exist" in github.composed_ok("o/r", _train_pr(), None, "merge-train.yml")
    assert "not the merge-train workflow" in github.composed_ok("o/r", _train_pr(), {**RUN, "path": ".github/workflows/ci.yml"},
                                                                "merge-train.yml")
    assert "not this repository" in github.composed_ok("o/r", _train_pr(), {**RUN, "repository": {"full_name": "x/y"}},
                                                       "merge-train.yml")
    assert "outside run" in github.composed_ok("o/r", _train_pr(created_at="2026-11-11T12:00:00Z"), RUN,
                                               "merge-train.yml")
    # the gate reads the run while the pass that opened the PR is still running: no upper bound yet
    live = {**RUN, "status": "in_progress", "updated_at": "2026-11-11T10:01:00Z"}
    assert github.composed_ok("o/r", _train_pr(), live, "merge-train.yml") == ""
    assert "outside run" in github.composed_ok("o/r", _train_pr(created_at="2026-11-11T09:00:00Z"), live,
                                               "merge-train.yml")


# ── a rider's history: eject after repeated stale/stuck, hold after repeated retries ────────────
def test_rider_gate_counts_releases_at_this_head_only() -> None:
    rel = lambda why, head=H, t=1: policy.RELEASED.format(train=t, head=head, why=why)  # noqa: E731
    assert policy.rider_gate([], H) == ("ride", "")
    # round 2, finding 2: stale (main moved) is never held against a rider, however often
    assert policy.rider_gate([rel("stale", t=t) for t in range(9)], H)[0] == "ride"
    assert policy.rider_gate([rel("stale"), rel("stuck", t=2)], H)[0] == "ride"
    assert policy.rider_gate([rel("stuck"), rel("stuck", t=2)], H)[0] == "hold"     # hold, never eject
    assert policy.rider_gate([rel("stuck"), rel("stuck", head=B)], H)[0] == "ride"   # a new head starts over
    assert policy.rider_gate([rel("retry"), rel("retry", t=2)], H)[0] == "hold"
    assert policy.rider_gate([rel("dropped"), rel("retry")], H)[0] == "ride"


def test_red_is_infra_only_when_no_check_gave_a_verdict() -> None:
    assert policy.red_is_infra([run("unit tests", "STARTUP_FAILURE"), run("review gate")], REQ) is True
    assert policy.red_is_infra([{"__typename": "StatusContext", "context": "unit tests", "state": "ERROR"}], REQ) is True
    assert policy.red_is_infra([run("unit tests", "STARTUP_FAILURE"), run("review gate", "FAILURE")], REQ) is False
    assert policy.red_is_infra([run("unit tests", "FAILURE")], REQ) is False
    assert policy.red_is_infra([run("other", "STARTUP_FAILURE")], REQ) is False     # not required: no red at all
