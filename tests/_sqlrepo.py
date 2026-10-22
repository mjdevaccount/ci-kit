"""A throwaway repo whose files are TRACKED (the sql gates index with `git ls-files`)."""
from __future__ import annotations

from pathlib import Path

from conftest import git, write


def tracked_repo(new_repo, files: dict[str, str]) -> Path:
    r = new_repo()
    for rel, text in files.items():
        write(r / rel, text)
    git(r, "add", "-A")
    return r
