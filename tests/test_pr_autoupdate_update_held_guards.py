"""`update-held`'s guards: what a held PR must NEVER cost, one test per rule (review of ci-kit#111).

  D1 every check on the head counts, not only `required-checks`: one running or failing = skipped, named;
  D2 a held PR is never labelled, paged, put in `relock` or fed to the heal ladder -- a conflict race or
     an error on its update is ONE line, `#N: held, not updated: <why>`;
  D3 the held pass runs LAST, and nothing it raises changes the armed pass's result or exit code: one
     `update-held: FAILED` line, one warning annotation, counted in the closing line's errors;
  D4 a held PR whose own checks are running holds a queue slot for the held budget (armed never waits);
  D5 "armed first" = every armed PR not yet current (BEHIND or UNKNOWN), wherever the queue put it;
  D6 a head younger than pr_held.YOUNG_S (or of unreadable age) is skipped: someone may be pushing;
  D7 any bot's PR is skipped, not just Renovate's.
Plus a killing test for each mutant the review found surviving (M1-M6).
"""
from __future__ import annotations

import subprocess
import time
from datetime import UTC, datetime

import pytest

import ci_kit.pr_autoupdate as upd
from ci_kit import pr_held, pr_queue
from ci_kit.merge_train import RIDING

OLD = "2026-11-01T00:00:00Z"


class _Done:
    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, err


def _pr(n: int, armed: bool = False, **k) -> dict:
    return {"number": n, "title": "t", "url": "u", "headRefOid": "a" * 40, "headRefName": f"b{n}", "labels": [],
            "isCrossRepository": False, "isDraft": False, "mergeStateStatus": "BEHIND", "mergeable": "MERGEABLE",
            "author": {"__typename": "User", "login": "someone"}, "headCommittedDate": OLD,
            "autoMergeRequest": {"enabledAt": OLD} if armed else None, "statusCheckRollup": [], **k}


def _check(name: str, status: str = "COMPLETED", conclusion: str | None = "SUCCESS") -> dict:
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


RUNNING = [_check("lint", "IN_PROGRESS", None)]


class World:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, rows: list[dict], put: tuple[int, str] = (0, "")) -> None:
        self.calls: list[tuple] = []
        self.pages: list[tuple] = []
        monkeypatch.setattr(upd.pr_list, "rows", lambda _: rows)
        monkeypatch.setattr(upd, "_gh_read", lambda *a: _Done(out="{}"))
        monkeypatch.setattr(upd, "reviewing", lambda repo, branch, wf: False)
        monkeypatch.setattr(upd, "page", lambda *a: self.pages.append(a) or "pushed")

        def gh(*a: str, **k) -> _Done:
            self.calls.append(a)
            if a[:3] == ("api", "-X", "PUT"):
                return _Done(*put)
            if a[:2] == ("pr", "view"):
                return _Done(out="OPEN")
            return _Done()
        monkeypatch.setattr(upd, "_gh", gh)

    def puts(self) -> list[int]:
        return [int(a[3].split("/")[4]) for a in self.calls if a[:3] == ("api", "-X", "PUT")]

    def run(self, **opts) -> tuple[int, list[str], list[int]]:
        summary: list[str] = []
        relock: list[int] = []
        rc = upd.run("o/r", summary, relock, upd.Options(**{"update_held": True, "event": "schedule", **opts}))
        return rc, summary, relock


def _held_lines(summary: list[str], n: int) -> list[str]:
    return [s for s in summary if s.startswith(f"#{n}:")]


# ---- D1: every check on the head, not only the required ones

def test_d1_a_non_required_check_running_or_failing_blocks_a_held_pr_named(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch, [_pr(2, statusCheckRollup=[_check("unit"), _check("e2e", "IN_PROGRESS", None)]),
                            _pr(3, statusCheckRollup=[_check("unit"), _check("docs", conclusion="FAILURE")])])
    rc, summary, _ = w.run(required=("unit",))
    assert rc == 0 and w.puts() == []
    assert _held_lines(summary, 2) == ["#2: held, not updated: check `e2e` still running (an update would cancel it)"]
    assert _held_lines(summary, 3) == ["#3: held, not updated: check `docs` is failing on its head "
                                       "(the author's push re-runs it)"]


def test_d1_the_armed_queue_still_reads_required_checks_only() -> None:
    p = _pr(1, armed=True, statusCheckRollup=[_check("unit"), _check("e2e", "IN_PROGRESS", None)])
    assert pr_queue.check_state(p["statusCheckRollup"], ("unit",)) == "green"   # unchanged semantics
    assert pr_queue.plan([p], 1, ("unit",)).update == [p]


# ---- D2: a held PR is never labelled, paged, relocked or laddered

@pytest.mark.parametrize("answer,why", [
    ((1, '{"message": "merge conflict between base and head"}'), "conflict (merge conflict between base and head)"),
    ((1, '{"message": "Bad credentials"}'), "error (Bad credentials)"),
])
def test_d2_a_held_update_failure_is_one_line_and_nothing_else(monkeypatch: pytest.MonkeyPatch, capsys,
                                                                 answer: tuple[int, str], why: str) -> None:
    w = World(monkeypatch, [_pr(2)], put=answer)
    rc, summary, relock = w.run(ladder=upd.pr_ladder.Config(on=True))
    assert rc == 0
    assert _held_lines(summary, 2) == [f"#2: held, not updated: {why}"]
    assert relock == [] and w.pages == []
    assert not [a for a in w.calls if a[:2] == ("pr", "edit") or a[:2] == ("label", "create")]
    assert "::error::" not in capsys.readouterr().out


# ---- D3: last, contained, never red, never silent

def test_d3_the_held_pass_runs_after_the_armed_pass_and_cannot_turn_it_red(monkeypatch: pytest.MonkeyPatch,
                                                                         capsys) -> None:
    stale = _pr(3, mergeStateStatus="CLEAN", labels=[{"name": "conflict"}])   # unlabel_resolved's work
    w = World(monkeypatch, [_pr(2), stale])

    def reviewing(repo: str, branch: str, wf: str) -> bool:
        raise subprocess.CalledProcessError(1, "gh run list", "", "boom")
    monkeypatch.setattr(upd, "reviewing", reviewing)
    rc, summary, _ = w.run(review_workflow="review.yml")
    assert rc == 0
    unlabel = summary.index(next(s for s in summary if s.startswith("#3: `conflict` label removed")))
    failed = summary.index("update-held: FAILED CalledProcessError: Command 'gh run list' returned non-zero exit status 1.")
    assert unlabel < failed                                              # the armed pass finished first
    assert summary[-1] == "update-held: 0 held PR(s) updated of 1 eligible, 0 skipped, 1 error(s)"
    out = capsys.readouterr().out
    assert "::warning::update-held: FAILED CalledProcessError" in out and "::error::" not in out


def test_d3_an_armed_error_still_turns_the_run_red_with_the_held_pass_on(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch, [_pr(1, armed=True)], put=(1, '{"message": "Bad credentials"}'))
    rc, _, _ = w.run()
    assert rc == 1


# ---- D4: a held PR running its checks holds a slot (for the held budget only)

def test_d4_a_held_pr_with_running_checks_occupies_a_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch, [_pr(2, mergeStateStatus="BLOCKED", statusCheckRollup=RUNNING), _pr(3)])
    _, summary, _ = w.run(max_in_flight=1)
    assert w.puts() == []
    assert "#3: held, not updated: no free queue slot (in flight: 0 armed + 1 held of 1)" in summary


def test_d4_armed_prs_never_wait_on_a_held_one(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch, [_pr(1, armed=True), _pr(2, mergeStateStatus="BLOCKED", statusCheckRollup=RUNNING)])
    w.run(max_in_flight=1)
    assert w.puts() == [1]


# ---- D5: armed first = every armed PR not yet current

def test_d5_an_armed_pr_behind_with_checks_running_holds_the_held_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch, [_pr(1, armed=True, statusCheckRollup=RUNNING), _pr(3)])
    _, summary, _ = w.run(max_in_flight=2)
    assert w.puts() == []
    assert "#3: held, not updated: an armed PR needed an update this pass (armed PRs go first)" in summary


# ---- D6: a young head (or an unreadable date) is skipped

def test_d6_a_young_or_undated_head_is_skipped_named(monkeypatch: pytest.MonkeyPatch) -> None:
    young = datetime.fromtimestamp(time.time() - 120, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    w = World(monkeypatch, [_pr(2, headCommittedDate=young), _pr(3, headCommittedDate=None),
                            _pr(4, headCommittedDate="not a date")])
    _, summary, _ = w.run()
    assert w.puts() == []
    assert _held_lines(summary, 2) == ["#2: held, not updated: head commit is 2 min old (someone may be pushing; "
                                       "waits 10 min)"]
    assert _held_lines(summary, 3) == ["#3: held, not updated: head commit date unreadable"]
    assert _held_lines(summary, 4) == ["#4: held, not updated: head commit date unreadable"]


def test_d6_the_date_rides_the_existing_pr_list_query() -> None:
    from ci_kit import pr_list
    assert "commit { committedDate statusCheckRollup" in pr_list.QUERY
    node = {"number": 1, "commits": {"nodes": [{"commit": {"committedDate": OLD, "statusCheckRollup": None}}]}}
    assert pr_list.rows({"data": {"repository": {"pullRequests": {"nodes": [node]}}}})[0]["headCommittedDate"] == OLD


# ---- D7: every bot, not one

@pytest.mark.parametrize("author", [{"__typename": "Bot", "login": "dependabot"}, {"login": "some-app[bot]"}])
def test_d7_any_bots_pr_is_skipped(monkeypatch: pytest.MonkeyPatch, author: dict) -> None:
    w = World(monkeypatch, [_pr(2, author=author)])
    _, summary, _ = w.run()
    assert w.puts() == []
    assert _held_lines(summary, 2) == [f"#2: held, not updated: a bot's PR ({author['login']})"]


# ---- the review's surviving mutants (finding 6)

def test_m1_an_unknown_armed_pr_holds_the_held_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch, [_pr(1, armed=True, mergeStateStatus="UNKNOWN"), _pr(2)])
    _, summary, _ = w.run()
    assert 2 not in w.puts()
    assert "#2: held, not updated: an armed PR needed an update this pass (armed PRs go first)" in summary


def test_m2_behind_but_mergeable_not_computed_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch, [_pr(2, mergeable="UNKNOWN")])
    _, summary, _ = w.run()
    assert w.puts() == [] and _held_lines(summary, 2) == ["#2: held, not updated: merge state not computed yet"]


def test_m3_a_failing_required_check_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch, [_pr(2, statusCheckRollup=[_check("unit", conclusion="FAILURE")])])
    _, summary, _ = w.run(required=("unit",))
    assert w.puts() == [] and "failing" in _held_lines(summary, 2)[0]


def test_m4_a_rider_label_alone_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch, [_pr(2, labels=[{"name": RIDING}])])
    _, summary, _ = w.run()
    assert w.puts() == [] and _held_lines(summary, 2) == ["#2: held, not updated: a merge train branch or rider"]


def test_m5_an_armed_pr_in_the_spare_list_alone_holds_the_held_pass() -> None:
    q = pr_queue.Plan(spare=[_pr(1, armed=True)])
    todo, skipped = pr_held.admit([_pr(2)], [_pr(2)], q, 3)
    assert todo == [] and "armed PRs go first" in skipped[0][1]


def test_m6_held_errors_are_counted_in_the_closing_line(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch, [_pr(2), _pr(3)], put=(1, '{"message": "Bad credentials"}'))
    rc, summary, _ = w.run()
    assert rc == 0
    assert summary[-1] == "update-held: 0 held PR(s) updated of 2 eligible, 0 skipped, 2 error(s)"
