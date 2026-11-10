"""Every workflow step that runs node tooling sets up node first, in its own job -- must fire.

The class check behind the review gate's 1.22.2 fix: ANY shipped workflow, ANY job, since a
caller may move any job to a self-hosted pool whose image has no node.
"""
from __future__ import annotations

import pathlib

import yaml
from _workflow_node import SETUP_NODE, node_faults

WORKFLOWS = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _workflows() -> dict[str, dict]:
    files = sorted(WORKFLOWS.glob("*.yml"))
    assert files, "no workflows found: the check would pass blind"
    return {f.name: yaml.safe_load(f.read_text(encoding="utf-8")) for f in files}


def test_every_shipped_workflow_sets_up_node_before_node_tooling() -> None:
    faults = {name: node_faults(wf) for name, wf in _workflows().items()}
    assert {k: v for k, v in faults.items() if v} == {}


def test_the_check_sees_the_review_gate_install() -> None:
    # proves the shipped pass is not blind: drop setup-node from the real file and it fires
    wf = _workflows()["review-gate.yml"]
    for job in wf["jobs"].values():
        job["steps"] = [s for s in job.get("steps") or [] if not str(s.get("uses", "")).startswith(SETUP_NODE)]
    assert node_faults(wf)


def _early(run: str) -> dict:
    return {"jobs": {"j": {"steps": [{"name": "early", "run": run}, {"uses": SETUP_NODE + "v7"}]}}}


def test_fires_on_each_node_tool_spelling() -> None:
    for run in ("npm ci", "x && npx foo", "/usr/bin/node -v", "echo $(node -v)", "pnpm i", "yarn",
                "corepack enable", "set -e; npm test", "a|npm run x", "npm"):
        assert node_faults(_early(run)) == ["j: step 0 (early)"], run


def test_ignores_comments_and_lookalikes() -> None:
    for run in ("# npm is installed below", "  # node later", "echo node-version", "npmrc=1",
                "nodejs_dir=x", "echo yarnball", "echo npmrc; true"):
        assert node_faults(_early(run)) == [], run


def test_setup_node_covers_only_its_own_job() -> None:
    wf = {"jobs": {"a": {"steps": [{"uses": SETUP_NODE + "v7"}]},
                   "b": {"steps": [{"name": "install", "run": "npm install -g x"}]}}}
    assert node_faults(wf) == ["b: step 0 (install)"]
