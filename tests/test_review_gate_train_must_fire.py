"""The review gate's merge-train WIRING -- must fire, both directions.

ci_kit/review/train.py decides (tests/test_merge_train_git.py proves it passes a genuine train and
blocks every forged one). This pins that the workflow can neither skip that decision nor let it
fail open: the train step keys on the `train/` prefix and writes is_train before python runs; it
is never continue-on-error; on a train, Decide (and so the model) does not run; the Comment and
the Gate run and read the TRAIN's result, so a train block or BLIND is a red check; and the job's
`result` output carries it. Each predicate is shown to fire on the mutation it exists for.
Since v1.29.0 all of that lives in the `decide` job (named `review`), which never runs the model.
"""
from __future__ import annotations

import copy
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"
TRAIN_IF = "startsWith(github.event.pull_request.head.ref, 'train/')"
IS_TRAIN = "steps.train.outputs.is_train == 'true'"
NOT_TRAIN = "steps.train.outputs.is_train != 'true'"


def _job() -> dict:
    return yaml.safe_load((ROOT / "review-gate.yml").read_text(encoding="utf-8"))["jobs"]["decide"]


def _step(job: dict, *, sid: str = "", name: str = "") -> dict:
    return next(s for s in job["steps"] if (sid and s.get("id") == sid) or (name and s.get("name") == name))


def faults(job: dict) -> list[str]:
    out: list[str] = []
    names = [s.get("id") or s.get("name") for s in job["steps"]]
    if "train" not in names:
        return ["no merge-train step"]
    t = _step(job, sid="train")
    if t.get("if") != TRAIN_IF:
        out.append(f"train step keys on {t.get('if')!r}")
    if t.get("continue-on-error"):
        out.append("train step is continue-on-error: a crash would not turn the job red")
    run = str(t.get("run", ""))
    if "is_train=true" not in run or run.find("is_train=true") > run.find("ci_kit.review.train"):
        out.append("is_train is not written before the verifier runs")
    if names.index("train") > names.index("decide"):
        out.append("train step runs after Decide")
    if NOT_TRAIN not in str(_step(job, sid="decide").get("if", "")):
        out.append("Decide (and the model) runs on a train")
    gate = _step(job, name="Gate")
    if IS_TRAIN not in str(gate.get("if", "")) or "!cancelled()" not in str(gate.get("if", "")):
        out.append("the Gate does not run on a train")
    if "steps.train.outputs.result" not in str((gate.get("env") or {}).get("RESULT", "")):
        out.append("the Gate does not read the train's result")
    if IS_TRAIN not in str(_step(job, name="Comment on the PR").get("if", "")):
        out.append("no comment on a train")
    if "steps.train.outputs.result" not in str((job.get("outputs") or {}).get("result", "")):
        out.append("the job's result output drops the train's")
    return out


def test_the_shipped_wiring_is_clean() -> None:
    assert faults(_job()) == []


def _mutated(fn) -> list[str]:
    job = copy.deepcopy(_job())
    fn(job)
    return faults(job)


def test_each_fault_fires() -> None:
    assert _mutated(lambda j: j["steps"].remove(_step(j, sid="train"))) == ["no merge-train step"]
    assert any("keys on" in f for f in _mutated(lambda j: _step(j, sid="train").update({"if": "false"})))
    assert any("continue-on-error" in f for f in _mutated(lambda j: _step(j, sid="train").update({"continue-on-error": True})))
    assert any("before the verifier" in f for f in _mutated(
        lambda j: _step(j, sid="train").update({"run": "python3 -m ci_kit.review.train\necho is_train=true"})))
    assert any("Decide" in f for f in _mutated(lambda j: _step(j, sid="decide").pop("if")))
    assert any("Gate does not run" in f for f in _mutated(
        lambda j: _step(j, name="Gate").update({"if": "steps.decide.outputs.required == 'true' && !cancelled()"})))
    assert any("train's result" in f for f in _mutated(
        lambda j: _step(j, name="Gate")["env"].update({"RESULT": "${{ steps.verdict.outputs.result }}"})))
    assert any("comment" in f for f in _mutated(
        lambda j: _step(j, name="Comment on the PR").update({"if": "steps.decide.outputs.required == 'true'"})))
    assert any("output" in f for f in _mutated(
        lambda j: j["outputs"].update({"result": "${{ steps.verdict.outputs.result }}"})))


def test_the_gate_step_reads_a_train_block_as_red() -> None:
    # the Gate's case: only `pass` exits 0; the train's block and blind both exit 1
    run = str(_step(_job(), name="Gate")["run"])
    assert "pass)" in run and "block)" in run and "exit 1" in run.split("block)")[1].split(";;")[0]


SECURE = "${{ vars.CI_SECURE_RUNNER && fromJSON('[\"self-hosted\",\"home-secure\"]') || 'ubuntu-latest' }}"


def test_the_train_job_holds_its_token_off_the_socket_pool() -> None:
    wf = yaml.safe_load((ROOT / "merge-train.yml").read_text(encoding="utf-8"))
    job = wf["jobs"]["train"]
    assert job["runs-on"] == SECURE
    assert "GH_TOKEN" in job["env"] and "github.token" not in job["env"]["GH_TOKEN"]
    first = job["steps"][0]
    assert "exit 1" in first["run"] and "GH_TOKEN" in first["run"]       # unconfigured is red
    assert not any("checkout" in str(s.get("uses", "")) for s in job["steps"])   # no PR code on the token's runner
