"""The merge train's pure decisions, and "did the review gate pass at this exact head"."""
from __future__ import annotations

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


def test_candidates_oldest_armed_first_and_every_exclusion_named() -> None:
    prs = [pr(3), pr(1), pr(2),
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


# ── review passed at this exact head ───────────────────────────────────────────────────────────
H = "a" * 40


def wr(i: int, conclusion: str = "success", status: str = "completed", n: int = 7, head: str = H, prs: bool = True) -> dict:
    return {"id": i, "created_at": f"2026-11-11T00:00:{i:02d}Z", "head_sha": head, "event": "pull_request",
            "status": status, "conclusion": conclusion, "head_branch": "feat",
            "pull_requests": [{"number": n, "head": {"sha": head}}] if prs else []}


def test_review_verdict_both_directions() -> None:
    assert github.verdict([wr(1)], 7, H, "feat")[0] is True
    assert github.verdict([], 7, H, "feat") == (False, "no review-gate run at this head")
    assert github.verdict([wr(1), wr(2, "failure")], 7, H, "feat")[0] is False     # the newer block wins
    assert github.verdict([wr(1, "failure"), wr(2)], 7, H, "feat")[0] is True      # a newer pass wins
    assert "still in_progress" in github.verdict([wr(1), wr(2, None, "in_progress")], 7, H, "feat")[1]
    assert github.verdict([wr(1, n=8)], 7, H, "feat")[0] is False                  # another PR's run
    assert github.verdict([wr(1, head="b" * 40)], 7, H, "feat")[0] is False        # another head
    assert github.verdict([wr(1, prs=False)], 7, H, "feat")[0] is True             # empty pull_requests: by branch
    assert github.verdict([wr(1, prs=False)], 7, H, "other")[0] is False


def test_review_passed_refuses_a_closed_or_forked_pr(monkeypatch) -> None:
    monkeypatch.setattr(github, "runs_at", lambda *a: [wr(1)])
    same = {"state": "open", "head": {"ref": "feat", "repo": {"full_name": "o/r"}}}
    assert github.review_passed("o/r", "review-gate.yml", 7, H, same)[0] is True
    assert github.review_passed("o/r", "review-gate.yml", 7, H, {**same, "state": "closed"})[0] is False
    fork = {"state": "open", "head": {"ref": "feat", "repo": None}}
    assert github.review_passed("o/r", "review-gate.yml", 7, H, fork)[0] is False
