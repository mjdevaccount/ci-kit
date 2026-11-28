"""ci-migration-open-prs must fire: a number an OLDER open PR (or the base) already holds -> 1, a free
number -> 0, a base git cannot read / an unreadable --prs-json / no repo -> 2. Replayed PR lists
(`--prs-json`), so no network."""
from __future__ import annotations

import json

from _pylint_repo import again, run
from conftest import git, write

MOD = "migration_open_prs"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
MINE = {"migrations/387_a.sql": "SELECT 1;\n"}


def _replay(repo, *prs: dict) -> tuple[str, ...]:
    write(repo / "prs.json", json.dumps(list(prs)))
    return ("--base", EMPTY_TREE, "--head", "HEAD", "--prs-json", "prs.json")


def _committed(new_repo, files: dict[str, str]):
    repo, _ = run(new_repo, MOD, files, "--selftest")
    git(repo, "commit", "-q", "-m", "c")
    return repo


def test_a_number_an_older_open_pr_holds_fires(new_repo) -> None:
    repo = _committed(new_repo, MINE)
    r = again(MOD, repo, *_replay(repo, {"number": 1, "head": "older", "added": ["migrations/387_b.sql"]}))
    assert r.returncode == 1 and "OLDER open PR #1" in r.stdout, r.stdout + r.stderr


def test_a_free_number_passes(new_repo) -> None:
    repo = _committed(new_repo, MINE)
    r = again(MOD, repo, *_replay(repo, {"number": 1, "head": "older", "added": ["migrations/388_b.sql"]}))
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_number_already_on_the_base_fires(new_repo) -> None:
    repo = _committed(new_repo, {"migrations/387_z.sql": "SELECT 1;\n"})
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    write(repo / "migrations/387_a.sql", "SELECT 2;\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "mine")
    write(repo / "prs.json", "[]")
    r = again(MOD, repo, "--base", base, "--head", "HEAD", "--prs-json", "prs.json")
    assert r.returncode == 1 and "already on the base as migrations/387_z.sql" in r.stdout, r.stdout + r.stderr


def test_a_custom_dir_and_pattern_are_read(new_repo) -> None:
    repo = _committed(new_repo, {"db/V0387__a.sql": "SELECT 1;\n"})
    args = _replay(repo, {"number": 1, "head": "older", "added": ["db/V0387__b.sql"]})
    r = again(MOD, repo, *args, "--dir", "db", "--pattern", r"V(\d+)__[^/]+\.sql")
    assert r.returncode == 1 and "OLDER open PR #1" in r.stdout, r.stdout + r.stderr


def test_a_missing_base_an_unreadable_list_and_no_repo_are_2(new_repo, monkeypatch) -> None:
    repo = _committed(new_repo, MINE)
    write(repo / "prs.json", "[]")
    assert again(MOD, repo, "--base", "no-such-ref", "--prs-json", "prs.json").returncode == 2
    write(repo / "prs.json", "{not json")
    assert again(MOD, repo, "--base", EMPTY_TREE, "--prs-json", "prs.json").returncode == 2
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    r = again(MOD, repo, "--base", EMPTY_TREE, "--repo", "")
    assert r.returncode == 2 and "no --repo" in r.stderr, r.stdout + r.stderr


def test_a_dir_or_pattern_that_matches_nothing_is_2(new_repo) -> None:
    repo = _committed(new_repo, MINE)
    args = _replay(repo, {"number": 1, "head": "older", "added": ["migrations/387_b.sql"]})
    for wrong in (("--dir", "migration"), ("--dir", "."), ("--pattern", r"V(\d+)__[^/]+\.sql")):
        r = again(MOD, repo, *args, *wrong)
        assert r.returncode == 2 and "rules on nothing" in r.stderr, (wrong, r.stdout + r.stderr)


def test_a_branch_adding_nothing_over_a_matching_base_passes(new_repo) -> None:
    repo = _committed(new_repo, MINE)
    write(repo / "prs.json", "[]")
    r = again(MOD, repo, "--base", "HEAD", "--prs-json", "prs.json")
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_pattern_without_a_group_is_2(new_repo) -> None:
    repo = _committed(new_repo, MINE)
    assert again(MOD, repo, "--pattern", r"\d+_a\.sql").returncode == 2
