"""The review gate's own code cannot be shadowed by the PR it grades -- must fire, both directions.

Every step that runs `python -m ci_kit...` has the PR checkout as its cwd, and `-m` puts the cwd
FIRST on sys.path, ahead of PYTHONPATH (the fetched kit). A PR that adds `ci_kit/review/tier.py`
writing `required=false` would skip its own review and turn `review gate` green (found by the
session reviewer on trade-data's caller, 2026-11-06). PYTHONSAFEPATH=1 drops the cwd entry.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import yaml

from ci_kit.review import tools

WORKFLOW = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "review-gate.yml"


def unsafe_steps(job: dict) -> list[str]:
    """Steps that import ci_kit from a cwd-first sys.path."""
    out = []
    for step in job.get("steps", []):
        run = step.get("run", "")
        if "-m ci_kit" in run or '-m "ci_kit' in run:
            if str((step.get("env") or {}).get("PYTHONSAFEPATH", "")) != "1":
                out.append(step.get("name") or step.get("id") or run[:40])
    return out


def _review_job() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["review"]


def test_every_ci_kit_step_is_safepath() -> None:
    job = _review_job()
    assert sum("ci_kit" in s.get("run", "") for s in job["steps"]) >= 5, "the steps this guards are gone"
    assert unsafe_steps(job) == []


def test_the_predicate_fires_on_a_step_without_it() -> None:
    job = _review_job()
    victim = next(s for s in job["steps"] if "-m ci_kit.review.tier" in s.get("run", ""))
    del victim["env"]["PYTHONSAFEPATH"]
    assert unsafe_steps(job) == [victim["name"]]


def _shadow(tmp_path: pathlib.Path) -> pathlib.Path:
    pkg = tmp_path / "ci_kit" / "review"
    pkg.mkdir(parents=True)
    (tmp_path / "ci_kit" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "verdict.py").write_text("print('SHADOWED')\n", encoding="utf-8")
    return tmp_path


def _run(cwd: pathlib.Path, safe: bool) -> str:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONSAFEPATH"}
    if safe:
        env["PYTHONSAFEPATH"] = "1"
    p = subprocess.run([sys.executable, "-m", "ci_kit.review.verdict", "--print-schema"], cwd=cwd, env=env,
                       capture_output=True, text=True, timeout=60)
    return p.stdout


def test_without_safepath_a_pr_ci_kit_shadows_the_gate(tmp_path: pathlib.Path) -> None:
    assert _run(_shadow(tmp_path), safe=False).strip() == "SHADOWED"


def test_with_safepath_the_installed_gate_runs(tmp_path: pathlib.Path) -> None:
    out = _run(_shadow(tmp_path), safe=True)
    assert "SHADOWED" not in out and out.strip().startswith("{")


def test_the_reviewers_tool_runs_do_not_inherit_it() -> None:
    # the PR project's own `python -m pytest` imports its packages off the cwd
    assert "PYTHONSAFEPATH" in tools.VENV_ENV
