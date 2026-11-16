"""Where the heal ladder's jobs run and what each may hold (.github/workflows/pr-heal.yml) -- both directions.

The jobs that run PR CODE (regen: the generators and the caller's install; fix: Claude Code with
Bash) hold no push token and no ntfy URL. The jobs that hold the token (push, report) run no PR
code: no install, no generator, no checkout of the PR as code to execute. Tier 3 runs only on the
sealed pool or hosted, never side by side with other jobs; the token-only jobs on the secure pool.
"""
from __future__ import annotations

import json
import pathlib

import _runners
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
HEAL = ROOT / ".github" / "workflows" / "pr-heal.yml"
UPD = ROOT / ".github" / "workflows" / "pr-autoupdate.yml"
SECURE = _runners.SECURE
SEALED = _runners.SEALED


def _jobs(path: pathlib.Path = HEAL) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]


def _secrets(job: dict) -> set[str]:
    t = json.dumps(job)
    return {x.split("}}")[0].split()[0].strip() for x in t.split("secrets.")[1:]}


def faults(jobs: dict) -> list[str]:
    out = []
    for name in ("regen", "fix"):
        held = _secrets(jobs[name]) - {"git-ssh-key", "claude-code-oauth-token", "anthropic-api-key"}
        if held:
            out.append(f"{name}: runs PR code and holds {sorted(held)}")
        if "claude" in json.dumps(jobs["regen"]).lower() and name == "regen" and \
                ("claude-code-oauth-token" in _secrets(jobs["regen"]) or "anthropic-api-key" in _secrets(jobs["regen"])):
            out.append("regen: holds a Claude credential")
    if jobs["fix"].get("runs-on") not in ("ubuntu-latest", SEALED):
        out.append("fix: tier 3 -- hosted or the sealed pool only")
    for name in ("plan", "claim", "push", "report"):
        if jobs[name].get("runs-on") not in ("ubuntu-latest", SECURE):
            out.append(f"{name}: token-only -- hosted or the secure pool only")
        steps = json.dumps(jobs[name].get("steps", []))
        for bad in ("inputs.install", "pr_regen run", "pr_fix run", "setup-uv", "setup-node"):
            if bad in steps:
                out.append(f"{name}: runs {bad!r} beside the token")
    if "token" in _secrets(jobs["plan"]):
        out.append("plan: holds the push token")
    return out


def test_the_shipped_heal_workflow_is_clean() -> None:
    assert faults(_jobs()) == []


def test_the_check_catches_a_token_beside_pr_code_must_fire() -> None:
    jobs = _jobs()
    jobs["regen"]["env"] = {"GH_TOKEN": "${{ secrets.token }}"}
    jobs["push"]["steps"].append({"run": "eval \"${{ inputs.install }}\""})
    jobs["fix"]["runs-on"] = "${{ fromJSON(vars.CI_RUNNER) }}"
    got = faults(jobs)
    assert any(f.startswith("regen: runs PR code") for f in got)
    assert any(f.startswith("push: runs") for f in got) and any(f.startswith("fix:") for f in got)


def test_the_push_is_never_forced_and_never_a_rebase() -> None:
    import inspect

    from ci_kit import pr_apply
    runs = " ".join(st.get("run", "") for j in _jobs().values() for st in j.get("steps", []))
    code = inspect.getsource(pr_apply.push) + inspect.getsource(pr_apply.apply)
    for text in (runs, code):
        assert "--force" not in text and "rebase" not in text and "+HEAD" not in text


def test_the_updater_exposes_the_ladder_rows_and_its_knobs() -> None:
    w = yaml.safe_load(UPD.read_text(encoding="utf-8"))
    on = w.get("on") or w.get(True)
    call = on["workflow_call"]
    assert {"heal", "heal-claude-per-pass", "heal-claude-per-pr", "heal-per-pr", "heal-stale-minutes"} <= set(call["inputs"])
    assert call["inputs"]["heal"]["default"] is False                    # opt-in: off means the old behaviour
    assert "heal" in call["outputs"]
    action = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))
    assert "heal" in action["outputs"]


def test_tier_3_runs_only_behind_a_claim_made_in_this_attempt() -> None:
    jobs = _jobs()
    assert "claim" in jobs["fix"]["needs"]
    cond = jobs["fix"]["if"]
    assert "needs.claim.outputs.claimed == 'true'" in cond and "github.run_attempt" in cond
    assert jobs["claim"]["outputs"]["attempt"] == "${{ github.run_attempt }}"


# ── v1.35.0: the header counts the jobs; the sealed hold is bounded by the code that fills it ─
def test_the_header_counts_the_jobs_it_lists() -> None:
    words = {3: "THREE", 4: "FOUR", 5: "FIVE", 6: "SIX", 7: "SEVEN"}
    head = HEAL.read_text(encoding="utf-8").split("name: pr-heal", 1)[0]
    jobs = _jobs()
    assert f"# {words[len(jobs)]} JOBS, ONE TRUST BOUNDARY EACH" in head
    for j in jobs:
        assert f"#   {j} " in head, f"the header does not describe job `{j}`"


def sealed_faults(jobs: dict) -> list[str]:
    from ci_kit import pr_fix
    fix = jobs["fix"]
    step = {s.get("name"): s for s in fix["steps"]}
    out = []
    if step["Fix"].get("timeout-minutes") != pr_fix.FIX_STEP_MIN or pr_fix.FIX_STEP_MIN * 60 <= pr_fix.CLI_TIMEOUT:
        out.append("the Fix step is not bounded by the CLI's own timeout plus the save")
    bounded = sum(s.get("timeout-minutes", 0) for n, s in step.items() if n != "Fix")
    if not bounded or bounded > pr_fix.SETUP_MIN or any(
            "timeout-minutes" not in step[n] for n in ("Install the project (a deploy key only in its own load step)",
                                                       "Install Claude Code")):
        out.append("a setup step that can stall is unbounded, or the bounds exceed SETUP_MIN")
    if fix.get("timeout-minutes") != pr_fix.FIX_JOB_MIN:
        out.append("the fix job's bound is not FIX_JOB_MIN")
    return out


def test_the_sealed_hold_is_sized_from_the_code_must_not_fire() -> None:
    assert sealed_faults(_jobs()) == []


def test_each_sealed_bound_fault_fires_must_fire() -> None:
    import copy
    for mut in (lambda j: j["fix"].update({"timeout-minutes": 360}),
                lambda j: next(s for s in j["fix"]["steps"] if s.get("name") == "Fix").pop("timeout-minutes"),
                lambda j: next(s for s in j["fix"]["steps"] if s.get("name") == "Install Claude Code").pop("timeout-minutes")):
        jobs = copy.deepcopy(_jobs())
        mut(jobs)
        assert sealed_faults(jobs)
