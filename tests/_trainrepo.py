"""Real git repos for the merge-train tests: a bare ORIGIN (GitHub's stand-in, with refs/pull/N/head)
and a work clone. No network; every git call is the real binary."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ENV = {**os.environ, "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
       "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
       "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
LINES = "".join(f"line {i}\n" for i in range(1, 21))


def git(cwd: Path, *args: str, env: dict | None = None) -> str:
    return subprocess.run(["git", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", *args],
                          cwd=cwd, check=True, capture_output=True, text=True, env=env or ENV).stdout.strip()


class Repo:
    """origin (bare) + work (a clone). PRs are branches pushed to origin as refs/pull/N/head."""

    def __init__(self, root: Path) -> None:
        self.origin = root / "origin.git"
        self.work = root / "work"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "--bare")
        self.work.mkdir()
        git(self.work, "init", "-q")
        git(self.work, "remote", "add", "origin", str(self.origin))
        self.write({"a.txt": LINES, "b.txt": LINES, "c.txt": LINES})
        self.main = self.commit("root")
        git(self.work, "push", "-q", "origin", "HEAD:refs/heads/main")

    def write(self, files: dict[str, str]) -> None:
        for path, text in files.items():
            p = self.work / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(text.encode())

    def commit(self, msg: str) -> str:
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "--allow-empty", "-m", msg)
        return git(self.work, "rev-parse", "HEAD")

    def advance_main(self, files: dict[str, str], msg: str = "main moves") -> str:
        git(self.work, "checkout", "-q", "--detach", self.main)
        self.write(files)
        self.main = self.commit(msg)
        git(self.work, "push", "-q", "origin", f"{self.main}:refs/heads/main")
        return self.main

    def pr(self, number: int, files: dict[str, str], msg: str = "", base: str = "") -> str:
        """A PR branch off `base` (default main), pushed as refs/pull/<n>/head; returns its head."""
        git(self.work, "checkout", "-q", "--detach", base or self.main)
        self.write(files)
        head = self.commit(msg or f"change {number}")
        git(self.work, "push", "-q", "origin", f"+{head}:refs/pull/{number}/head")
        git(self.work, "push", "-q", "origin", f"+{head}:refs/heads/pr{number}")
        return head


def edit(line: int, text: str) -> str:
    """LINES with one line replaced."""
    rows = LINES.splitlines(keepends=True)
    rows[line - 1] = text + "\n"
    return "".join(rows)
