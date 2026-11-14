"""The review gate's job split (v1.29.0; three jobs since v1.35.0) can neither skip a review nor read a park as a pass.

GitHub counts a SKIPPED job as passed, and the caller's `review gate` aggregates this workflow's
jobs. With the model review in its own job, `decide` green + `model review` skipped would read
as a pass. These predicates pin the wiring that prevents it, each shown to fire on its mutation:

  * `Mode` (executed here in bash) says `review` for every required, not-blind, not-carried,
    not-parked diff, and is the ONLY way the sealed job runs;
  * the in-workflow `verdict` aggregate allows `model review` to be skipped only when mode is
    not `review`, and never allows `decide` to be skipped;
  * a parked review is RED: `review deferred` (a STEP of `verdict` since v1.35.0, `always()` so a red
    above cannot skip it) exits 1 on either admission's park;
  * `record verdict` (a STEP of `verdict` since v1.35.0) never runs where PR code ran: `verdict`
    checks nothing out; it runs `always()` and can never turn the aggregate red;
  * inside `model review`, once its Admit parked, nothing that reviews, comments or gates runs.
"""
from __future__ import annotations

import copy
import os
import pathlib
import shutil
import subprocess
import tempfile

import pytest
import yaml

WF = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "review-gate.yml"
ALLOWED = "${{ needs.decide.outputs.mode != 'review' && 'review' || '' }}"
NOT_PARKED = "steps.admit.outputs.defer != 'true'"
SKIP_IF = "${{ !cancelled() && !(needs.decide.result == 'success' && needs.decide.outputs.finished == 'true') }}"
FINISH_NEEDS = ("steps.mode.outputs.mode == 'finish'", "steps.carry.outputs.carried == 'true'",
                "steps.verdict.outputs.result == 'pass'")


def _wf() -> dict:
    return yaml.safe_load(WF.read_text(encoding="utf-8"))


def _step(job: dict, sid: str) -> dict:
    return next(s for s in job["steps"] if s.get("id") == sid or s.get("name") == sid)


def _carry_skip_faults(jobs: dict) -> list[str]:
    """v1.35.0: `verdict` is skipped ONLY on a carried pass `decide` finished after a green Gate."""
    out: list[str] = []
    if " ".join(str(jobs["verdict"].get("if")).split()) != SKIP_IF:
        out.append("verdict is skipped on something other than a finished carry")
    dec = jobs["decide"]
    ids = [s.get("id") or s.get("name") for s in dec["steps"]]
    fin = next((s for s in dec["steps"] if s.get("id") == "finish"), {})
    fif = " ".join(str(fin.get("if")).split())
    if not fif.startswith("success() ") or "always()" in fif or "cancelled()" in fif \
            or any(c not in fif for c in FINISH_NEEDS) or "Gate" not in ids or ids.index("Gate") > ids.index("finish"):
        out.append("decide finishes something other than a carried pass after a green Gate")
    if "steps.finish.outputs.done == 'true' && steps.upload.outcome == 'success'" not in str(dec["outputs"].get("finished")):
        out.append("decide says finished before its comment and record landed")
    return out


def faults(wf: dict) -> list[str]:
    out: list[str] = []
    jobs = wf["jobs"]
    if jobs["review"].get("if") != "needs.decide.outputs.mode == 'review'":
        out.append("model review is not keyed on mode == review alone")
    v = jobs["verdict"]
    if not {"decide", "review"} <= set(v.get("needs") or []):
        out.append("verdict does not aggregate decide and review")
    if "!cancelled()" not in str(v.get("if")):
        out.append("verdict can be skipped")
    if any("actions/checkout" in str(s.get("uses")) for s in v["steps"]) or {"defer", "record"} & set(jobs):
        out.append("verdict checks the PR out, or record/defer left the job that never runs PR code")
    green = next((s for s in v["steps"] if "re-actors/alls-green" in str(s.get("uses"))), None)
    if green is None or " ".join(str(green["with"].get("allowed-skips")).split()) != ALLOWED:
        out.append("verdict allows skips beyond the mode's")
    own = next((s for s in v["steps"] if "needs.review.outputs.result" in str((s.get("env") or {}).get("RESULT"))
                and s.get("name") != "record verdict"), None)
    run = str((own or {}).get("run", ""))
    if own is None or own.get("if") or own.get("continue-on-error") or '[ "$RESULT" = pass ]' not in run \
            or "defer) " not in run or run.count("exit 1") < 2:
        out.append("verdict does not read the review's own result")
    d = next((s for s in v["steps"] if s.get("name") == "review deferred"), {})
    if "needs.decide.outputs.mode == 'defer'" not in str(d.get("if")) or "needs.review.outputs.parked == 'true'" not in str(d.get("if")):
        out.append("review deferred does not fire on both parks")
    if "always()" not in str(d.get("if")):
        out.append("review deferred is skipped by a red step above it (implicit success())")
    if "exit 1" not in str(d.get("run")) or d.get("continue-on-error"):
        out.append("review deferred is not red")
    r = next((s for s in v["steps"] if s.get("name") == "record verdict"), {})
    if "always()" not in str(r.get("if")) or not r.get("continue-on-error"):
        out.append("record verdict can be skipped by a red above it, or can turn the gate red")
    started = False
    for s in jobs["review"]["steps"]:
        if s.get("id") == "admit":
            started = True
            continue
        name = s.get("name") or s.get("uses")
        if started and name != "Stop the git ssh agent" and NOT_PARKED not in str(s.get("if", "")):
            out.append(f"model review step runs after a park: {name}")
    # ONE comment and ONE push, in `verdict`, from whichever job finished (never the review job:
    # the PR's code has run there, so it holds no ntfy secret)
    body = str((v.get("env") or {}).get("BODY"))
    if "needs.review.outputs.body" not in body or "needs.decide.outputs.body" not in body:
        out.append("verdict does not post the finishing job's body")
    names = [s.get("name") for s in v["steps"]]
    if "Comment on the PR" not in names or "Push a block to ntfy" not in names:
        out.append("verdict does not comment and push")
    for job in ("decide", "review"):
        if any("ntfy" in str(s.get("env") or {}) or s.get("name") == "Comment on the PR" for s in jobs[job]["steps"]):
            out.append(f"{job} still comments or pushes itself")
        if "steps.body.outputs.body" not in str((jobs[job].get("outputs") or {}).get("body")):
            out.append(f"{job} does not hand its body on")
    out += _carry_skip_faults(jobs)
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


def _green(jobs: dict) -> dict:
    return next(s for s in jobs["verdict"]["steps"] if "re-actors/alls-green" in str(s.get("uses")))


def test_each_fault_fires() -> None:
    assert _mut(lambda j, w: j["review"].update({"if": "needs.decide.outputs.mode != 'skip'"}))
    assert _mut(lambda j, w: j["verdict"].update({"needs": ["decide"]}))
    assert _mut(lambda j, w: j["verdict"].update({"if": "success()"}))
    assert _mut(lambda j, w: _green(j)["with"].update({"allowed-skips": "review"}))
    assert _mut(lambda j, w: _step(j["verdict"], "review deferred").update({"if": "always() && needs.decide.outputs.mode == 'defer'"}))
    assert _mut(lambda j, w: _step(j["verdict"], "review deferred").update({"run": "echo parked"}))
    assert _mut(lambda j, w: _step(j["verdict"], "review deferred").update({"continue-on-error": True}))
    assert _mut(lambda j, w: _step(j["verdict"], "review deferred").update(
        {"if": "needs.decide.outputs.mode == 'defer' || needs.review.outputs.parked == 'true'"}))
    assert _mut(lambda j, w: _step(j["verdict"], "record verdict").update({"continue-on-error": False}))
    assert _mut(lambda j, w: _step(j["verdict"], "record").update({"if": "needs.decide.outputs.hash != ''"}))
    assert _mut(lambda j, w: j["verdict"]["steps"].insert(0, {"uses": "actions/checkout@v7"}))
    assert _mut(lambda j, w: j["verdict"]["steps"].remove(_step(j["verdict"], "The review's own verdict")))
    assert _mut(lambda j, w: _step(j["verdict"], "The review's own verdict").update({"run": "true"}))
    assert _mut(lambda j, w: _step(j["review"], "review").update({"if": "always()"}))
    assert _mut(lambda j, w: _step(j["review"], "Gate").update({"if": "!cancelled()"}))
    assert _mut(lambda j, w: j["verdict"]["env"].update({"BODY": "${{ needs.decide.outputs.body }}"}))
    assert _mut(lambda j, w: j["review"]["outputs"].pop("body"))
    assert _mut(lambda j, w: j["review"]["steps"].append({"name": "Comment on the PR", "run": "true"}))
    assert _mut(lambda j, w: (w.get("on") or w[True])["workflow_call"]["outputs"]["result"].update(
        {"value": "${{ jobs.review.outputs.result }}"}))


def _fin(jobs: dict) -> dict:
    return _step(jobs["decide"], "finish")


def test_the_carry_skip_fires_on_each_mutation() -> None:
    # a skip keyed on `finished` alone (decide red but finished) would read a red decide's run green
    assert _mut(lambda j, w: j["verdict"].update({"if": "${{ !cancelled() && needs.decide.outputs.finished != 'true' }}"}))
    assert _mut(lambda j, w: j["verdict"].update({"if": "${{ needs.decide.outputs.mode != 'finish' }}"}))
    assert _mut(lambda j, w: _fin(j).update({"if": "always() && " + " && ".join(FINISH_NEEDS)}))
    assert _mut(lambda j, w: _fin(j).update({"if": "success() && steps.mode.outputs.mode == 'finish'"}))
    assert _mut(lambda j, w: j["decide"]["outputs"].update({"finished": "${{ steps.finish.outputs.done }}"}))

    def before_gate(j: dict, w: dict) -> None:
        steps = j["decide"]["steps"]
        steps.insert(0, steps.pop(steps.index(_fin(j))))
    assert _mut(before_gate)


def test_only_a_finished_carry_skips_the_verdict_job_must_not_fire() -> None:
    # the skip condition, evaluated over every (decide result, finished) pair: one skips
    def runs(result: str, finished: str) -> bool:
        return not (result == "success" and finished == "true")
    assert [runs(r, f) for r in ("success", "failure") for f in ("true", "")] == [False, True, True, True]
    v = _wf()["jobs"]["verdict"]
    assert "needs.decide.result == 'success'" in v["if"] and "needs.decide.outputs.finished == 'true'" in v["if"]


def test_a_carry_and_a_blind_and_a_train_are_finished_by_decide() -> None:
    # the jobs that finish a verdict without the sealed runner still hand it on and gate
    d = _wf()["jobs"]["decide"]
    for sid in ("Hand the verdict on", "Gate"):
        cond = str(_step(d, sid).get("if"))
        assert "steps.mode.outputs.mode == 'finish'" in cond and "is_train == 'true'" in cond and "!cancelled()" in cond
    assert "exit 1" in _step(d, "Gate")["run"] and "pass)" in _step(d, "Gate")["run"]


def test_the_record_step_records_a_carry_or_a_review_never_a_park() -> None:
    cond = " ".join(str(_step(_wf()["jobs"]["verdict"], "record")["if"]).split())
    assert "needs.decide.outputs.hash != ''" in cond
    assert "needs.decide.outputs.mode == 'finish' && needs.decide.outputs.result == 'pass'" in cond
    assert "needs.review.outputs.result == 'pass' || needs.review.outputs.result == 'block'" in cond


def test_the_jobs_and_so_the_check_names_are_exactly_three() -> None:
    # `<caller> / review`, `<caller> / model review`, `<caller> / verdict`; the caller's `review gate`
    # (the REQUIRED check) aggregates the call and is untouched (v1.35.0: two boots fewer)
    jobs = _wf()["jobs"]
    assert {k: v["name"] for k, v in jobs.items()} == {"decide": "review", "review": "model review", "verdict": "verdict"}


# ── Mode, executed ────────────────────────────────────────────────────────────────────────────
BASH = shutil.which("bash")


def _mode(**env: str) -> dict[str, str]:
    step = _step(_wf()["jobs"]["decide"], "mode")
    with tempfile.TemporaryDirectory() as d:
        out = pathlib.Path(d) / "output"
        out.write_text("", encoding="utf-8")
        # a real file, never os.devnull: bash on Windows writes `nul` as a file in the cwd
        full = {**os.environ, "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(pathlib.Path(d) / "summary"),
                **{k: "" for k in ("IS_TRAIN", "REQUIRED", "BLIND", "CARRIED", "DEFER")}, **env}
        subprocess.run([BASH, "-e", "-c", step["run"]], env=full, check=True, capture_output=True, text=True)
        return dict(line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines() if "=" in line)


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
