"""Where the review gate's jobs may run -- must fire, both directions.

`review` holds the Claude credential and runs the PR's own tests: hosted, or the SEALED pool (one
job per boot on a VM rolled back to a clean snapshot, home-host docs/CI-RUNNER-CONFIG-PLAN.md
§ Sealed pool) -- never the secure pool, which runs jobs side by side, nor the socket pool.
`record` holds no secret but writes the artifact a later run trusts to carry a verdict: the
SECURE pool (no docker socket) or hosted, never the shared socket pool (CI_RUNNER), where a
concurrent PR job is root over docker. Both jobs' labels are fixed in the workflow, so a repo
variable can only switch a job on or off (as pr-autoupdate's update job).
"""
from __future__ import annotations

import pathlib

import yaml

WORKFLOW = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "review-gate.yml"
SECURE = "${{ vars.CI_SECURE_RUNNER && fromJSON('[\"self-hosted\",\"home-secure\"]') || 'ubuntu-latest' }}"
SEALED = "${{ vars.CI_SEALED_RUNNER && fromJSON('[\"self-hosted\",\"home-sealed\"]') || 'ubuntu-latest' }}"


def placement_faults(jobs: dict) -> list[str]:
    """Each job that runs somewhere its class may not."""
    out = []
    if jobs["review"].get("runs-on") not in ("ubuntu-latest", SEALED):
        out.append("review: holds the credential -- hosted or the fixed sealed labels only")
    rec = str(jobs["record"].get("runs-on"))
    if rec not in ("ubuntu-latest", SECURE):
        out.append(f"record: {rec} is neither hosted nor the fixed secure labels")
    if "CI_RUNNER" in rec.replace("CI_SECURE_RUNNER", ""):
        out.append("record: on the shared socket pool")
    return out


def toolchain_faults(job: dict) -> list[str]:
    """A step that runs npm/npx/node before any actions/setup-node step: the hosted image ships
    node, the sealed pool's image does not (its first sealed run went BLIND on exit 127)."""
    out, have_node = [], False
    for i, step in enumerate(job.get("steps", [])):
        if str(step.get("uses", "")).startswith("actions/setup-node@"):
            have_node = True
        words = str(step.get("run", "")).replace("&&", " ").replace(";", " ").split()
        if not have_node and {"npm", "npx", "node"} & set(words):
            out.append(f"review step {i} ({step.get('name', '?')}): node tooling before setup-node")
    return out


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def test_the_shipped_placement_is_clean() -> None:
    jobs = _jobs()
    assert jobs["record"]["runs-on"] == SECURE
    assert jobs["review"]["runs-on"] == SEALED
    assert placement_faults(jobs) == []


def test_fires_when_record_joins_the_socket_pool() -> None:
    jobs = _jobs()
    jobs["record"]["runs-on"] = "${{ vars.CI_RUNNER && fromJSON(vars.CI_RUNNER) || 'ubuntu-latest' }}"
    assert placement_faults(jobs)


def test_fires_when_record_takes_its_labels_from_a_variable() -> None:
    jobs = _jobs()
    jobs["record"]["runs-on"] = "${{ vars.CI_SECURE_RUNNER && fromJSON(vars.CI_SECURE_RUNNER) || 'ubuntu-latest' }}"
    assert placement_faults(jobs)


def test_fires_when_review_joins_the_secure_pool() -> None:
    jobs = _jobs()
    jobs["review"]["runs-on"] = SECURE
    assert placement_faults(jobs)


def test_fires_when_review_takes_its_labels_from_a_variable() -> None:
    jobs = _jobs()
    jobs["review"]["runs-on"] = "${{ vars.CI_SEALED_RUNNER && fromJSON(vars.CI_SEALED_RUNNER) || 'ubuntu-latest' }}"
    assert placement_faults(jobs)


def test_fires_when_review_joins_the_socket_pool() -> None:
    jobs = _jobs()
    jobs["review"]["runs-on"] = "${{ vars.CI_RUNNER && fromJSON(vars.CI_RUNNER) || 'ubuntu-latest' }}"
    assert placement_faults(jobs)


def test_the_shipped_review_job_sets_up_node_before_npm() -> None:
    assert toolchain_faults(_jobs()["review"]) == []


def test_fires_when_setup_node_is_dropped() -> None:
    job = _jobs()["review"]
    job["steps"] = [s for s in job["steps"] if not str(s.get("uses", "")).startswith("actions/setup-node@")]
    assert toolchain_faults(job)


def test_fires_when_npm_moves_above_setup_node() -> None:
    job = _jobs()["review"]
    steps = job["steps"]
    i = next(n for n, s in enumerate(steps) if "npm install" in str(s.get("run", "")))
    steps.insert(0, steps.pop(i))
    assert toolchain_faults(job)
