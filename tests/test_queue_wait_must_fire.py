"""ci-queue-wait must fire: over the limit -> 1, within -> 0, cannot measure -> 2."""
from __future__ import annotations

import pytest

import ci_kit.queue_wait as qw

RUN = {"id": 1, "created_at": "2026-10-24T00:00:00Z", "status": "completed"}


def _jobs(*starts: str, skipped: str = "") -> list[dict]:
    js = [{"started_at": s, "conclusion": "success", "runner_name": "home-1",
           "created_at": RUN["created_at"]} for s in starts]
    return js + ([{"started_at": skipped, "conclusion": "skipped"}] if skipped else [])


def test_wait_is_the_first_real_job_start() -> None:
    assert qw.wait_s(RUN, _jobs("2026-10-24T00:04:00Z", "2026-10-24T00:01:30Z")) == 90
    # a skipped job's (instant) start is not a runner picking the run up
    assert qw.wait_s(RUN, _jobs("2026-10-24T00:02:00Z", skipped="2026-10-24T00:00:01Z")) == 120
    assert qw.wait_s(RUN, []) is None


def _fake(waits: list[float], fail: bool = False):
    def api(path: str) -> dict:
        if fail:
            raise RuntimeError("HTTP 401")
        if path.endswith("/jobs?per_page=100"):
            rid = int(path.split("/runs/")[1].split("/")[0])
            return {"jobs": _jobs(f"2026-10-24T00:{int(waits[rid]) // 60:02d}:{int(waits[rid]) % 60:02d}Z")}
        if "page=1" not in path:
            return {"workflow_runs": []}
        return {"workflow_runs": [{**RUN, "id": i} for i in range(len(waits))]}
    return api


@pytest.mark.parametrize(("waits", "limit", "rc"), [
    ([10, 20, 30, 40, 50, 60, 70, 80, 90, 100], 300, 0),
    ([10, 20, 30, 40, 50, 60, 70, 80, 400, 500], 300, 1),
])
def test_p90_gate(monkeypatch: pytest.MonkeyPatch, waits, limit, rc) -> None:
    monkeypatch.setattr(qw, "_api", _fake(waits))
    assert qw.main(["--repo", "o/r", "--workflow", "ci.yml", "--max-p90", str(limit)]) == rc


def test_blind_is_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qw, "_api", _fake([], fail=True))
    assert qw.main(["--repo", "o/r", "--workflow", "ci.yml", "--max-p90", "300"]) == 2
    monkeypatch.setattr(qw, "_api", _fake([]))   # nothing to measure is not "within the limit"
    assert qw.main(["--repo", "o/r", "--workflow", "ci.yml", "--max-p90", "300"]) == 2


@pytest.mark.parametrize(("waits", "rc"), [
    ([10] * 96 + [900] * 4, 0),    # p95 = the 96th of 100 sorted: still 10
    ([10] * 90 + [900] * 10, 1),   # the same p90 run would pass a 900s p90 limit; p95 sees the tail
])
def test_p95_gate(monkeypatch: pytest.MonkeyPatch, waits, rc) -> None:
    monkeypatch.setattr(qw, "_api", _fake(waits))
    assert qw.main(["--repo", "o/r", "--workflow", "ci.yml", "--max-p95", "300"]) == rc


def test_a_limit_is_required() -> None:
    with pytest.raises(SystemExit):
        qw.main(["--repo", "o/r", "--workflow", "ci.yml"])


def test_runs_are_paged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PR-scope window holds hundreds of runs; one page of 100 was all the old reader saw."""
    pages = {1: 100, 2: 100, 3: 40}

    def api(path: str) -> dict:
        n = pages.get(int(path.rsplit("page=", 1)[1]), 0)
        return {"workflow_runs": [{**RUN, "id": i} for i in range(n)]}
    monkeypatch.setattr(qw, "_api", api)
    assert len(qw.list_runs("o/r", "ci.yml", "", "", 24, 1000)) == 240
    assert len(qw.list_runs("o/r", "ci.yml", "", "", 24, 150)) == 150


def test_never_started_is_counted_not_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    def api(path: str) -> dict:
        if path.endswith("/jobs?per_page=100"):
            rid = int(path.split("/runs/")[1].split("/")[0])
            return {"jobs": [] if rid else _jobs("2026-10-24T00:10:00Z")}
        return {"workflow_runs": [{**RUN, "id": i} for i in range(3)] if "page=1" in path else []}
    monkeypatch.setattr(qw, "_api", api)
    assert qw.measure("o/r", "ci.yml", "", "", 24) == ([(0, 600.0)], 2)


def test_cancelled_while_queued_is_not_a_start() -> None:
    """GitHub stamps started_at = created_at on a job cancelled before any runner took it."""
    ghost = {"started_at": RUN["created_at"], "conclusion": "cancelled", "runner_name": ""}
    assert qw.wait_s(RUN, [ghost, *_jobs("2026-10-24T00:11:00Z")]) == 660
    assert qw.wait_s(RUN, [ghost]) is None


def test_job_unit_sees_the_pool_behind() -> None:
    jobs = _jobs("2026-10-24T00:00:16Z", "2026-10-24T01:00:00Z")
    assert qw.wait_s(RUN, jobs) == 16
    assert sorted(qw.job_waits(jobs)) == [16, 3600]
