"""Every workflow step that runs npm/npx installs node first, in its own job -- must fire.

A self-hosted pool's image carries what its builder put there, not what ubuntu-latest has: the
sealed VM has no node, so the review gate's `npm install -g @anthropic-ai/claude-code` failed with
`npm: command not found` and every review went BLIND once the job moved there (1.22.1).
"""
from __future__ import annotations

import pathlib
import re

import yaml

WORKFLOWS = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"
_NPM = re.compile(r"(^|[\s;&|(])(npm|npx)\s")


def node_faults(workflow: dict) -> list[str]:
    """`job: step` for each npm/npx step with no actions/setup-node step before it in its job."""
    out = []
    for name, job in (workflow.get("jobs") or {}).items():
        have_node = False
        for i, step in enumerate(job.get("steps") or []):
            if str(step.get("uses", "")).startswith("actions/setup-node@"):
                have_node = True
            elif _NPM.search(str(step.get("run", ""))) and not have_node:
                out.append(f"{name}: step {i} ({step.get('name', 'unnamed')})")
    return out


def test_every_shipped_workflow_installs_node_before_npm() -> None:
    files = sorted(WORKFLOWS.glob("*.yml"))
    assert files, "no workflows found: the check would pass blind"
    faults = {f.name: node_faults(yaml.safe_load(f.read_text(encoding="utf-8"))) for f in files}
    assert {k: v for k, v in faults.items() if v} == {}


def test_the_check_fires_on_npm_without_setup_node() -> None:
    bad = {"jobs": {"review": {"steps": [{"name": "Install", "run": "npm install -g x && x --version"}]}}}
    assert node_faults(bad) == ["review: step 0 (Install)"]
    good = {"jobs": {"review": {"steps": [{"uses": "actions/setup-node@v7"}, {"run": "npm install -g x"}]}}}
    assert node_faults(good) == []
    assert node_faults({"jobs": {"j": {"steps": [{"run": "echo npmrc; pnpm i"}]}}}) == []
