"""Where the review gate's jobs may run -- must fire, both directions.

`review` holds the Claude credential and runs the PR's own tests: hosted only, under the trust
boundary in home-host docs/CI-RUNNER.md. `record` holds no secret but writes the artifact a later
run trusts to carry a verdict: the SECURE pool (no docker socket) or hosted, never the shared
socket pool (CI_RUNNER), where a concurrent PR job is root over docker. Its labels are fixed in
the workflow, so a repo variable can only switch it on or off (as pr-autoupdate's update job).
"""
from __future__ import annotations

import pathlib

import yaml

WORKFLOW = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "review-gate.yml"
SECURE = "${{ vars.CI_SECURE_RUNNER && fromJSON('[\"self-hosted\",\"home-secure\"]') || 'ubuntu-latest' }}"


def placement_faults(jobs: dict) -> list[str]:
    """Each job that runs somewhere its class may not."""
    out = []
    if jobs["review"].get("runs-on") != "ubuntu-latest":
        out.append("review: holds the credential, must stay hosted")
    rec = str(jobs["record"].get("runs-on"))
    if rec not in ("ubuntu-latest", SECURE):
        out.append(f"record: {rec} is neither hosted nor the fixed secure labels")
    if "CI_RUNNER" in rec.replace("CI_SECURE_RUNNER", ""):
        out.append("record: on the shared socket pool")
    return out


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def test_the_shipped_placement_is_clean() -> None:
    jobs = _jobs()
    assert jobs["record"]["runs-on"] == SECURE
    assert placement_faults(jobs) == []


def test_fires_when_record_joins_the_socket_pool() -> None:
    jobs = _jobs()
    jobs["record"]["runs-on"] = "${{ vars.CI_RUNNER && fromJSON(vars.CI_RUNNER) || 'ubuntu-latest' }}"
    assert placement_faults(jobs)


def test_fires_when_record_takes_its_labels_from_a_variable() -> None:
    jobs = _jobs()
    jobs["record"]["runs-on"] = "${{ vars.CI_SECURE_RUNNER && fromJSON(vars.CI_SECURE_RUNNER) || 'ubuntu-latest' }}"
    assert placement_faults(jobs)


def test_fires_when_review_leaves_hosted() -> None:
    jobs = _jobs()
    jobs["review"]["runs-on"] = SECURE
    assert placement_faults(jobs)
