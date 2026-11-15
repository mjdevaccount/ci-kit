"""Where relock.yml's jobs run, what each may hold, and when it may run at all -- both directions.

`lock` RUNS PR CODE (the lock tool can run a build backend): it holds no token, only an optional
read-only deploy key, and runs HOSTED. `push` holds the token and runs no PR code, on hosted or the
fixed secure labels, and takes nothing from `lock` but its `state` (to decide whether to run) and
the artifact (which it re-checks): its paths and its kit come from its own inputs. Every clause of
the event gate is removed from a copy and the check must fail; a non-pull_request caller is a RED
job, never a quiet skip.
"""
from __future__ import annotations

import copy
import json
import pathlib

import yaml
from test_heal_workflow_must_fire import SECURE, _secrets

WF = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "relock.yml"
GATE = (
    "github.event_name == 'pull_request'",
    "inputs.branch-prefix != ''",
    "!contains(inputs.branch-prefix, ' ')",
    "github.event.pull_request.head.repo.full_name == github.repository",
    "startsWith(github.event.pull_request.head.ref, inputs.branch-prefix)",
)
SAME_REPO = GATE[3]
PR_CODE = ("setup-uv", "setup-node", "setup-dotnet", "heal-install", "pr_regen run", "inputs.install")


def _wf() -> dict:
    return yaml.safe_load(WF.read_text(encoding="utf-8"))


def _on(wf: dict) -> dict:
    return wf.get("on") or wf.get(True)


def faults(wf: dict) -> list[str]:
    jobs, out = wf["jobs"], []
    if set(_on(wf)) != {"workflow_call"}:
        out.append(f"triggers {sorted(_on(wf))}: a reusable is workflow_call only")
    lock, push, trig = jobs["lock"], jobs["push"], jobs.get("trigger") or {}
    # lock: PR code, no token, hosted
    held = _secrets(lock) - {"git-ssh-key"}
    if held:
        out.append(f"lock: runs PR code and holds {sorted(held)}")
    if lock.get("runs-on") != "ubuntu-latest":
        out.append(f"lock: runs-on {lock.get('runs-on')} -- hosted only (a shared pool can rewrite its output)")
    if (lock.get("permissions") or {}) != {"contents": "read"}:
        out.append("lock: wider than contents: read")
    guard = " ".join(str(lock.get("if", "")).split())
    out += [f"lock: gate lacks: {c}" for c in GATE if c not in guard]
    if "||" in guard:
        out.append("lock: gate has an ||: one clause can admit what the others refuse")
    # push: the token, no PR code, its own inputs
    if push.get("runs-on") not in ("ubuntu-latest", SECURE):
        out.append(f"push: runs-on {push.get('runs-on')} -- hosted or the fixed secure labels only")
    steps = json.dumps(push.get("steps", []))
    out += [f"push: runs {bad!r} beside the token" for bad in PR_CODE if bad in steps]
    if "needs.lock.outputs" in steps or "needs.lock.outputs" in json.dumps(push.get("env", {})):
        out.append("push: reads a lock output past its `if` -- code in lock can write any output")
    pguard = " ".join(str(push.get("if", "")).split())
    for c in ("needs.lock.outputs.state == 'regen'", GATE[0], SAME_REPO):
        if c not in pguard:
            out.append(f"push: if lacks: {c}")
    apply = next((s for s in push.get("steps", []) if s.get("id") == "apply"), {})
    env = apply.get("env") or {}
    if env.get("LOCKFILES") != "${{ inputs.lockfiles }}" or "--lockfiles \"$LOCKFILES\"" not in apply.get("run", ""):
        out.append("push: the paths it writes are not its own `lockfiles` input")
    kit = next((s for s in push.get("steps", []) if s.get("id") == "kit"), {})
    if (kit.get("with") or {}).get("ref") != "${{ inputs.kit-ref }}":
        out.append("push: ci-kit is not fetched at its own kit-ref")
    if (kit.get("with") or {}).get("base"):
        out.append("push: fetches the base after a credential-less checkout (fails on a private caller)")
    tok = next((s for s in push.get("steps", []) if s.get("id") == "token"), {})
    if (tok.get("with") or {}).get("scope") != "push":
        out.append("push: no pr-token step with scope push")
    # trigger: any other caller event, or an empty prefix, is red
    tif = " ".join(str(trig.get("if", "")).split())
    ws = ("contains(inputs.branch-prefix, ' ')", "contains(inputs.branch-prefix, fromJSON('\"\\t\"'))",
          "contains(inputs.branch-prefix, fromJSON('\"\\n\"'))")
    if "github.event_name != 'pull_request'" not in tif or "inputs.branch-prefix == ''" not in tif \
            or any(w not in tif for w in ws) or "exit 1" not in json.dumps(trig.get("steps", [])):
        out.append("trigger: a non-pull_request caller or an empty prefix would skip green")
    text = WF.read_text(encoding="utf-8").split("\nname: relock", 1)[1]
    for bad in ("--force", "rebase", "curl ", "raw.githubusercontent", "pull_request_target:"):
        if bad in text:
            out.append(f"the workflow body holds {bad!r}")
    return out


def test_the_shipped_relock_workflow_is_clean() -> None:
    assert faults(_wf()) == []


def test_a_token_beside_pr_code_or_a_pool_runner_is_caught_must_fire() -> None:
    wf = _wf()
    wf["jobs"]["lock"]["env"] = {"GH_TOKEN": "${{ secrets.token }}"}
    wf["jobs"]["lock"]["runs-on"] = "${{ fromJSON(vars.CI_RUNNER) }}"
    wf["jobs"]["push"]["steps"].insert(0, {"uses": "astral-sh/setup-uv@v10.3.3"})
    wf["jobs"]["push"]["runs-on"] = "${{ fromJSON(vars.CI_SECURE_RUNNER) }}"
    got = faults(wf)
    assert any(f.startswith("lock: runs PR code and holds ['token']") for f in got)
    assert any(f.startswith("lock: runs-on") for f in got) and any(f.startswith("push: runs-on") for f in got)
    assert "push: runs 'setup-uv' beside the token" in got


def test_each_gate_clause_is_load_bearing_must_fire() -> None:
    for c in GATE:
        wf = _wf()
        wf["jobs"]["lock"]["if"] = wf["jobs"]["lock"]["if"].replace(c, "true")
        assert f"lock: gate lacks: {c}" in faults(wf), c
    wf = _wf()
    wf["jobs"]["lock"]["if"] += " || github.actor == 'renovate[bot]'"
    assert any("||" in f for f in faults(wf))


def test_push_trusts_no_lock_output_and_no_lock_kit_must_fire() -> None:
    wf = _wf()
    apply = next(s for s in wf["jobs"]["push"]["steps"] if s.get("id") == "apply")
    apply["env"]["LOCKFILES"] = "${{ needs.lock.outputs.paths }}"
    kit = next(s for s in wf["jobs"]["push"]["steps"] if s.get("id") == "kit")
    kit["with"]["ref"] = "${{ needs.lock.outputs.kit-sha }}"
    got = faults(wf)
    assert any("reads a lock output" in f for f in got)
    assert "push: the paths it writes are not its own `lockfiles` input" in got
    assert "push: ci-kit is not fetched at its own kit-ref" in got


def test_a_wrong_event_or_empty_prefix_is_red_not_a_skip_must_fire() -> None:
    for mut in (lambda t: t.update({"if": "github.event_name != 'pull_request'"}),
                lambda t: t["steps"][0].update({"run": "echo skipped"})):
        wf = copy.deepcopy(_wf())
        mut(wf["jobs"]["trigger"])
        assert "trigger: a non-pull_request caller or an empty prefix would skip green" in faults(wf)
    wf = _wf()
    _on(wf)["pull_request_target"] = {}
    assert any(f.startswith("triggers") for f in faults(wf))


def test_a_whitespace_prefix_is_red_and_push_fetches_no_base_must_fire() -> None:
    wf = _wf()
    wf["jobs"]["trigger"]["if"] = wf["jobs"]["trigger"]["if"].replace("|| contains(inputs.branch-prefix, ' ')", "")
    assert "trigger: a non-pull_request caller or an empty prefix would skip green" in faults(wf)
    wf = _wf()
    next(s for s in wf["jobs"]["push"]["steps"] if s.get("id") == "kit")["with"]["base"] = "x"
    assert any("fetches the base" in f for f in faults(wf))


def test_the_push_gate_rechecks_the_event_must_fire() -> None:
    wf = _wf()
    wf["jobs"]["push"]["if"] = "needs.lock.outputs.state == 'regen'"
    got = faults(wf)
    assert f"push: if lacks: {GATE[0]}" in got and f"push: if lacks: {SAME_REPO}" in got
