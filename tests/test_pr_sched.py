"""The PR scheduler (ci_kit/pr_queue.py policy + ci_kit/pr_sched.py actor) -- must fire, both directions.

ORDER AND TIMING ONLY: every predicate here moves WHEN a job runs. Nothing skips a check: a parked
review is red until it is re-run and reviewed (tests/test_review_gate_split_must_fire.py pins
that the workflow cannot read a parked or skipped review as a pass).
"""
from __future__ import annotations

import json
import subprocess

import pytest

from ci_kit import pr_admit, pr_heal, pr_queue, pr_sched, redclass

NOW = 1_800_000_000.0
SEALED = "home-sealed"


def _pr(n: int, *, armed: bool = True, labels: tuple[str, ...] = (), state: str = "BLOCKED", **kw) -> dict:
    return {"number": n, "isDraft": False, "isCrossRepository": False, "headRefName": f"b{n}",
            "headRefOid": f"{n:040d}", "autoMergeRequest": {"enabledAt": "x"} if armed else None,
            "labels": [{"name": lb} for lb in labels], "mergeStateStatus": state, **kw}


def _job(pr: int, run: int, status: str, *, started: float | None = None, created: float = NOW - 60,
         attempt: int = 1, pool: str = SEALED) -> pr_queue.Job:
    return pr_queue.Job(pr, run, attempt, "model review", pool, status, created, started)


# ── policy: who is the head ───────────────────────────────────────────────────────────────────
def test_the_head_is_the_lowest_armed_member() -> None:
    prs = [_pr(3080), _pr(3073), _pr(3070, armed=False), {**_pr(3060), "isDraft": True},
           {**_pr(3050), "isCrossRepository": True}, {**_pr(3040), "headRefName": "train/x"}]
    assert pr_queue.head(prs)["number"] == 3073
    assert pr_queue.head([_pr(1, armed=False)]) is None


def test_an_unarmed_renovate_pr_is_never_the_head() -> None:
    # a major bump left open for a human must not hold the head's priority for a week
    ren = {**_pr(3000, armed=False), "headRefName": "renovate/major-x", "author": {"login": "app/renovate"}}
    assert pr_queue.head([ren, _pr(3073)])["number"] == 3073
    assert pr_queue.head([{**ren, "autoMergeRequest": {"enabledAt": "x"}}, _pr(3073)])["number"] == 3000


# ── policy: admission (the review gate's park) ────────────────────────────────────────────────
def test_admission_parks_only_a_later_pr_while_the_head_needs_the_pool() -> None:
    head = _pr(3073)
    assert pr_queue.admit(_pr(3080), head, True)[0] is True
    assert pr_queue.admit(_pr(3080), head, False)[0] is False      # work-conserving
    assert pr_queue.admit(_pr(3073), head, True)[0] is False       # the head itself
    assert pr_queue.admit(_pr(3070), head, True)[0] is False       # ahead of the head
    assert pr_queue.admit(_pr(3080), None, True)[0] is False       # no queue


# ── policy: preemption on a scarce pool ───────────────────────────────────────────────────────
def test_a_queued_non_head_job_yields_to_a_waiting_head_must_fire() -> None:
    s = pr_queue.schedule(3073, [_job(3073, 1, "queued"), _job(3080, 2, "queued")], {SEALED: 1}, NOW)
    assert [j.run for j, _ in s.preempt] == [2] and SEALED in s.demand


def test_a_young_holder_is_preempted_an_old_one_finishes() -> None:
    young = _job(3080, 2, "in_progress", started=NOW - 60)
    old = _job(3081, 3, "in_progress", started=NOW - 600)
    head = _job(3073, 1, "queued")
    assert [j.run for j, _ in pr_queue.schedule(3073, [head, young], {SEALED: 1}, NOW).preempt] == [2]
    assert pr_queue.schedule(3073, [head, old], {SEALED: 1}, NOW).preempt == []


def test_preemption_takes_only_what_the_head_needs() -> None:
    jobs = [_job(3073, 1, "queued"), _job(3080, 2, "in_progress", started=NOW - 30),
            _job(3081, 3, "in_progress", started=NOW - 90)]
    got = pr_queue.schedule(3073, jobs, {SEALED: 2}, NOW).preempt
    assert [j.run for j, _ in got] == [2]   # one head job, two slots full: the youngest yields
    assert pr_queue.schedule(3073, jobs, {SEALED: 3}, NOW).preempt == []  # a free slot: nothing yields


def test_never_preempted_the_head_a_main_run_or_a_run_parked_often_enough_must_not_fire() -> None:
    head = _job(3073, 1, "queued")
    for other in (_job(0, 2, "in_progress", started=NOW - 10), _job(0, 2, "queued"),
                  _job(3080, 2, "queued", attempt=4), _job(3073, 2, "in_progress", started=NOW - 10)):
        assert pr_queue.schedule(3073, [head, other], {SEALED: 1}, NOW).preempt == [], other


def test_no_head_demand_means_no_preemption_must_not_fire() -> None:
    assert pr_queue.schedule(3073, [_job(3080, 2, "queued")], {SEALED: 1}, NOW).preempt == []
    assert pr_queue.schedule(None, [_job(3080, 2, "queued")], {SEALED: 1}, NOW).preempt == []


def test_a_starved_head_is_named() -> None:
    jobs = [_job(3073, 1, "queued", created=NOW - 1000), _job(3080, 2, "in_progress", started=NOW - 900)]
    s = pr_queue.schedule(3073, jobs, {SEALED: 1}, NOW, stall_s=900)
    assert s.stalled and "#3080" in s.stalled[0]
    assert pr_queue.schedule(3073, jobs, {SEALED: 1}, NOW, stall_s=1200).stalled == []


# ── redclass + pr_heal: a parked run is neither judgment nor a machinery rerun ───────────────
DEFERRED = {"name": "reviewer / review deferred", "conclusion": "failure",
            "steps": [{"name": "Parked by the PR queue (not a failure of this PR)", "conclusion": "failure"}]}
VERDICT = {"name": "reviewer / verdict", "conclusion": "failure",
           "steps": [{"name": "Run re-actors/alls-green@b5b5b37504aa4183270bd3d855c52a67f212be35", "conclusion": "failure"}]}


def test_review_deferred_is_parked_and_its_aggregate_a_consequence_must_fire() -> None:
    assert redclass.job_class(DEFERRED)[0] == redclass.PARKED
    classes = [(j["name"], *redclass.job_class(j)) for j in (DEFERRED, VERDICT)]
    assert redclass.run_class(classes)[0] == redclass.PARKED


def test_a_failing_step_named_like_it_elsewhere_is_still_judgment_must_not_fire() -> None:
    assert redclass.job_class({**DEFERRED, "name": "unit tests"})[0] == redclass.JUDGMENT
    lint = ("lint", redclass.JUDGMENT, "step failed: ruff")
    assert redclass.run_class([lint, ("reviewer / review deferred", *redclass.job_class(DEFERRED))])[0] == redclass.JUDGMENT


def test_pr_heal_gives_an_unlabelled_parked_pr_back_to_the_scheduler() -> None:
    v = pr_heal.Verdict(3080, "a" * 40, [pr_heal.Red(["review gate"], 9, 1, redclass.PARKED, "parked", "u")])
    assert v.kind == redclass.PARKED
    assert pr_heal.decide(v, [], behind=False, limit=2)[0] == "park"
    assert pr_heal.decide(v, [], behind=True, limit=2)[0] == "update"
    assert pr_heal.machinery({3080: (v, [], {})}) == frozenset({3080})


def test_pr_heal_never_assesses_a_labelled_parked_pr() -> None:
    p = {**_pr(3080, labels=(pr_heal.PARKED,)),
         "statusCheckRollup": [{"name": "review gate", "status": "COMPLETED", "conclusion": "FAILURE"}]}
    out, errors = pr_heal.assess_armed("o/r", [p], ("review gate",), 2, None, None, [])
    assert out == {} and errors == 0


# ── the actor, on a fake GitHub ───────────────────────────────────────────────────────────────
class Gh:
    """Routes `gh api <path>` to canned JSON; records every write."""

    def __init__(self, routes: dict[str, object], fail: tuple[str, ...] = ()) -> None:
        self.routes, self.fail, self.writes = routes, fail, []

    def read(self, *args: str) -> subprocess.CompletedProcess:
        path = next(a for a in args if a.startswith("repos/"))
        for key, val in self.routes.items():
            if path.startswith(key):
                return subprocess.CompletedProcess(args, 0, json.dumps(val), "")
        return subprocess.CompletedProcess(args, 0, json.dumps({"workflow_runs": [], "jobs": []}), "")

    def write(self, *args: str, **_: object) -> subprocess.CompletedProcess:
        self.writes.append(" ".join(args))
        bad = any(f in " ".join(args) for f in self.fail)
        return subprocess.CompletedProcess(args, 1 if bad else 0, "", "HTTP 403: Resource not accessible" if bad else "")


def _iso(t: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(t, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(rid: int, pr: int, *, status: str = "in_progress", conclusion: str | None = None, wf: int = 7) -> dict:
    return {"id": rid, "event": "pull_request", "pull_requests": [{"number": pr}], "head_branch": f"b{pr}",
            "run_attempt": 1, "status": status, "conclusion": conclusion, "workflow_id": wf,
            "created_at": _iso(NOW - 100), "head_sha": f"{pr:040d}"}


def _rjob(name: str, status: str, *, started: float | None = None, labels: tuple[str, ...] = (SEALED,),
          conclusion: str | None = None) -> dict:
    return {"name": name, "status": status, "labels": list(labels), "created_at": _iso(NOW - 120),
            "started_at": _iso(started) if started else None, "conclusion": conclusion}


@pytest.fixture
def labels(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, bool]]:
    got: list[tuple[int, bool]] = []

    def fake(repo: str, n: int, write, add: bool) -> bool:
        got.append((n, add))
        return n != 9666
    monkeypatch.setattr(pr_sched, "_label", fake)
    return got


def _pass(gh: Gh, prs: list[dict], **kw) -> tuple[int, list[str]]:
    summary: list[str] = []
    errors = pr_sched.pass_("o/r", prs, {SEALED: 1}, gh.read, gh.write, summary, preempt_after_s=180, stall_s=900,
                            behind=lambda p: p.get("mergeStateStatus") == "BEHIND", now=NOW, **kw)
    return errors, summary


def _pools(holder: int, holder_started: float) -> dict[str, object]:
    return {"repos/o/r/actions/runs?status=queued": {"workflow_runs": [_run(1, 3073, status="queued")]},
            "repos/o/r/actions/runs?status=in_progress": {"workflow_runs": [_run(2, holder)]},
            "repos/o/r/actions/runs/1/jobs": {"jobs": [_rjob("reviewer / model review", "queued")]},
            "repos/o/r/actions/runs/2/jobs": {"jobs": [_rjob("reviewer / model review", "in_progress",
                                                             started=holder_started)]}}


def test_the_pass_parks_a_young_holder_labelling_before_cancelling_must_fire(labels) -> None:
    gh = Gh(_pools(3080, NOW - 60))
    errors, summary = _pass(gh, [_pr(3073), _pr(3080)])
    assert labels == [(3080, True)]
    assert [w for w in gh.writes if not w.startswith("label create")] == ["api -X POST repos/o/r/actions/runs/2/cancel"]
    assert errors == 0 and any("PARKED run 2" in s for s in summary) and any("head #3073" in s for s in summary)


def test_an_old_holder_is_left_to_finish_must_not_fire(labels) -> None:
    gh = Gh(_pools(3080, NOW - 600))
    errors, _ = _pass(gh, [_pr(3073), _pr(3080)])
    assert errors == 0 and labels == [] and not any("cancel" in w for w in gh.writes)


def test_no_label_no_cancel_so_nothing_is_stranded(labels) -> None:
    gh = Gh(_pools(9666, NOW - 60))
    errors, _ = _pass(gh, [_pr(3073), _pr(9666)])
    assert errors == 1 and not any("cancel" in w for w in gh.writes)


def test_a_refused_cancel_is_an_error_naming_the_grant(labels) -> None:
    gh = Gh(_pools(3080, NOW - 60), fail=("cancel",))
    errors, summary = _pass(gh, [_pr(3073), _pr(3080)])
    assert errors == 1 and any("Actions: Read and write" in s for s in summary)


def _parked_routes(pr: int, *, deferred: bool) -> dict[str, object]:
    run = _run(50 + pr, pr, status="completed", conclusion="failure" if deferred else "cancelled")
    return {f"repos/o/r/actions/runs?head_sha={pr:040d}": {"workflow_runs": [run]},
            f"repos/o/r/actions/runs/{50 + pr}/jobs": {"jobs": [
                _rjob("reviewer / review deferred", "completed", conclusion="failure", labels=())]}}


def test_the_parked_head_is_rerun_at_once_a_deferred_review_whole(labels) -> None:
    gh = Gh(_parked_routes(3073, deferred=True))
    errors, summary = _pass(gh, [_pr(3073, labels=(pr_sched.PARKED,), state="BLOCKED")])
    assert errors == 0
    assert "api -X POST repos/o/r/actions/runs/3123/rerun" in gh.writes
    assert labels == [(3073, False)] and any("RE-RUN (queue head)" in s for s in summary)


def test_a_cancelled_run_reruns_only_its_failed_jobs(labels) -> None:
    gh = Gh(_parked_routes(3073, deferred=False))
    _pass(gh, [_pr(3073, labels=(pr_sched.PARKED,))])
    assert "api -X POST repos/o/r/actions/runs/3123/rerun-failed-jobs" in gh.writes


def test_non_heads_refill_one_per_pass_in_landing_order(labels) -> None:
    routes = {**_parked_routes(3080, deferred=True), **_parked_routes(3081, deferred=True)}
    gh = Gh(routes)
    _pass(gh, [_pr(3073), _pr(3081, labels=(pr_sched.PARKED,)), _pr(3080, labels=(pr_sched.PARKED,))])
    reruns = [w for w in gh.writes if w.endswith("/rerun")]
    assert reruns == ["api -X POST repos/o/r/actions/runs/3130/rerun"]


def test_a_non_head_waits_while_the_head_needs_the_pool_must_not_fire(labels) -> None:
    routes = {**_pools(0, NOW - 600), **_parked_routes(3080, deferred=True)}
    gh = Gh(routes)
    _, summary = _pass(gh, [_pr(3073), _pr(3080, labels=(pr_sched.PARKED,))])
    assert not any(w.endswith("/rerun") for w in gh.writes)
    assert any("waits for a free pool" in s for s in summary)


def test_a_parked_head_behind_main_is_left_to_the_update(labels) -> None:
    gh = Gh(_parked_routes(3073, deferred=True))
    _, summary = _pass(gh, [_pr(3073, labels=(pr_sched.PARKED,), state="BEHIND")])
    assert not any("rerun" in w for w in gh.writes) and any("queue's update" in s for s in summary)


def test_an_updated_pr_is_not_rerun_its_push_is_the_fresh_run(labels) -> None:
    gh = Gh(_parked_routes(3073, deferred=True))
    _pass(gh, [_pr(3073, labels=(pr_sched.PARKED,))], updated=frozenset({3073}))
    assert not any("rerun" in w for w in gh.writes)


def test_a_stale_label_is_removed(labels) -> None:
    gh = Gh({})   # nothing parked, nothing active at its head (a new push replaced the parked run)
    _pass(gh, [_pr(3080, labels=(pr_sched.PARKED,))])
    assert labels == [(3080, False)]


def test_the_label_stays_while_a_run_at_its_head_is_active_must_not_fire(labels) -> None:
    # that run may be the admission about to end parked: dropping the label now would strand it
    run = _run(77, 3080, status="in_progress")
    gh = Gh({f"repos/o/r/actions/runs?head_sha={3080:040d}": {"workflow_runs": [run]}})
    _pass(gh, [_pr(3080, labels=(pr_sched.PARKED,))])
    assert labels == []


def test_an_updated_pr_loses_the_label_at_once(labels) -> None:
    gh = Gh(_parked_routes(3080, deferred=True))
    _pass(gh, [_pr(3073), _pr(3080, labels=(pr_sched.PARKED,))], updated=frozenset({3080}))
    assert labels == [(3080, False)] and not any("rerun" in w for w in gh.writes)


def test_a_non_head_parked_too_long_turns_the_run_red(labels) -> None:
    routes = {**_pools(0, NOW - 600), **_parked_routes(3080, deferred=True)}
    routes[f"repos/o/r/actions/runs?head_sha={3080:040d}"]["workflow_runs"][0]["updated_at"] = _iso(NOW - 3600)
    errors, summary = _pass(Gh(routes), [_pr(3073), _pr(3080, labels=(pr_sched.PARKED,))])
    assert errors == 1 and any(s.startswith("PARKED STARVED") for s in summary)
    routes[f"repos/o/r/actions/runs?head_sha={3080:040d}"]["workflow_runs"][0]["updated_at"] = _iso(NOW - 600)
    assert _pass(Gh(routes), [_pr(3073), _pr(3080, labels=(pr_sched.PARKED,))])[0] == 0


def test_only_the_next_parked_pr_can_starve_the_rest_wait_their_turn_must_not_fire(labels) -> None:
    routes = {**_pools(0, NOW - 600), **_parked_routes(3080, deferred=True), **_parked_routes(3081, deferred=True)}
    routes[f"repos/o/r/actions/runs?head_sha={3081:040d}"]["workflow_runs"][0]["updated_at"] = _iso(NOW - 3600)
    errors, summary = _pass(Gh(routes), [_pr(3073), _pr(3080, labels=(pr_sched.PARKED,)), _pr(3081, labels=(pr_sched.PARKED,))])
    assert errors == 0 and not any(s.startswith("PARKED STARVED") for s in summary)


def test_a_forks_run_is_never_attributed_to_our_branch_of_the_same_name() -> None:
    run = {**_run(5, 3080), "pull_requests": [], "head_repository": {"full_name": "someone/r"}}
    assert pr_sched.pr_of(run, {"b3080": 3080}, "o/r") == 0
    assert pr_sched.pr_of({**run, "head_repository": {"full_name": "o/r"}}, {"b3080": 3080}, "o/r") == 3080


def test_a_train_job_is_never_preempted(labels) -> None:
    train = {**_pr(3090, armed=False), "headRefName": "train/1"}
    routes = _pools(3090, NOW - 30)
    routes["repos/o/r/actions/runs?status=in_progress"]["workflow_runs"][0]["head_branch"] = "train/1"
    gh = Gh(routes)
    _pass(gh, [_pr(3073), train])
    assert not any("cancel" in w for w in gh.writes) and labels == []


# ── out-of-queue merges ───────────────────────────────────────────────────────────────────────
def _oq(merged: list[int], *, behind_by: int = 0, head: int = 3073, ref: str = "b", login: str = "agent") -> str:
    gh = Gh({"repos/o/r/commits/sha1/pulls": [{"number": n, "merged_at": "x", "head": {"ref": f"{ref}{n}"},
                                                "user": {"login": login}} for n in merged],
             "repos/o/r/compare/": {"behind_by": behind_by}})
    return pr_sched.out_of_queue("o/r", [_pr(head), _pr(3090)], "b" * 40, "sha1", gh.read)


def test_a_later_pr_merged_past_an_in_flight_head_pages_must_fire() -> None:
    assert "#3080" in _oq([3080])


def test_renovates_own_merge_pages_whatever_its_number_td3072_must_fire() -> None:
    # td#3072 (Renovate, platformAutomerge off: never armed) landed past head #3073, 03:53Z 11-14
    assert "#3072" in _oq([3072], ref="renovate/x-")
    assert "#3072" in _oq([3072], login="renovate[bot]")


def test_the_heads_own_landing_a_train_or_a_stale_head_does_not_page_must_not_fire() -> None:
    assert _oq([3072]) == ""             # an armed lower PR was the head itself (closed now)
    assert _oq([3080], ref="train/") == ""    # a train landing IS the queue
    assert _oq([3080], behind_by=3) == ""   # the head was not in flight against the old main



def test_a_skipped_red_head_hands_the_watch_to_the_pr_that_can_land_must_fire() -> None:
    # #3073 red (skipped by the hold): #3080 is the head in flight, and #3085 landing past it pages
    gh = Gh({"repos/o/r/commits/sha1/pulls": [{"number": 3085, "merged_at": "x", "head": {"ref": "b3085"},
                                                "user": {"login": "agent"}}],
             "repos/o/r/compare/": {"behind_by": 0}})
    prs = [_pr(3073), _pr(3080), _pr(3085)]
    assert "#3085" in pr_sched.out_of_queue("o/r", prs, "b" * 40, "sha1", gh.read, frozenset({3073}))
    assert pr_queue.head(prs, frozenset({3073}))["number"] == 3080


def test_a_head_red_only_by_cancelled_runs_was_never_in_flight_must_not_fire() -> None:
    # 2nd review, finding 1 (repeat): parked #3079 (cancelled runs) skipped while current; #3084 held
    # success and merged; the push pass sees #3079 BEHIND and the head -- the hold allowed that merge
    gh = Gh({"repos/o/r/commits/sha1/pulls": [{"number": 3084, "merged_at": "x", "head": {"ref": "b3084"},
                                                "user": {"login": "agent"}}],
             "repos/o/r/compare/": {"behind_by": 0}})
    cancelled = [{"__typename": "CheckRun", "name": "model review", "status": "COMPLETED", "conclusion": "CANCELLED"}]
    prs = [_pr(3079, statusCheckRollup=cancelled, labels=(pr_sched.PARKED,))]
    assert pr_sched.out_of_queue("o/r", prs, "b" * 40, "sha1", gh.read) == ""

# ── the review gate's admission CLI ───────────────────────────────────────────────────────────
def _admit(monkeypatch: pytest.MonkeyPatch, tmp_path, prs: list[dict], *, edit_rc: int = 0, demand: bool = True,
           argv: tuple[str, ...] = (), label: str | None = "PR queue: parked") -> dict[str, str]:
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))

    def run(cmd, **_):
        if cmd[:3] == ["gh", "pr", "list"]:  # no rollup: the review gate's token cannot read check runs
            assert "statusCheckRollup" not in " ".join(cmd)
            rows = [{**p, "baseRefName": p.get("baseRefName", "main")} for p in prs]
            return subprocess.CompletedProcess(cmd, 0, json.dumps(rows), "")
        if cmd[:3] == ["gh", "api", "repos/o/r"]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"default_branch": "main"}), "")
        if cmd[:3] == ["gh", "pr", "edit"]:
            return subprocess.CompletedProcess(cmd, edit_rc, "", "no such label")
        if cmd[:3] == ["gh", "label", "list"]:
            got = [] if label is None else [{"name": pr_sched.PARKED, "description": label}]
            return subprocess.CompletedProcess(cmd, 0, json.dumps(got), "")
        raise subprocess.CalledProcessError(1, cmd)
    monkeypatch.setattr(pr_admit.subprocess, "run", run)
    monkeypatch.setattr(pr_admit, "head_demand", lambda *a: demand)
    pr_admit.admit_main(["--repo", "o/r", "--pr", "3080", "--pool", SEALED, *argv])
    return dict(line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines())


def test_admission_parks_a_later_pr_and_labels_it_must_fire(monkeypatch, tmp_path) -> None:
    got = _admit(monkeypatch, tmp_path, [_pr(3073), _pr(3080)])
    assert got["defer"] == "true" and got["head"] == "3073"


def test_admission_skips_a_conflicting_or_stacked_lowest_pr_like_the_hold_must_fire(monkeypatch, tmp_path) -> None:
    for low in (_pr(3070, state="DIRTY"), _pr(3070, state="UNKNOWN", mergeable="CONFLICTING"), _pr(3070, baseRefName="b1")):
        got = _admit(monkeypatch, tmp_path, [low, _pr(3073), _pr(3080)])
        assert got["head"] == "3073"


def test_admission_runs_when_it_cannot_label_or_cannot_look_or_was_parked_enough_must_not_fire(monkeypatch, tmp_path) -> None:
    assert _admit(monkeypatch, tmp_path, [_pr(3073), _pr(3080)], edit_rc=1)["defer"] == "false"
    assert _admit(monkeypatch, tmp_path, [_pr(3073), _pr(3080)], argv=("--attempt", "4"))["defer"] == "false"
    assert _admit(monkeypatch, tmp_path, [_pr(3073), _pr(3080)], demand=False)["defer"] == "false"


def test_admission_parks_nothing_while_the_scheduler_is_off_must_not_fire(monkeypatch, tmp_path) -> None:
    # a label made while the push token could re-run, then the grant was lost (a rotated PAT)
    off = _admit(monkeypatch, tmp_path, [_pr(3073), _pr(3080)], label=pr_sched.PARKED_OFF)
    assert off["defer"] == "false" and "off" in off["why"]
    assert _admit(monkeypatch, tmp_path, [_pr(3073), _pr(3080)], label=None)["defer"] == "false"

    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, "gh")
    monkeypatch.setattr(pr_admit.subprocess, "run", boom)
    pr_admit.admit_main(["--repo", "o/r", "--pr", "3080", "--pool", SEALED])
    assert (tmp_path / "out").read_text(encoding="utf-8").splitlines()[-3] == "defer=false"


def test_capacity_parses_or_refuses() -> None:
    assert pr_sched.capacity_of("") == {}
    assert pr_sched.capacity_of('{"home-sealed": 1, "home-heavy": 1}') == {"home-sealed": 1, "home-heavy": 1}
    for bad in ('["home-sealed"]', '{"home-sealed": 0}', '{"home-sealed": "1"}', "nope"):
        with pytest.raises(ValueError):
            pr_sched.capacity_of(bad)


# ── pr_autoupdate: the head is updated the moment its verdict is recorded ─────────────────────
def _head_first(monkeypatch: pytest.MonkeyPatch, head: dict, *, settled: bool = True, scarce: bool = True,
                why: str = "behind, checks still running (an update would cancel them)") -> tuple[int | None, pr_queue.Plan]:
    import ci_kit.pr_autoupdate as upd
    monkeypatch.setattr(pr_sched, "review_settled", lambda *a: settled)
    other = _pr(3080, state="BEHIND")
    q = pr_queue.Plan(update=[other], held=[(head, why)])
    opts = upd.Options(scarce=((SEALED, 1),) if scarce else ())
    return upd._head_first("o/r", [head, other], q, opts, []), q


def test_a_behind_head_with_its_verdict_recorded_is_updated_first_must_fire(monkeypatch) -> None:
    n, q = _head_first(monkeypatch, _pr(3073, state="BEHIND"))
    assert n == 3073 and q.update[0]["number"] == 3073 and q.held == []


def test_the_head_waits_for_a_verdict_its_own_red_or_the_switch_must_not_fire(monkeypatch) -> None:
    assert _head_first(monkeypatch, _pr(3073, state="BEHIND"), settled=False)[0] is None
    assert _head_first(monkeypatch, _pr(3073, state="BEHIND"), scarce=False)[0] is None
    assert _head_first(monkeypatch, _pr(3073, state="BEHIND"),
                       why="behind, a check is failing on its head (the author's fix re-queues it)")[0] is None
    assert _head_first(monkeypatch, _pr(3073, state="BLOCKED"))[0] is None   # current: nothing to update


def test_replay_td3078_head_behind_with_unit_running_on_the_stale_base_is_updated_now_must_fire(monkeypatch) -> None:
    # td#3078, 05:26Z 11-14: queue head, review verdict RECORDED, BEHIND main (its author pushed a
    # fix on a base missing #3077) while `unit tests` ran on that stale head -- a result worthless for
    # merging. The serial queue held it ("checks still running") until unit finished, then updated,
    # then ran unit again; the coordinator queue-jumped it by hand. The head goes first, NOW.
    import ci_kit.pr_autoupdate as upd
    rollup = [{"name": "unit tests", "status": "IN_PROGRESS", "conclusion": None},
              {"name": "review gate", "status": "COMPLETED", "conclusion": "SUCCESS"}]
    head = {**_pr(3078, state="BEHIND"), "statusCheckRollup": rollup}
    later = {**_pr(3080, state="BEHIND"), "statusCheckRollup": [{"name": "unit tests", "status": "COMPLETED",
                                                                   "conclusion": "SUCCESS"}]}
    q = pr_queue.plan([head, later], 1, ("unit tests", "review gate"))
    assert any(p is head and "running" in why for p, why in q.held)   # the old rule: wait for unit
    review_run = {**_run(9, 3078, status="in_progress"), "head_branch": "b3078"}
    gh = Gh({"repos/o/r/actions/runs?status=queued": {"workflow_runs": []},
             "repos/o/r/actions/runs?status=in_progress": {"workflow_runs": [review_run]},
             "repos/o/r/actions/runs/9/jobs": {"jobs": [_rjob("reviewer / review", "completed", labels=()),
                                                         _rjob("reviewer / record verdict", "completed", labels=())]}})
    monkeypatch.setattr(upd, "_gh_read", gh.read)
    summary: list[str] = []
    n = upd._head_first("o/r", [head, later], q, upd.Options(scarce=((SEALED, 1),)), summary)
    assert n == 3078 and q.update[0] is head and not any(p is head for p, _ in q.held)
    assert any("updated first" in s for s in summary)


def test_replay_td3078_but_the_verdict_still_owed_waits_must_not_fire(monkeypatch) -> None:
    import ci_kit.pr_autoupdate as upd
    head = {**_pr(3078, state="BEHIND"), "statusCheckRollup": [{"name": "review gate", "status": "IN_PROGRESS"}]}
    q = pr_queue.plan([head], 1, ("review gate",))
    review_run = {**_run(9, 3078, status="in_progress"), "head_branch": "b3078"}
    gh = Gh({"repos/o/r/actions/runs?status=in_progress": {"workflow_runs": [review_run]},
             "repos/o/r/actions/runs/9/jobs": {"jobs": [_rjob("reviewer / review", "completed", labels=()),
                                                         _rjob("reviewer / model review", "in_progress")]}})
    monkeypatch.setattr(upd, "_gh_read", gh.read)
    assert upd._head_first("o/r", [head], q, upd.Options(scarce=((SEALED, 1),)), []) is None


def test_a_token_without_actions_write_parks_nothing_and_creates_no_label_must_fire(labels) -> None:
    # PR_UPDATE_TOKEN's documented grant is Actions: READ; a park it cannot re-run strands the PR
    routes = {**_pools(3080, NOW - 30), "repos/o/r/actions/runs?status=completed": {"workflow_runs": [{"id": 5}]}}
    gh = Gh(routes, fail=("runs/5/cancel",))
    errors, summary = _pass(gh, [_pr(3073), _pr(3080)])
    assert errors >= 1 and any(s.startswith("SCHEDULER OFF") for s in summary)
    assert not any(w.startswith("label create") for w in gh.writes)       # admission cannot add it
    assert [w for w in gh.writes if "cancel" in w] == ["api -X POST repos/o/r/actions/runs/5/cancel"]
    assert labels == []
    assert f"label edit {pr_sched.PARKED} -R o/r --description {pr_sched.PARKED_OFF}" in gh.writes


def test_the_probe_with_the_grant_is_a_no_op_409_and_the_pass_parks_must_not_fire(labels) -> None:
    routes = {**_pools(3080, NOW - 30), "repos/o/r/actions/runs?status=completed": {"workflow_runs": [{"id": 5}]}}
    gh = Gh(routes)
    gh.write = _conflict_on(gh.write, "runs/5/cancel")
    errors, summary = _pass(gh, [_pr(3073), _pr(3080)])
    assert errors == 0 and not any(s.startswith("SCHEDULER OFF") for s in summary)
    assert "api -X POST repos/o/r/actions/runs/2/cancel" in gh.writes and labels == [(3080, True)]


def _conflict_on(write, path: str):
    def w(*args: str, **kw: object) -> subprocess.CompletedProcess:
        got = write(*args, **kw)
        if path in " ".join(args):
            return subprocess.CompletedProcess(args, 1, "", "HTTP 409: Cannot cancel a workflow run that is completed.")
        return got
    return w
