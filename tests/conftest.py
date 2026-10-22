"""Shared fixtures: throwaway git repos where the bad condition is REAL, not mocked.

The must-fire convention (W3-1): every gate is driven against a known-BAD input (exit 1), a
known-GOOD input (exit 0) and a BLIND one it cannot read (exit 2). A gate only ever seen passing
is indistinguishable from one that cannot fail.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

NL = chr(10)


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=False)


def run_tool(module: str, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a tool the way a caller does: from INSIDE the caller's checkout, not from the kit's own checkout."""
    return subprocess.run([sys.executable, "-m", f"ci_kit.{module}", *args], cwd=cwd,
                          capture_output=True, text=True, check=False, encoding="utf-8",
                          errors="replace")


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline=NL)


@pytest.fixture
def new_repo(tmp_path: Path):
    def make(name: str = "r") -> Path:
        r = tmp_path / name
        r.mkdir()
        git(r, "init", "-q", "-b", "main")
        git(r, "config", "user.email", "t@example.com")
        git(r, "config", "user.name", "t")
        git(r, "config", "core.autocrlf", "false")
        return r
    return make
