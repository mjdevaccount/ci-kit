"""ci-image-pr-filter must fire: a file the Dockerfile COPYs by name that the paths-filter does not
list -> 1, a COPY of a path the repo does not hold -> 1, every file listed -> 0; no Dockerfile or
no filter key -> 2."""
from __future__ import annotations

import pytest

pytest.importorskip("yaml")

from _pylint_repo import again, run  # noqa: E402
from conftest import git, write  # noqa: E402

MOD = "image_pr_filter"
WORKFLOW = """\
jobs:
  changes:
    steps:
      - uses: dorny/paths-filter@v3
        with:
          filters: |
            image:
              - 'Dockerfile'
              - 'pyproject.toml'
              - 'docs/GATE.md'
"""
DOCKERFILE = """\
FROM python:3.14-slim
COPY pyproject.toml \\
     ./
COPY --chown=app:app app/ app/
COPY ["docs/GATE.md", "docs/"]
ADD prompts/*.yaml prompts/
"""


def _files(**extra: str) -> dict[str, str]:
    return {"Dockerfile": DOCKERFILE, ".github/workflows/ci.yml": WORKFLOW, "pyproject.toml": "[project]\n",
            "docs/GATE.md": "# gate\n", "docs/NEW.md": "# new\n", "app/main.py": "x = 1\n",
            "prompts/a.yaml": "a: 1\n", **extra}


def test_every_copied_file_listed_passes(new_repo) -> None:
    _, r = run(new_repo, MOD, _files())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "2 file(s) COPYd by name" in r.stdout


def test_a_copied_file_missing_from_the_filter_fires(new_repo) -> None:
    _, r = run(new_repo, MOD, _files(Dockerfile=DOCKERFILE + "COPY docs/NEW.md docs/\n"))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "COPYs docs/NEW.md but .github/workflows/ci.yml's 'image' filter does not list it" in r.stdout


def test_a_copy_of_a_path_the_repo_does_not_hold_fires(new_repo) -> None:
    _, r = run(new_repo, MOD, _files(Dockerfile=DOCKERFILE + "COPY docs/GONE.md docs/\n"))
    assert r.returncode == 1 and "'docs/GONE.md'" in r.stdout, r.stdout + r.stderr


def test_custom_paths_and_key_are_read(new_repo) -> None:
    files = _files(**{"build/app.Dockerfile": DOCKERFILE, ".github/workflows/pr.yml": WORKFLOW.replace("image:", "img:")})
    _, r = run(new_repo, MOD, files, "--dockerfile", "build/app.Dockerfile", "--workflow", ".github/workflows/pr.yml",
               "--filter", "img")
    assert r.returncode == 0, r.stdout + r.stderr


def test_no_dockerfile_is_2(new_repo) -> None:
    repo, _ = run(new_repo, MOD, _files())
    git(repo, "rm", "-qf", "Dockerfile")
    assert again(MOD, repo).returncode == 2


def test_no_filter_key_is_2(new_repo) -> None:
    repo, _ = run(new_repo, MOD, _files())
    write(repo / ".github/workflows/ci.yml", WORKFLOW.replace("image:", "other:"))
    r = again(MOD, repo)
    assert r.returncode == 2 and "'image'" in r.stderr, r.stdout + r.stderr
