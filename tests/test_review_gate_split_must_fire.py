"""The review gate's five-job split (v1.29.0) can neither skip a review nor read a park as a pass.

GitHub counts a SKIPPED job as passed, and the caller's `review gate` aggregates this workflow's
jobs. With the model review in its own job, `decide` green + `model review` skipped would read
as a pass. These predicates pin the wiring that prevents it, each shown to fire on its mutation:

  * `Mode` (executed here in bash) says `review` for every required, not-blind, not-carried,
    not-parked diff, and is the ONLY way the sealed job runs;
  * the in-workflow `verdict` aggregate allows `model review` to be skipped only when mode is
    not `review`, and never allows `decide` to be skipped;
  * a parked review is RED: `review deferred` exits 1 on either admission's park;
  * inside `model review`, once its Admit parked, nothing that reviews, comments or gates runs.
"""
from __future__ import annotations

import copy
import os
import pathlib
import shutil
import subprocess

import pytest
import yaml

WF = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "review-gate.yml"
ALLOWED = "${{ needs.decide.outputs.mode == 'review' && 'defer' || 'review, defer' }}"
NOT_PARKED = "steps.admit.outputs.defer != 'true'"


def _wf() -> dict:
    return yaml.safe_load(WF.read_text(encoding="utf-8"))


def _step(job: dict, sid: str) -> dict:
    return next(s for s in job["steps"] if s.get("id") == sid or s.get("name") == sid)


def faults(wf: dict) -> list[str]:
    out: list[str] = []
    jobs = wf["jobs"]
    if jobs["review"].get("if") != "needs.decide.outputs.mode == 'review'":
        out.append("model review is not keyed on mode == review alone")
    v = jobs["verdict"]
    if not {"decide", "review", "defer"} <= set(v.get("needs") or []):
        out.append("verdict does not aggregate decide, review and defer")
    if "!cancelled()" not in str(v.get("if")):
        out.append("verdict can be skipped")
    green = next((s for s in v["steps"] if "re-actors/alls-green" in str(s.get("uses"))), None)
    if green is None or str(green["with"].get("allowed-skips")) != ALLOWED:
        out.append("verdict allows skips beyond the mode's")
    d = jobs["defer"]
    if "needs.decide.outputs.mode == 'defer'" not in str(d.get("if")) or "needs.review.outputs.parked == 'true'" not in str(d.get("if")):
        out.append("review deferred does not fire on both parks")
    if "exit 1" not in str(d["steps"][-1].get("run")) or d["steps"][-1].get("continue-on-error") or d.get("continue-on-error"):
        out.append("review deferred is not red")
    started = False
    for s in jobs["review"]["steps"]:
        if s.get("id") == "admit":
            started = True
            continue
        name = s.get("name") or s.get("uses")
        if started and name != "Stop the git ssh agent" and NOT_PARKED not in str(s.get("if", "")):
            out.append(f"model review step runs after a park: {name}")
    res = str((wf.get("on") or wf[True])["workflow_call"]["outputs"]["result"]["value"])
    if "jobs.review.outputs.result" not in res or "jobs.decide.outputs.result" not in res:
        out.append("the workflow's result drops one job's")
    return out


def test_the_shipped_split_is_clean() -> None:
    assert faults(_wf()) == []


def _mut(fn) -> list[str]:
    wf = copy.deepcopy(_wf())
    fn(wf["jobs"], wf)
    return faults(wf)


def test_each_fault_fires() -> None:
    assert _mut(lambda j, w: j["review"].update({"if": "needs.decide.outputs.mode != 'skip'"}))
    assert _mut(lambda j, w: j["verdict"].update({"needs": ["decide", "defer"]}))
    assert _mut(lambda j, w: j["verdict"].update({"if": "success()"}))
    assert _mut(lambda j, w: j["verdict"]["steps"][0]["with"].update({"allowed-skips": "review, defer"}))
    assert _mut(lambda j, w: j["defer"].update({"if": "needs.decide.outputs.mode == 'defer'"}))
    assert _mut(lambda j, w: j["defer"]["steps"][-1].update({"run": "echo parked"}))
    assert _mut(lambda j, w: j["defer"].update({"continue-on-error": True}))
    assert _mut(lambda j, w: _step(j["review"], "review").update({"if": "always()"}))
    assert _mut(lambda j, w: _step(j["review"], "Gate").update({"if": "!cancelled()"}))
    assert _mut(lambda j, w: (w.get("on") or w[True])["workflow_call"]["outputs"]["result"].update(
        {"value": "${{ jobs.review.outputs.result }}"}))


def test_a_carry_and_a_blind_and_a_train_are_finished_by_decide() -> None:
    # the jobs that finish a verdict without the sealed runner still comment, gate and push
    d = _wf()["jobs"]["decide"]
    for sid in ("Comment on the PR", "Gate"):
        cond = str(_step(d, sid).get("if"))
        assert "steps.mode.outputs.mode == 'finish'" in cond and "is_train == 'true'" in cond and "!cancelled()" in cond
    assert "exit 1" in _step(d, "Gate")["run"] and "pass)" in _step(d, "Gate")["run"]


def test_the_record_job_records_a_carry_or_a_review_never_a_park() -> None:
    cond = " ".join(str(_wf()["jobs"]["record"]["if"]).split())
    assert "needs.decide.outputs.hash != ''" in cond
    assert "needs.decide.outputs.mode == 'finish' && needs.decide.outputs.result == 'pass'" in cond
    assert "needs.review.outputs.result == 'pass' || needs.review.outputs.result == 'block'" in cond


# ── Mode, executed ────────────────────────────────────────────────────────────────────────────
BASH = shutil.which("bash")


def _mode(**env: str) -> dict[str, str]:
    step = _step(_wf()["jobs"]["decide"], "mode")
    out = pathlib.Path(os.environ.get("TMP", "/tmp")) / f"mode-{os.getpid()}.out"
    out.write_text("", encoding="utf-8")
    full = {**os.environ, "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": os.devnull,
            **{k: "" for k in ("IS_TRAIN", "REQUIRED", "BLIND", "CARRIED", "DEFER")}, **env}
    subprocess.run([BASH, "-e", "-c", step["run"]], env=full, check=True, capture_output=True, text=True)
    got = dict(line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines() if "=" in line)
    out.unlink()
    return got


@pytest.mark.skipif(BASH is None, reason="no bash")
def test_mode_reviews_every_required_diff_it_cannot_finish_must_fire() -> None:
    assert _mode(REQUIRED="true")["mode"] == "review"
    assert _mode(REQUIRED="true", CARRIED="false", DEFER="false")["mode"] == "review"
    got = _mode(REQUIRED="true", DEFER="true")
    assert got["mode"] == "defer" and got["result"] == "parked"


@pytest.mark.skipif(BASH is None, reason="no bash")
def test_mode_finishes_only_a_carry_a_blind_a_train_or_an_out_of_tier_diff_must_not_fire() -> None:
    assert _mode(IS_TRAIN="true", REQUIRED="true")["mode"] == "train"
    assert _mode(REQUIRED="false")["mode"] == "skip"
    assert _mode(REQUIRED="")["mode"] == "skip"
    assert _mode(REQUIRED="true", CARRIED="true", DEFER="true")["mode"] == "finish"   # a carry never parks
    assert _mode(REQUIRED="true", BLIND="no credential")["mode"] == "finish"
