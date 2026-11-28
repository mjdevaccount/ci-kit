"""HEAD FAIL-FAST and THE HEAD HOLDS A SLOT (v1.45.0) -- must fire, both directions.

Two replays of one night on a consumer (2026-11-26, `max-in-flight: 1`, queue hold on, a merge
train configured, scarce pools sealed + heavy), each built from that night's pass logs and job
times:

  * #3201 -- armed, BEHIND, three fix pushes. The passes that looked said "its review has not
    recorded a verdict yet" while the model job had not started; CI ran on heads that could not
    merge and a session updated the branch by hand.
  * #3206 -- updated in the SAME pass as queue head #3205 (`queue: 2/1 in flight` one pass later),
    15.8 runner-minutes on a head that could not be the one that merged.

  * #3209 -- queue head, BEHIND from 06:19:54Z, its model review finished when the pass looked
    (the pass's runner took 229 s to arrive). The verdict was on record seven seconds later and
    no pass could act on it before a session updated the branch by hand: THE HEAD'S VERDICT IS
    WAITED FOR, in the pass's tail.

ORDER AND TIMING ONLY: nothing here skips a check. Every required check still passes at the head
that merges; what changes is which head the one full run is spent on.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib
import subprocess

import pytest
import yaml

import ci_kit.pr_autoupdate as upd
from ci_kit import doomed_runs, pr_queue, pr_sched

SEALED, HEAVY = "home-sealed", "home-heavy"
REQUIRED = ("lint + doc checks", "unit tests", "review gate", "image-pr-build")
NOW = 1_800_000_000.0


def _iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pr(n: int, *, state: str = "BEHIND", sha: str = "", labels: tuple[str, ...] = (), rollup: list[dict] | None = None) -> dict:
    return {"number": n, "isDraft": False, "isCrossRepository": False, "headRefName": f"b{n}",
            "headRefOid": sha or f"{n:040d}", "autoMergeRequest": {"enabledAt": "x"},
            "labels": [{"name": lb} for lb in labels], "mergeStateStatus": state, "statusCheckRollup": rollup or [],
            "title": f"pr {n}", "url": f"https://example.invalid/{n}", "baseRefName": "main"}


def _check(name: str, status: str, conclusion: str | None = None) -> dict:
    return {"name": name, "status": status, "conclusion": conclusion}


RUNNING = [_check("lint + doc checks", "IN_PROGRESS"), _check("unit tests", "QUEUED"), _check("review gate", "QUEUED")]


class Gh:
    """Routes `gh api <path>` to canned JSON (longest matching prefix); records every write."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes, self.writes = routes, []

    def __call__(self, *args: str, **_: object) -> subprocess.CompletedProcess:
        if "-X" in args:
            self.writes.append(" ".join(args))
            return subprocess.CompletedProcess(args, 0, "{}", "")
        path = next(a for a in args if a.startswith("repos/"))
        hit = max((k for k in self.routes if path.startswith(k)), key=len, default=None)
        body = self.routes[hit] if hit else {"workflow_runs": [], "jobs": []}
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")


def _job(name: str, status: str, *, started: float | None = None, conclusion: str | None = None) -> dict:
    return {"name": name, "status": status, "conclusion": conclusion, "labels": [SEALED],
            "created_at": _iso(NOW - 300), "started_at": _iso(started) if started else None}


def _review(branch: str, jobs: list[dict], rid: int = 9) -> dict[str, object]:
    """One active review-gate run on `branch` with these jobs."""
    run = {"id": rid, "event": "pull_request", "head_branch": branch, "status": "in_progress", "run_attempt": 1}
    return {"repos/o/r/actions/runs?status=queued": {"workflow_runs": []},
            "repos/o/r/actions/runs?status=in_progress": {"workflow_runs": [run]},
            f"repos/o/r/actions/runs/{rid}/jobs": {"jobs": jobs}}


OPTS = upd.Options(scarce=((HEAVY, 1), (SEALED, 1)), max_in_flight=1, required=REQUIRED, hold=True,
                   review_workflow="review-gate.yml", train_workflow="merge-train.yml", head_fail_fast=True)


# ── pr_sched.review_young: what "young" is, both directions ───────────────────────────────────
@pytest.mark.parametrize("jobs,young", [
    ([_job("reviewer / review", "queued")], True),                                            # nothing has run
    ([_job("reviewer / review", "completed"), _job("reviewer / model review", "queued")], True),   # waiting for the pool
    ([_job("reviewer / review", "completed"), _job("reviewer / model review", "in_progress", started=NOW - 60)], True),
    ([_job("reviewer / review", "completed"),
      _job("reviewer / model review", "completed", conclusion="skipped")], True),             # a carry: no model time
    ([_job("reviewer / review", "completed"), _job("reviewer / model review", "in_progress", started=NOW - 180)], False),
    ([_job("reviewer / review", "completed"), _job("reviewer / model review", "in_progress")], False),  # start unread
    ([_job("reviewer / review", "completed"),
      _job("reviewer / model review", "completed", conclusion="success")], False),            # the verdict is next
])
def test_a_review_is_young_until_its_model_job_has_cost_the_bound(jobs: list[dict], young: bool) -> None:
    got, why = pr_sched.review_young("o/r", "b1", Gh(_review("b1", jobs)), NOW, 180)
    assert got is young and bool(why) is not young


def test_a_branch_with_no_active_review_is_young() -> None:
    assert pr_sched.review_young("o/r", "b1", Gh({}), NOW, 180) == (True, "")


# ── replay: #3201's three fix pushes ──────────────────────────────────────────────────────────
# (pushed head, what the review run had done when the pass looked). Pass times: 04:21:44Z and
# 04:36:08Z are the passes that ran; the third push had NO pass for 10 min 38 s (a new head starts
# none without the caller's `synchronize` trigger), so its row is the pass that trigger starts.
PUSHES = [
    ("1ef80a9ed012ad3b7307d4a56b96844d51ac0f5e", [_job("reviewer / review", "queued")]),
    ("d937e6c6b65f9c800130e52a80698dc7632c743e", [_job("reviewer / review", "queued")]),
    ("d8a6ced8c09fb11fb8883fedc7ceca32e88bbda2",
     [_job("reviewer / review", "completed", conclusion="success"), _job("reviewer / model review", "queued")]),
]


def _pass_3201(monkeypatch: pytest.MonkeyPatch, sha: str, jobs: list[dict], opts: upd.Options) -> tuple[list[str], Gh, list[str]]:
    """One pass over the night's state at a fix push: (the head shas PUT, the write log, the summary)."""
    head = _pr(3201, sha=sha, rollup=RUNNING)
    later = _pr(3202, state="BLOCKED", rollup=RUNNING)   # the night's other armed PR: current, in flight
    prs = [head, later]
    ci = {"id": 71, "event": "pull_request", "head_sha": sha, "status": "in_progress", "name": "CI",
          "path": ".github/workflows/ci.yml", "pull_requests": [{"number": 3201}]}
    review = {"id": 9, "event": "pull_request", "head_sha": sha, "status": "in_progress", "name": "review-gate",
              "path": ".github/workflows/review-gate.yml", "pull_requests": [{"number": 3201}]}
    gh = Gh({**_review("b3201", jobs), f"repos/o/r/actions/runs?head_sha={sha}": {"workflow_runs": [ci, review]}})
    monkeypatch.setattr(upd, "_gh_read", gh)
    monkeypatch.setattr(upd, "reviewing", lambda *a: True)   # the review gate IS running at the pushed head
    puts: list[str] = []
    monkeypatch.setattr(upd, "_put", lambda repo, p: (puts.append(p["headRefOid"]), (upd.Outcome("updated"), ""))[1])
    summary: list[str] = []
    q = upd._train_plan(prs, prs, summary, opts)
    assert any(p is head and "checks still running" in why for p, why in q.held)   # pr_queue's rule: hold it
    moved = upd._head_first("o/r", prs, q, opts, summary)
    keep = upd._reserve(prs, q, opts, summary)
    upd._drive("o/r", q, moved, opts, [], summary, doomed_runs.Canceller("o/r", opts.review_workflow, gh, summary), None, keep)
    return puts, gh, summary


@pytest.mark.parametrize("sha,jobs", PUSHES)
def test_replay_td3201_each_fix_push_is_updated_at_once_and_ci_runs_once_must_fire(monkeypatch, sha, jobs) -> None:
    puts, gh, summary = _pass_3201(monkeypatch, sha, jobs, OPTS)
    assert puts == [sha]                                        # ONE update, pinned to the pushed head: no hand update
    cancelled = [w for w in gh.writes if w.endswith("/cancel")]
    assert cancelled == ["api -X POST repos/o/r/actions/runs/71/cancel"]   # the stale head's CI, never the review run
    assert any("head fail-fast" in s for s in summary)


@pytest.mark.parametrize("sha,jobs", PUSHES)
def test_replay_td3201_without_head_fail_fast_the_head_waits_on_its_stale_head_must_not_fire(monkeypatch, sha, jobs) -> None:
    # what ran that night: nothing is PUT, the CI at the stale head keeps its runners
    import dataclasses
    puts, gh, summary = _pass_3201(monkeypatch, sha, jobs, dataclasses.replace(OPTS, head_fail_fast=False))
    assert puts == [] and gh.writes == []
    assert any("its review has not recorded a verdict yet" in s for s in summary)


def test_a_head_whose_model_review_is_past_the_bound_waits_for_its_verdict_must_not_fire(monkeypatch) -> None:
    # #3206 as the 05:47:43Z pass would have read it: model review started 05:42:23Z, 5.3 min in
    jobs = [_job("reviewer / review", "completed", conclusion="success"),
            _job("reviewer / model review", "in_progress", started=NOW - 320)]
    monkeypatch.setattr(upd.time, "time", lambda: NOW)
    puts, gh, summary = _pass_3201(monkeypatch, "7" * 40, jobs, OPTS)
    assert puts == [] and gh.writes == []
    assert any("its model review has run 5.3 min; updated when it records" in s for s in summary)


def test_a_red_head_or_an_open_train_is_never_updated_by_fail_fast_must_not_fire(monkeypatch) -> None:
    monkeypatch.setattr(pr_sched, "review_settled", lambda *a: False)
    monkeypatch.setattr(pr_sched, "review_young", lambda *a: (True, ""))
    head = _pr(3201)
    for why in ("behind, a check is failing on its head (the author's fix re-queues it)", "merge train #9 is open"):
        q = pr_queue.Plan(held=[(head, why)])
        assert upd._head_first("o/r", [head], q, OPTS, []) is None and q.update == []
    q = pr_queue.Plan(held=[(_pr(3201, labels=("conflict",)), "behind, checks still running")])
    assert upd._head_first("o/r", [q.held[0][0]], q, OPTS, []) is None       # a conflict is the ladder's


# ── replay: #3206, updated beside head #3205 (pass 05:32:15Z) ─────────────────────────────────
def _night_0532() -> tuple[dict, dict, dict]:
    head = _pr(3205, rollup=[_check("unit tests", "IN_PROGRESS"), _check("review gate", "COMPLETED", "SUCCESS")])
    nxt = _pr(3206, labels=(pr_sched.PARKED,), rollup=[_check("unit tests", "COMPLETED", "CANCELLED")])
    red = _pr(3207, rollup=[_check("unit tests", "COMPLETED", "FAILURE")])
    return head, nxt, red


def _plan_0532(monkeypatch: pytest.MonkeyPatch, opts: upd.Options) -> tuple[pr_queue.Plan, list[str], int | None]:
    head, nxt, red = _night_0532()
    prs = [head, nxt, red]
    monkeypatch.setattr(pr_sched, "review_settled", lambda *a: True)   # #3205's verdict was recorded
    summary: list[str] = []
    skip = pr_queue.blocked(prs, REQUIRED, frozenset({3206}), "main")
    assert pr_queue.head(prs, skip) is head
    q = upd._train_plan(prs, prs, summary, opts, machinery=frozenset({3206}))
    assert [p["number"] for p in q.update] == [3206]                   # the plan gave the one slot to #3206
    upd._head_first("o/r", prs, q, opts, summary, skip)
    return q, summary, upd._reserve(prs, q, opts, summary, skip)


def test_replay_td3206_the_head_takes_the_slot_and_the_next_pr_is_not_updated_must_fire(monkeypatch) -> None:
    q, summary, keep = _plan_0532(monkeypatch, OPTS)
    assert [p["number"] for p in q.update] == [3205] and keep == 3205
    assert any(p["number"] == 3206 and "queued behind queue head #3205" in why for p, why in q.held)
    assert [p["number"] for p in q.spare] == [3206]
    assert any("#3206 not updated this pass" in s for s in summary)


def test_replay_td3206_without_the_hold_both_are_updated_as_before_must_not_fire(monkeypatch) -> None:
    import dataclasses
    q, _, keep = _plan_0532(monkeypatch, dataclasses.replace(OPTS, hold=False))
    assert [p["number"] for p in q.update] == [3205, 3206] and keep is None   # what ran: `queue: 2/1 in flight`


def test_reserve_never_takes_out_a_conflict_and_leaves_room_past_one_slot() -> None:
    head, a, b = _pr(1), _pr(2), _pr(3)
    dirty = _pr(4, state="DIRTY")
    q = pr_queue.Plan(update=[dirty, a, b], held=[(head, "behind, checks still running")])
    assert pr_queue.reserve(q, head, 1) == [a, b] and q.update == [dirty] and q.spare == [a, b]
    q = pr_queue.Plan(update=[head, a, b])
    assert pr_queue.reserve(q, head, 2) == [b] and q.update == [head, a]          # two slots: the head and one more
    q = pr_queue.Plan(update=[a], in_flight=[head])
    assert pr_queue.reserve(q, {**head, "mergeStateStatus": "BLOCKED"}, 1) == []  # a current head: nothing to reserve
    assert pr_queue.reserve(q, head, 1) == [] and q.update == [a]                 # in flight: it holds its slot already
    assert pr_queue.reserve(pr_queue.Plan(update=[a]), None, 1) == []
    assert pr_queue.reserve(pr_queue.Plan(update=[a], held=[(head, "x")]), head, 0) == []   # no queue


def test_an_open_merge_train_owns_the_lane_so_nothing_is_reserved_must_not_fire() -> None:
    head, ejected = _pr(1), _pr(2)
    q = pr_queue.Plan(update=[ejected], held=[(head, "merge train #9 is open")])
    assert upd._reserve([head, ejected], q, OPTS, []) is None and q.update == [ejected]


def test_the_heads_slot_is_not_handed_on_while_its_review_runs(monkeypatch) -> None:
    head, nxt = _pr(1), _pr(2)
    # ONLY the head is under review: #2 is free to take a slot, so `keep` is all that stops it
    monkeypatch.setattr(upd, "reviewing", lambda repo, branch, wf: branch == "b1")
    monkeypatch.setattr(upd, "_put", lambda repo, p: pytest.fail(f"#{p['number']} was updated"))
    summary: list[str] = []
    q = pr_queue.Plan(update=[head], spare=[nxt])
    upd._drive("o/r", q, None, OPTS, [], summary, None, None, 1)
    assert any("the queue head keeps its slot" in s for s in summary)
    puts: list[int] = []
    monkeypatch.setattr(upd, "reviewing", lambda repo, branch, wf: branch == "b1")
    monkeypatch.setattr(upd, "_put", lambda repo, p: (puts.append(p["number"]), (upd.Outcome("updated"), ""))[1])
    upd._drive("o/r", pr_queue.Plan(update=[head], spare=[nxt]), None, OPTS, [], [], None, None, None)
    assert puts == [2]                                                           # no head named: the old hand-on


def test_the_switch_is_on_by_default_and_reads_its_env(monkeypatch) -> None:
    seen: list[upd.Options] = []
    monkeypatch.setattr(upd, "run", lambda repo, summary, relock, opts, *a: seen.append(opts) or 0)
    for raw, on in ((None, True), ("true", True), ("false", False), ("0", False)):
        monkeypatch.delenv("CI_KIT_HEAD_FAIL_FAST", raising=False)
        if raw is not None:
            monkeypatch.setenv("CI_KIT_HEAD_FAIL_FAST", raw)
        assert upd.main(["--repo", "o/r"]) == 0 and seen[-1].head_fail_fast is on


def test_the_workflow_input_defaults_on_and_reaches_the_tool_as_env() -> None:
    wf = yaml.safe_load((pathlib.Path(__file__).resolve().parent.parent / ".github/workflows/pr-autoupdate.yml")
                        .read_text(encoding="utf-8"))
    on = wf.get("on") or wf[True]
    assert on["workflow_call"]["inputs"]["head-fail-fast"] == {**on["workflow_call"]["inputs"]["head-fail-fast"],
                                                               "type": "boolean", "default": True}
    assert wf["jobs"]["update"]["env"]["CI_KIT_HEAD_FAIL_FAST"] == "${{ inputs.head-fail-fast }}"


# ── replay: #3209, the verdict the pass waits for ─────────────────────────────────────────────
SHA9 = "d607ed617" + "0" * 31
WAIT = dataclasses.replace(OPTS, head_wait_s=240.0)
RED = [_check("unit tests", "COMPLETED", "FAILURE")]
# 06:24:05Z: `reviewer / review` and the model review done, `reviewer / verdict` 7 s from recording
DONE = [_job("reviewer / review", "completed", conclusion="success"),
        _job("reviewer / model review", "completed", conclusion="success"), _job("reviewer / verdict", "in_progress")]
SKIP = frozenset({3207})   # #3207, lower and armed, was red and behind that night: not the head


class Clock:
    def __init__(self) -> None:
        self.t, self.sleeps = NOW, []

    def time(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def _tail_3209(monkeypatch: pytest.MonkeyPatch, fresh: list[dict], settles_on_read: int | None,
               opts: upd.Options = WAIT) -> tuple[list[str], list[str], Clock]:
    """The pass's tail for head #3209: (head shas PUT, the tail's lines, the clock). The review reads
    settled from its `settles_on_read`-th read on (None = never); `fresh` is the re-read PR list."""
    clock, reads, puts, tail = Clock(), [], [], []
    monkeypatch.setattr(upd.time, "time", clock.time)
    monkeypatch.setattr(upd.time, "sleep", clock.sleep)
    monkeypatch.setattr(pr_sched, "review_settled",
                        lambda *a: (reads.append(1), settles_on_read is not None and len(reads) >= settles_on_read)[1])
    monkeypatch.setattr(pr_sched, "moved", lambda *a: False)
    monkeypatch.setattr(upd, "_read_prs", lambda repo, summary: fresh)
    monkeypatch.setattr(upd, "_put", lambda repo, p: (puts.append(p["headRefOid"]), (upd.Outcome("updated"), ""))[1])
    upd._await_head("o/r", _pr(3209, sha=SHA9, rollup=RUNNING), SKIP, opts, tail)
    return puts, tail, clock


def test_replay_td3209_the_pass_waits_for_the_verdict_and_updates_the_head_itself_must_fire(monkeypatch) -> None:
    red, head = _pr(3207, rollup=RED), _pr(3209, sha=SHA9, rollup=RUNNING)
    prs, summary, later = [red, head], [], []
    monkeypatch.setattr(upd, "_gh_read", Gh(_review("b3209", DONE)))
    monkeypatch.setattr(upd.time, "time", lambda: NOW)
    q = upd._train_plan(prs, prs, summary, WAIT)
    assert upd._head_first("o/r", prs, q, WAIT, summary, SKIP) is None and q.awaiting is head   # not updated mid-pass
    assert any("its model review has finished" in s and "which this pass waits for" in s for s in summary)
    upd._held_last("o/r", prs, q, WAIT, summary, later, SKIP)
    assert len(later) == 1                                       # the wait is the pass's tail, after its outputs
    # the verdict records 7 s on: the second read, 15 s into the wait, sees it; #3209 is still head and behind
    puts, tail, clock = _tail_3209(monkeypatch, [red, _pr(3209, sha=SHA9, rollup=RUNNING)], settles_on_read=2)
    assert puts == [SHA9] and clock.sleeps == [upd.HEAD_POLL_S]  # ONE update, by the queue: no hand update
    assert tail == ["#3209: queue head, verdict on record 15s into the wait -> updated"]


@pytest.mark.parametrize("fresh,why", [
    ([_pr(3209, sha=SHA9, rollup=[_check("review gate", "COMPLETED", "FAILURE")])], "a check is failing"),  # a red verdict
    ([_pr(3209, sha="e" * 40, rollup=RUNNING)], "a new head was pushed"),
    ([{**_pr(3209, sha=SHA9, rollup=RUNNING), "autoMergeRequest": None}], "no longer armed"),
    ([_pr(3208, rollup=RUNNING), _pr(3209, sha=SHA9, rollup=RUNNING)], "no longer the queue head (by what this pass read as blocked)"),
    ([_pr(3209, sha=SHA9, state="BLOCKED", rollup=RUNNING)], "merge state BLOCKED, not BEHIND"),        # already current
    ([_pr(3209, sha=SHA9, state="UNKNOWN", rollup=RUNNING)], "merge state UNKNOWN, not BEHIND"),        # not computed
    ([_pr(3209, sha=SHA9, labels=(upd.LABEL,), rollup=RUNNING)], "labelled `conflict`"),
    ([_pr(3207, rollup=RED)], "no longer open"),
])
def test_the_recorded_verdict_updates_nothing_the_fresh_read_refuses_must_not_fire(monkeypatch, fresh, why) -> None:
    puts, tail, _ = _tail_3209(monkeypatch, fresh, settles_on_read=1)
    assert puts == [] and len(tail) == 1 and "not updated: " + why in tail[0]


def test_a_verdict_that_does_not_come_ends_the_wait_at_its_bound_must_not_fire(monkeypatch) -> None:
    puts, tail, clock = _tail_3209(monkeypatch, [_pr(3209, sha=SHA9, rollup=RUNNING)], settles_on_read=None)
    assert puts == [] and sum(clock.sleeps) == WAIT.head_wait_s                    # never past the bound
    assert tail == ["#3209: queue head, still behind main -- no verdict within 240s of waiting; "
                    "the review's completion starts the pass that updates it"]


def test_a_failed_wait_is_one_line_and_a_warning_never_an_exception(monkeypatch, capsys) -> None:
    def boom(*a: object) -> bool:
        raise RuntimeError("api down")

    monkeypatch.setattr(pr_sched, "moved", lambda *a: False)
    monkeypatch.setattr(pr_sched, "review_settled", boom)
    tail: list[str] = []
    upd._await_head("o/r", _pr(3209, sha=SHA9), SKIP, WAIT, tail)
    assert tail == ["head verdict wait: FAILED RuntimeError: api down"]
    assert "::warning::head verdict wait: FAILED" in capsys.readouterr().out


def test_the_wait_is_off_at_zero_and_with_head_fail_fast_off_must_not_fire(monkeypatch) -> None:
    red, head = _pr(3207, rollup=RED), _pr(3209, sha=SHA9, rollup=RUNNING)
    monkeypatch.setattr(upd, "_gh_read", Gh(_review("b3209", DONE)))
    monkeypatch.setattr(upd.time, "time", lambda: NOW)
    for opts in (OPTS, dataclasses.replace(WAIT, head_fail_fast=False)):
        prs, summary, later = [red, head], [], []
        q = upd._train_plan(prs, prs, summary, opts)
        assert upd._head_first("o/r", prs, q, opts, summary, SKIP) is None and q.awaiting is None
        upd._held_last("o/r", prs, q, opts, summary, later, SKIP)
        assert later == [] and not any("this pass waits" in s for s in summary)


def test_the_held_pass_runs_before_the_wait_and_both_after_the_outputs(monkeypatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(upd, "_held_boundary", lambda *a: order.append("held"))
    monkeypatch.setattr(upd, "_await_head", lambda *a: order.append("wait"))
    q, later = pr_queue.Plan(awaiting=_pr(3209)), []
    opts = dataclasses.replace(WAIT, update_held=True)
    upd._held_last("o/r", [], q, opts, [], later, SKIP)
    assert order == [] and len(later) == 2                       # nothing ran: main runs them after the outputs
    for step in later:
        step([])
    assert order == ["held", "wait"]
    upd._held_last("o/r", [], q, opts, [], None, SKIP)            # a direct caller of run(): in place, same order
    assert order == ["held", "wait", "held", "wait"]


def test_the_wait_reads_its_env_and_a_bad_value_is_off_with_a_warning(monkeypatch, capsys) -> None:
    monkeypatch.delenv("CI_KIT_HEAD_VERDICT_WAIT_S", raising=False)
    assert upd.head_wait_s(True) == upd.HEAD_WAIT_S == 240.0 and upd.head_wait_s(False) == 0.0
    for raw, want in (("90", 90.0), ("0", 0.0), ("-5", 0.0), ("soon", 0.0)):
        monkeypatch.setenv("CI_KIT_HEAD_VERDICT_WAIT_S", raw)
        assert upd.head_wait_s(True) == want
    assert "head-verdict-wait-seconds is not a number ('soon')" in capsys.readouterr().out
    # main hands `later` to run whenever the wait is on, so the wait runs after relock= / heal= are written
    seen: list[tuple] = []
    monkeypatch.setattr(upd, "run", lambda repo, summary, relock, opts, *a: seen.append((opts.head_wait_s, len(a))) or 0)
    monkeypatch.setenv("CI_KIT_HEAD_VERDICT_WAIT_S", "240")
    monkeypatch.delenv("CI_KIT_UPDATE_HELD", raising=False)
    assert upd.main(["--repo", "o/r"]) == 0 and seen[-1] == (240.0, 2)
    monkeypatch.setenv("CI_KIT_HEAD_FAIL_FAST", "false")
    assert upd.main(["--repo", "o/r"]) == 0 and seen[-1] == (0.0, 1)


def test_the_wait_input_defaults_to_240_and_reaches_the_tool_as_env() -> None:
    wf = yaml.safe_load((pathlib.Path(__file__).resolve().parent.parent / ".github/workflows/pr-autoupdate.yml")
                        .read_text(encoding="utf-8"))
    on = wf.get("on") or wf[True]
    spec = on["workflow_call"]["inputs"]["head-verdict-wait-seconds"]
    assert (spec["type"], spec["default"]) == ("number", 240)
    assert wf["jobs"]["update"]["env"]["CI_KIT_HEAD_VERDICT_WAIT_S"] == "${{ inputs.head-verdict-wait-seconds }}"
    assert wf["jobs"]["update"]["timeout-minutes"] * 60 > 240 + 120   # the wait fits the job with the pass's own time


# ── review of v1.45.0 (2026-11-26): R1 R2 R4 R6, each red without its fix ─────────────────────
def _train(n: int = 100, *, state: str = "BLOCKED") -> dict:
    """An open merge train: live while BLOCKED with checks running, dead once main moved under it."""
    return {**_pr(n, state=state, rollup=RUNNING), "headRefName": "train/9", "autoMergeRequest": None,
            "createdAt": _iso(NOW - 60)}


def test_r1_a_pushs_before_never_comes_from_another_event_must_not_fire(monkeypatch) -> None:
    # `pull_request_target: synchronize` carries `before` too: the PR's PREVIOUS HEAD. Read as main's
    # previous sha, pr_sched.out_of_queue compares the head with its own old head and pages
    seen: list[upd.Options] = []
    monkeypatch.setattr(upd, "run", lambda repo, summary, relock, opts, *a: seen.append(opts) or 0)
    monkeypatch.setenv("CI_KIT_BEFORE", "1ef80a9ed012ad3b7307d4a56b96844d51ac0f5e")   # #3201's previous head
    monkeypatch.setenv("CI_KIT_SHA", "a" * 40)
    for event, want in (("pull_request_target", ""), ("workflow_run", ""), ("schedule", ""),
                        ("push", "1ef80a9ed012ad3b7307d4a56b96844d51ac0f5e")):
        monkeypatch.setenv("CI_KIT_EVENT", event)
        assert upd.main(["--repo", "o/r"]) == 0 and seen[-1].before == want
    wf = yaml.safe_load((pathlib.Path(__file__).resolve().parent.parent / ".github/workflows/pr-autoupdate.yml")
                        .read_text(encoding="utf-8"))
    assert wf["jobs"]["update"]["env"]["CI_KIT_BEFORE"] == "${{ github.event_name == 'push' && github.event.before || '' }}"


def test_r2_a_live_train_refuses_fail_fast_the_slot_and_the_tail_through_the_real_planner_must_not_fire(monkeypatch) -> None:
    # what `_train_plan` REALLY returns for a head with running checks beside a live train: the reason
    # stays "checks still running" -- no "merge train" in it -- so a guard reading the reason never fired
    head, nxt, train = _pr(3201, rollup=RUNNING), _pr(3202), _train()
    prs, summary = [head, nxt, train], []
    monkeypatch.setattr(upd, "_gh_read", Gh(_review("b3201", [_job("reviewer / review", "completed", conclusion="success"),
                                                              _job("reviewer / model review", "queued")])))
    monkeypatch.setattr(upd.time, "time", lambda: NOW)
    q = upd._train_plan(prs, [head, nxt], summary, WAIT)
    assert [w for p, w in q.held if p is head] == ["behind, checks still running (an update would cancel them)"]
    assert upd._head_first("o/r", prs, q, WAIT, summary) is None and q.update == []     # a young review, and still no
    assert any("merge train #100 holds the lane, not moved up" in s for s in summary)
    assert upd._reserve(prs, q, WAIT, summary) is None
    assert upd._not_now(head, head, prs, frozenset(), WAIT) == "a merge train holds the lane"


def test_r2_a_dead_train_holds_nothing_so_fail_fast_still_fires_must_fire(monkeypatch) -> None:
    head, train = _pr(3201, rollup=RUNNING), _train(state="BEHIND")           # main moved under the train
    prs, summary = [head, train], []
    monkeypatch.setattr(upd, "_gh_read", Gh(_review("b3201", [_job("reviewer / review", "queued")])))
    monkeypatch.setattr(upd.time, "time", lambda: NOW)
    q = upd._train_plan(prs, [head], summary, WAIT)
    assert upd._head_first("o/r", prs, q, WAIT, summary) == 3201 and q.update == [head]
    assert upd._not_now(head, head, prs, frozenset(), WAIT) == ""


def test_r4_a_pass_that_hands_on_a_relock_or_a_heal_does_not_wait_must_not_fire(monkeypatch) -> None:
    # a JOB's outputs reach the job that needs them when this job ENDS: the wait would hold a heal back
    q, later, summary = pr_queue.Plan(awaiting=_pr(3209)), [], []
    upd._held_last("o/r", [], q, WAIT, summary, later, SKIP, True)
    assert later == [] and summary == ["#3209: queue head, its verdict is NOT waited for -- this pass hands on a "
                                       "relock or a heal, which a waiting job would hold back"]
    upd._held_last("o/r", [], q, WAIT, summary, later, SKIP, False)
    assert len(later) == 1
    # and `run` tells `_held_last` the truth about its own outputs
    told: list[bool] = []
    monkeypatch.setattr(upd, "_read_prs", lambda repo, summary: [])
    monkeypatch.setattr(upd, "_gh_read", Gh({}))
    monkeypatch.setattr(upd, "_gh", Gh({}))
    monkeypatch.setattr(upd, "_held_last", lambda *a: told.append(a[-1]))
    for relock, healing in (([], []), ([7], []), ([], [{"pr": 7}]), ([], None)):
        assert upd.run("o/r", [], relock, upd.Options(), healing) == 0
    assert told == [False, True, True, False]


def test_r6_a_new_head_pushed_during_the_wait_ends_it_at_once_must_not_fire(monkeypatch) -> None:
    clock, tail = Clock(), []
    monkeypatch.setattr(upd.time, "time", clock.time)
    monkeypatch.setattr(upd.time, "sleep", clock.sleep)
    monkeypatch.setattr(pr_sched, "review_settled", lambda *a: False)
    monkeypatch.setattr(upd, "_put", lambda repo, p: pytest.fail("updated a head that was replaced"))
    # two reads at the waited-for head, then the branch's active run is at another head
    runs = iter([SHA9, SHA9, "e" * 40])
    monkeypatch.setattr(upd, "_gh_read", Gh({}))
    monkeypatch.setattr(pr_sched, "active_runs", lambda repo, read, branch="": [{"id": 9, "head_sha": next(runs)}])
    upd._await_head("o/r", _pr(3209, sha=SHA9, rollup=RUNNING), SKIP, WAIT, tail)
    assert clock.sleeps == [upd.HEAD_POLL_S] * 2                              # not the 240 s bound
    assert tail == ["#3209: queue head, a new head was pushed 30s into the wait -- the wait ends, "
                    "the pass that push starts decides"]


def test_r6_moved_reads_only_a_run_at_another_head() -> None:
    def gh(*shas: str | None) -> Gh:
        runs = [{"id": i, "head_sha": s} if s else {"id": i} for i, s in enumerate(shas)]
        return Gh({"repos/o/r/actions/runs?status=queued": {"workflow_runs": []},
                   "repos/o/r/actions/runs?status=in_progress": {"workflow_runs": runs}})

    assert pr_sched.moved("o/r", "b1", SHA9, gh()) is False                   # no active run: no evidence
    assert pr_sched.moved("o/r", "b1", SHA9, gh(SHA9, None)) is False         # a run that names no head: none either
    assert pr_sched.moved("o/r", "b1", SHA9, gh(SHA9, "e" * 40)) is True



# ── second review of v1.45.0 (2026-11-26) ─────────────────────────────────────────────────────
OLD9 = "a" * 40   # #3209's PREVIOUS head: its review run was cancelled by the push and has not ended
# what that cancelled run reads as while it is still active
CANCELLED = [_job("reviewer / review", "completed", conclusion="success"),
             _job("reviewer / model review", "completed", conclusion="cancelled")]
YOUNG = [_job("reviewer / review", "completed", conclusion="success"),
         _job("reviewer / model review", "in_progress", started=NOW - 60)]
OWED = [_job("reviewer / review", "completed", conclusion="success"),
        _job("reviewer / model review", "completed", conclusion="cancelled"), _job("reviewer / verdict", "in_progress")]
RECORDED = [_job("reviewer / review", "completed", conclusion="success"),
            _job("reviewer / model review", "completed", conclusion="success"),
            _job("reviewer / verdict", "completed", conclusion="success")]


def _two_heads(old_jobs: list[dict], now_jobs: list[dict], branch: str = "b3209") -> Gh:
    """Two ACTIVE review-gate runs on one branch: run 8 at the previous head (created first), run 9 at SHA9."""
    runs = [{"id": 8, "event": "pull_request", "head_branch": branch, "head_sha": OLD9, "status": "in_progress",
             "created_at": _iso(NOW - 900)},
            {"id": 9, "event": "pull_request", "head_branch": branch, "head_sha": SHA9, "status": "in_progress",
             "created_at": _iso(NOW - 300)}]
    return Gh({"repos/o/r/actions/runs?status=queued": {"workflow_runs": []},
               "repos/o/r/actions/runs?status=in_progress": {"workflow_runs": runs},
               "repos/o/r/actions/runs/8/jobs": {"jobs": old_jobs}, "repos/o/r/actions/runs/9/jobs": {"jobs": now_jobs}})


def test_review2_an_older_heads_active_run_is_not_read_for_this_head() -> None:
    # young: the old head's CANCELLED model job is not this head's finished review
    assert pr_sched.review_young("o/r", "b3209", _two_heads(CANCELLED, YOUNG), NOW, 180, SHA9) == (True, "")
    assert pr_sched.review_young("o/r", "b3209", _two_heads(CANCELLED, YOUNG), NOW, 180)[0] is False   # unfiltered: the bug
    # and a review at THIS head past the bound is still not young
    assert pr_sched.review_young("o/r", "b3209", _two_heads(CANCELLED, DONE), NOW, 180, SHA9)[0] is False
    # settled: the old head's run owes this head nothing; this head's own verdict is what counts
    assert pr_sched.review_settled("o/r", "b3209", _two_heads(OWED, RECORDED), SHA9) is True
    assert pr_sched.review_settled("o/r", "b3209", _two_heads(OWED, RECORDED)) is False               # unfiltered
    assert pr_sched.review_settled("o/r", "b3209", _two_heads(RECORDED, YOUNG), SHA9) is False        # still owed HERE
    # a run that names no head is read, as before
    assert pr_sched.review_settled("o/r", "b1", Gh(_review("b1", YOUNG)), SHA9) is False


def test_review2_the_pass_reads_the_review_at_the_heads_own_sha_must_fire(monkeypatch) -> None:
    """Through the real planner and the real reads: the previous head's cancelled run is still
    active, this head's review is 60 s into its model job -> fail-fast fires."""
    head = _pr(3209, sha=SHA9, rollup=RUNNING)
    prs = [head, _pr(3210, state="BLOCKED", rollup=RUNNING)]
    monkeypatch.setattr(upd, "_gh_read", _two_heads(CANCELLED, YOUNG))
    monkeypatch.setattr(upd, "reviewing", lambda *a: True)
    monkeypatch.setattr(upd.time, "time", lambda: NOW)
    summary: list[str] = []
    q = upd._train_plan(prs, prs, summary, OPTS)
    assert upd._head_first("o/r", prs, q, OPTS, summary) == 3209 and q.update[0] is head
    assert any("head fail-fast" in s for s in summary)


def test_review2_the_wait_is_not_ended_or_stretched_by_the_previous_heads_run(monkeypatch) -> None:
    """The tail's wait with the real reads: run 8 (previous head, still cancelling, owes a verdict
    it will never record) beside run 9 (this head, verdict on record). Not a push, and settled."""
    clock, puts, tail = Clock(), [], []
    head = _pr(3209, sha=SHA9, rollup=RUNNING)
    monkeypatch.setattr(upd.time, "time", clock.time)
    monkeypatch.setattr(upd.time, "sleep", clock.sleep)
    monkeypatch.setattr(upd, "_gh_read", _two_heads(OWED, RECORDED))
    monkeypatch.setattr(upd, "_read_prs", lambda repo, summary: [head])
    monkeypatch.setattr(upd, "_put", lambda repo, p: (puts.append(p["headRefOid"]), (upd.Outcome("updated"), ""))[1])
    upd._await_head("o/r", head, SKIP, WAIT, tail)
    assert puts == [SHA9] and clock.sleeps == [], tail


def test_review2_moved_is_a_run_newer_than_this_heads_never_an_older_one() -> None:
    def gh(*runs: tuple[str, float | None]) -> Gh:
        rows = [{"id": i, "head_sha": s, **({"created_at": _iso(t)} if t else {})} for i, (s, t) in enumerate(runs)]
        return Gh({"repos/o/r/actions/runs?status=queued": {"workflow_runs": []},
                   "repos/o/r/actions/runs?status=in_progress": {"workflow_runs": rows}})

    assert pr_sched.moved("o/r", "b1", SHA9, gh((SHA9, NOW - 300), (OLD9, NOW - 900))) is False   # the previous head's run
    assert pr_sched.moved("o/r", "b1", SHA9, gh((SHA9, NOW - 300), ("e" * 40, NOW - 30))) is True  # a push
    assert pr_sched.moved("o/r", "b1", SHA9, gh((SHA9, NOW - 300), ("e" * 40, None))) is True      # age unread: a push
    assert pr_sched.moved("o/r", "b1", SHA9, gh(("e" * 40, NOW - 30))) is True                     # nothing left at this head


def test_review2_the_wait_is_clamped_to_300_s_with_a_warning(monkeypatch, capsys) -> None:
    for raw, want in (("300", 300.0), ("301", 300.0), ("900", 300.0), ("inf", 300.0), ("240", 240.0)):
        monkeypatch.setenv("CI_KIT_HEAD_VERDICT_WAIT_S", raw)
        assert upd.head_wait_s(True) == want
    out = capsys.readouterr().out
    assert out.count("::warning::head-verdict-wait-seconds") == 3 and "900 is over the 300 s" in out
    monkeypatch.delenv("CI_KIT_HEAD_VERDICT_WAIT_S")
    assert upd.head_wait_s(True) == upd.HEAD_WAIT_S == 240.0                   # the default is under the clamp
    root = pathlib.Path(__file__).resolve().parent.parent
    wf = yaml.safe_load((root / ".github/workflows/pr-autoupdate.yml").read_text(encoding="utf-8"))
    inputs = (wf.get("on") or wf[True])["workflow_call"]["inputs"]
    # the clamp leaves the pass its own time inside the job, and the input says so
    assert wf["jobs"]["update"]["timeout-minutes"] * 60 - upd.HEAD_WAIT_MAX_S >= 300
    assert "CLAMPED to 300" in inputs["head-verdict-wait-seconds"]["description"]
    # what the wait holds is said where a consumer sets it: the verdict job shares the pool
    for name in ("head-verdict-wait-seconds", "scarce-pools", "head-fail-fast"):
        assert "verdict" in inputs[name]["description"] and "pool" in inputs[name]["description"], name
    assert "blocks its own verdict" in (root / "docs/pr-ops/autoupdate.md").read_text(encoding="utf-8")
