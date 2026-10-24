"""ci-queue-wait must fire: over the limit -> 1, within -> 0, cannot measure -> 2."""
from __future__ import annotations

import pytest

import ci_kit.queue_wait as qw

RUN = {"id": 1, "created_at": "2026-10-24T00:00:00Z", "status": "completed"}


def _jobs(*starts: str, skipped: str = "") -> list[dict]:
    js = [{"started_at": s, "conclusion": "success"} for s in starts]
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
