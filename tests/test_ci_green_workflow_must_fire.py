"""ci-green.yml's guard and placement -- must fire, both directions.

The job pushes with a `contents: write` token, so it may run hosted or on the SECURE pool (fixed
labels, no docker socket), never the shared socket pool (CI_RUNNER). And it may only advance on a
SUCCESSFUL push/dispatch run of the DEFAULT branch from THIS repo: drop any one clause and a PR
run's head, a fork's `main`, or a red run would be published as green. Each clause is removed from
a copy and the check must fail.
"""
from __future__ import annotations

import pathlib

import yaml

WORKFLOW = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci-green.yml"
SECURE = "${{ vars.CI_SECURE_RUNNER && fromJSON('[\"self-hosted\",\"home-secure\"]') || 'ubuntu-latest' }}"
CLAUSES = (
    "github.event_name == 'workflow_run'",
    "github.event.workflow_run.conclusion == 'success'",
    "github.event.workflow_run.head_branch == github.event.repository.default_branch",
    "github.event.workflow_run.event == 'push'",
    "github.event.workflow_run.head_repository.full_name == github.repository",
)
HEAD = "${{ github.event.workflow_run.head_sha }}"
EVENTS = ("(github.event.workflow_run.event == 'push' || "
          "github.event.workflow_run.event == 'workflow_dispatch')")
ENV = "${{ vars.CI_SECURE_RUNNER && 'ci-secure' || '' }}"


def faults(job: dict) -> list[str]:
    out = []
    runs_on = str(job.get("runs-on"))
    if runs_on not in ("ubuntu-latest", SECURE):
        out.append(f"runs-on {runs_on}: neither hosted nor the fixed secure labels")
    guard = " ".join(str(job.get("if", "")).split())
    out += [f"guard lacks: {c}" for c in CLAUSES if c not in guard]
    if "pull_request" in guard:
        out.append("guard admits a pull_request run")
    # a conjunction: the only `||` allowed is inside the push|dispatch parenthetical
    if "||" in guard.replace(EVENTS, ""):
        out.append("guard has a top-level ||: one clause can admit what the others refuse")
    if str(job.get("environment")) != ENV:
        out.append(f"environment {job.get('environment')}: not ci-secure on the secure pool")
    steps = job.get("steps", [])
    co = next((s for s in steps if str(s.get("uses", "")).startswith("actions/checkout")), None)
    if co is None or co.get("with", {}).get("ref") != HEAD or co.get("with", {}).get("fetch-depth") != 0:
        out.append("checkout is not the run's head_sha at full depth")
    elif co["with"].get("token") != "${{ secrets.token }}":
        out.append("checkout does not carry the PAT: a GITHUB_TOKEN push over a workflow file is refused")
    tool = next((s for s in steps if s.get("with", {}).get("tool") == "ci-green"), None)
    if tool is None or f"--sha {HEAD}" not in " ".join(str(tool["with"].get("args", "")).split()):
        out.append("ci-green does not get --sha head_sha")
    return out


def _job() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["advance"]


def test_shipped_workflow_is_clean() -> None:
    assert faults(_job()) == []


def test_socket_pool_fires() -> None:
    job = _job()
    job["runs-on"] = "${{ vars.CI_RUNNER && fromJSON(vars.CI_RUNNER) || 'ubuntu-latest' }}"
    assert any("runs-on" in f for f in faults(job))


def test_each_guard_clause_fires() -> None:
    for clause in CLAUSES:
        job = _job()
        job["if"] = " ".join(str(job["if"]).split()).replace(clause, "true")
        assert f"guard lacks: {clause}" in faults(job), clause


def test_pr_run_admitted_fires() -> None:
    job = _job()
    job["if"] = str(job["if"]) + " || github.event.workflow_run.event == 'pull_request'"
    assert "guard admits a pull_request run" in faults(job)


def _checkout(job: dict) -> dict:
    return next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout"))


def test_checkout_of_branch_tip_fires() -> None:
    job = _job()
    _checkout(job)["with"]["ref"] = "main"
    assert "checkout is not the run's head_sha at full depth" in faults(job)


def test_shallow_checkout_fires() -> None:
    job = _job()
    _checkout(job)["with"]["fetch-depth"] = 1
    assert "checkout is not the run's head_sha at full depth" in faults(job)


def test_top_level_or_fires() -> None:
    job = _job()
    job["if"] = "(" + str(job["if"]) + ") || true"
    assert any("top-level ||" in f for f in faults(job))


def test_missing_environment_fires() -> None:
    job = _job()
    del job["environment"]
    assert any(f.startswith("environment") for f in faults(job))


def test_github_token_checkout_fires() -> None:
    job = _job()
    del _checkout(job)["with"]["token"]
    assert any("PAT" in f for f in faults(job))
