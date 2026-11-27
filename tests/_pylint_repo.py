"""The moved-from-trade-data python gates (v1.49.0), driven as a caller runs them: a throwaway REAL
git repo, the tool run from inside it, the exit code the verdict. One helper, so four must-fire
suites are a table each and not four copies of the same harness."""
from __future__ import annotations

from pathlib import Path

from _sqlrepo import tracked_repo
from conftest import run_tool

PAD = "x = 1\n"


def pads(d: str, n: int) -> dict[str, str]:
    """`n` small tracked modules under `d`, for a per-dir floor."""
    return {f"{d}/pad_{i}.py": PAD for i in range(n)}


def run(new_repo, module: str, files: dict[str, str], *args: str):
    repo = tracked_repo(new_repo, files)
    return repo, run_tool(module, repo, "--root", str(repo), *args)


def again(module: str, repo: Path, *args: str):
    return run_tool(module, repo, "--root", str(repo), *args)
