"""ci-queue-wait must fire: over the limit -> 1, within -> 0, cannot measure -> 2."""
from __future__ import annotations

import pytest

import ci_kit.queue_wait as qw

RUN = {"id": 1, "created_at": "2026-10-24T00:00:00Z", "status": "completed"}
T0 = RUN["created_at"]


def _at(s: float) -> str:
    return f"2026-10-24T{int(s) // 3600:02d}:{int(s) % 3600 // 60:02d}:{int(s) % 60:02d}Z"


def _jobs(*starts: str, skipped: str = "") -> list[dict]:
    js = [{"started_at": s, "conclusion": "success", "runner_name": "home-1", "created_at": T0} for s in starts]
    return js + ([{"started_at": skipped, "conclusion": "skipped"}] if skipped else [])


def _ghost(queued_s: float) -> dict:
    """A job cancelled while QUEUED: GitHub stamps started_at = created_at, no runner."""
    return {"started_at": T0, "created_at": T0, "completed_at": _at(queued_s),
            "conclusion": "cancelled", "runner_name": ""}


def _page(path: str) -> int:
    return int(path.rsplit("&page=", 1)[1])


def _fake(jobs_for: dict[int, list[dict]], fail: bool = False):
    """runs 0..n-1 on page 1 only; each run's jobs from `jobs_for`."""
    def api(path: str) -> dict:
        if fail:
            raise RuntimeError("HTTP 401")
        if path.endswith("/jobs?per_page=100"):
            return {"jobs": jobs_for[int(path.split("/runs/")[1].split("/")[0])]}
        n = len(jobs_for) if _page(path) == 1 else 0
        return {"workflow_runs": [{**RUN, "id": i} for i in range(n)]}
    return api


def _waits(waits: list[float]) -> dict[int, list[dict]]:
    return {i: _jobs(_at(w)) for i, w in enumerate(waits)}


def _main(*extra: str) -> int:
    return qw.main(["--repo", "o/r", "--workflow", "ci.yml", *extra])


def test_wait_is_the_first_real_job_start() -> None:
    assert qw.wait_s(RUN, _jobs(_at(240), _at(90))) == (90, False)
    # a skipped job's (instant) start is not a runner picking the run up
    assert qw.wait_s(RUN, _jobs(_at(120), skipped=_at(1))) == (120, False)
    assert qw.wait_s(RUN, []) is None


def test_rerun_waits_from_the_attempt_not_the_first_creation() -> None:
    rerun = {**RUN, "run_started_at": _at(7200)}
    assert qw.wait_s(rerun, _jobs(_at(7230))) == (30, False)


@pytest.mark.parametrize(("waits", "limit", "rc"), [
    ([10, 20, 30, 40, 50, 60, 70, 80, 90, 100], 300, 0),
    ([10, 20, 30, 40, 50, 60, 70, 80, 400, 500], 300, 1),
])
def test_p90_gate(monkeypatch: pytest.MonkeyPatch, waits, limit, rc) -> None:
    monkeypatch.setattr(qw, "_api", _fake(_waits(waits)))
    assert _main("--max-p90", str(limit)) == rc


def test_blind_is_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qw, "_api", _fake({}, fail=True))
    assert _main("--max-p90", "300") == 2
    monkeypatch.setattr(qw, "_api", _fake({}))   # nothing to measure is not "within the limit"
    assert _main("--max-p90", "300") == 2
    assert _main("--max-p90", "300", "--min-runs", "0") == 2   # not a max() traceback read as rc 1


@pytest.mark.parametrize(("waits", "rc"), [
    ([10] * 96 + [900] * 4, 0),    # p95 = the 96th of 100 sorted: still 10
    ([10] * 90 + [900] * 10, 1),   # the same p90 run would pass a 900s p90 limit; p95 sees the tail
])
def test_p95_gate(monkeypatch: pytest.MonkeyPatch, waits, rc) -> None:
    calls: list[str] = []
    api = _fake(_waits(waits))
    monkeypatch.setattr(qw, "_api", lambda p: calls.append(p) or api(p))
    assert _main("--max-p95", "300") == rc
    assert sum("/jobs?" in c for c in calls) == 100   # 100 runs measured once each, not a page re-read


def test_a_limit_is_required() -> None:
    with pytest.raises(SystemExit):
        qw.main(["--repo", "o/r", "--workflow", "ci.yml"])


def test_runs_are_paged_and_truncation_is_a_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PR-scope window holds hundreds of runs; one page of 100 was all the old reader saw."""
    pages = {1: 100, 2: 100, 3: 40}
    monkeypatch.setattr(qw, "_api", lambda p: {"workflow_runs": [{**RUN, "id": _page(p) * 1000 + i}
                                                          for i in range(pages.get(_page(p), 0))]})
    runs, cut = qw.list_runs("o/r", "ci.yml", "", "", 24, 1000)
    assert (len(runs), cut) == (240, False)
    runs, cut = qw.list_runs("o/r", "ci.yml", "", "", 24, 150)
    assert (len(runs), cut) == (150, True)
    runs, cut = qw.list_runs("o/r", "ci.yml", "", "", 24, 240)   # exactly the window: not truncated
    assert (len(runs), cut) == (240, False)
    runs, cut = qw.list_runs("o/r", "ci.yml", "", "", 24, 200)   # a full page at the cap: one more read decides
    assert (len(runs), cut) == (200, True)


def test_a_jam_of_runs_cancelled_while_queued_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reviewer 580 F1: 9 runs cancelled after 25 min queued + 1 that started in 5s exited 0."""
    jam = {i: [_ghost(1500)] for i in range(9)} | {9: _jobs(_at(5))}
    monkeypatch.setattr(qw, "_api", _fake(jam))
    assert _main("--max-p90", "180", "--branch", "", "--event", "pull_request") == 1
    waits, empty, cut, _ = qw.measure("o/r", "ci.yml", "", "", 24)
    assert (sorted(w for _, w, _ in waits), sum(c for *_, c in waits), empty, cut) == ([5] + [1500] * 9, 9, 0, False)


def test_a_quick_supersede_cancel_is_harmless(monkeypatch: pytest.MonkeyPatch) -> None:
    runs = {i: [_ghost(20)] for i in range(5)} | {i: _jobs(_at(10)) for i in range(5, 10)}
    monkeypatch.setattr(qw, "_api", _fake(runs))
    assert _main("--max-p90", "180") == 0


def test_job_unit_fires_on_the_pool_behind_through_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every run's lint starts in 5s; its unit job waits an hour then is cancelled still queued."""
    runs = {i: [*_jobs(_at(5)), _ghost(3600)] for i in range(10)}
    monkeypatch.setattr(qw, "_api", _fake(runs))
    assert _main("--max-p90", "180") == 0                    # per run: the first job, 5s
    assert _main("--max-p90", "180", "--unit", "job") == 1   # per job: the hour is seen
    waits, _, _, _ = qw.measure("o/r", "ci.yml", "", "", 24, unit="job")
    assert sorted(w for _, w, _ in waits) == [5] * 10 + [3600] * 10


def test_cancelled_while_queued_is_not_a_start() -> None:
    assert qw.wait_s(RUN, [_ghost(25 * 60), *_jobs(_at(660))]) == (660, False)
    assert qw.wait_s(RUN, [_ghost(25 * 60)]) == (1500, True)
    assert qw.job_waits([_ghost(25 * 60), {"conclusion": "skipped", "started_at": T0}]) == [(1500, True)]


def test_partial_rerun_ignores_carried_over_jobs() -> None:
    """Reviewer 581 N1: attempt 2 re-runs only the failed job; the passed jobs come back with
    attempt 1's timestamps, before the attempt started. Scoring them gave a negative wait."""
    rerun = {**RUN, "run_started_at": _at(3600)}
    carried = _jobs(_at(30), _at(40))
    assert qw.wait_s(rerun, [*carried, *_jobs(_at(3700))]) == (100, False)
    assert qw.wait_s(rerun, carried) is None   # nothing this attempt ran: not a 0s or negative wait


def test_the_api_1000_cap_is_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reviewer 581 N2: the filtered runs API serves 1000 results; total_count says there were more."""
    def api(p: str) -> dict:
        # the live API (reviewer 582 F1): total_count 1354 on pages 1-10, then 0 with no runs --
        # a reader that took the LAST page's total_count would call the window complete
        n = 100 if _page(p) <= 10 else 0
        return {"total_count": 1400 if n else 0, "workflow_runs": [{**RUN, "id": _page(p) * 1000 + i} for i in range(n)]}
    monkeypatch.setattr(qw, "_api", api)
    runs, cut = qw.list_runs("o/r", "ci.yml", "", "", 24, 5000)
    assert (len(runs), cut) == (1000, True)


def test_job_unit_sees_the_pool_behind() -> None:
    jobs = _jobs(_at(16), _at(3600))
    assert qw.wait_s(RUN, jobs) == (16, False)
    assert sorted(qw.job_waits(jobs)) == [(16, False), (3600, False)]


def test_a_run_repeated_across_pages_counts_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reviewer 582 F3: a run completing between page reads shifts the list; the boundary run repeats."""
    pages = {1: list(range(100)), 2: [99, *range(100, 140)]}
    monkeypatch.setattr(qw, "_api", lambda p: {"workflow_runs": [{**RUN, "id": i} for i in pages.get(_page(p), [])]})
    runs, _ = qw.list_runs("o/r", "ci.yml", "", "", 24, 1000)
    assert len(runs) == 140 and len({r["id"] for r in runs}) == 140


def test_no_gh_is_did_not_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reviewer 582 F5: gh missing raised OSError, which exited 1 ("over the limit"), not 2."""
    def boom(p: str) -> dict:
        raise FileNotFoundError("gh")
    monkeypatch.setattr(qw, "_api", boom)
    assert qw.main(["--repo", "o/r", "--workflow", "ci.yml", "--max-p95", "60"]) == 2
