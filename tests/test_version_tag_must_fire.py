"""ci-version-tag must-fire: a release that ran and is missing -> 1, every bump tagged -> 0,
nothing to read -> 2. The version history is a REAL git repo; only GitHub and the index are faked.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from conftest import git, run_tool, write

from ci_kit import version_tag as vt


def _commit(repo: Path, version: str | None, msg: str, extra: str = "") -> str:
    body = "[project]\nname = 'kit'\n" + (f"version = '{version}'\n" if version else "") + extra
    write(repo / "pyproject.toml", body)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def repo(new_repo, monkeypatch) -> dict[str, str]:
    r = new_repo("kit")
    shas = {
        "1.0.0": _commit(r, "1.0.0", "one"),
        "1.0.1.dev0": _commit(r, "1.0.1.dev0", "dev"),
        "1.0.1": _commit(r, "1.0.1", "patch"),
    }
    _commit(r, "1.0.1", "unrelated edit", extra="# comment\n")
    shas["1.1.0"] = _commit(r, "1.1.0", "minor")
    monkeypatch.chdir(r)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return shas


class Fake:
    """A remote whose state the test sets; records every write."""

    def __init__(self, tags=None, releases=(), indexed=(), runs=None, takes=True) -> None:
        self._tags = dict(tags or {})
        self._releases, self._indexed = set(releases), set(indexed)
        self._runs = runs or {}
        self.takes = takes
        self.writes: list[tuple[str, ...]] = []

    def default_branch(self) -> str:
        return "main"

    def tags(self):
        return dict(self._tags)

    def releases(self):
        return set(self._releases)

    def indexed(self, project):
        return set(self._indexed)

    def newest_run(self, workflow, tag):
        return self._runs.get(tag)

    def create_tag(self, tag, sha):
        self.writes.append(("tag", tag, sha))
        if self.takes:
            self._tags[tag] = sha

    def ref_sha(self, tag):
        return self._tags.get(tag)

    def dispatch(self, workflow, tag):
        self.writes.append(("dispatch", workflow, tag))


ARGS = ["--repo", "o/kit", "--release-workflow", "release.yml", "--github-release", "--pypi", "kit"]


def _all_released(shas, but=()):
    vs = [v for v in shas if vt.is_release(v) and v not in but]
    return dict(tags={f"v{v}": shas[v] for v in vs}, releases={f"v{v}" for v in vs}, indexed=set(vs))


def test_bumps_reads_first_parent_history_first_commit_wins(repo) -> None:
    assert vt.bumps() == [(v, repo[v]) for v in ("1.0.0", "1.0.1.dev0", "1.0.1", "1.1.0")]


def test_untagged_bump_is_tagged_at_its_bump_commit_and_released(repo, capsys) -> None:
    fake = Fake(**_all_released(repo, but=("1.1.0",)))
    assert vt.main(ARGS, remote=fake) == 0
    assert fake.writes == [("tag", "v1.1.0", repo["1.1.0"]), ("dispatch", "release.yml", "v1.1.0")]
    assert "tagged" in capsys.readouterr().out


def test_dev_versions_are_never_tagged(repo) -> None:
    fake = Fake(**_all_released(repo))
    assert vt.main(ARGS, remote=fake) == 0
    assert fake.writes == []


def test_a_release_that_ran_and_is_still_missing_is_exit_1(repo, capsys) -> None:
    state = _all_released(repo)
    state["indexed"].discard("1.0.1")
    fake = Fake(**state, runs={"v1.0.1": ("completed", "failure", "https://x/run/1")})
    assert vt.main(ARGS, remote=fake) == 1
    assert fake.writes == []                       # a failed publish is not re-dispatched blind
    assert "kit on the index missing" in capsys.readouterr().out


def test_missing_release_with_no_run_is_dispatched(repo) -> None:
    state = _all_released(repo)
    state["releases"].discard("v1.0.0")
    fake = Fake(**state)
    assert vt.main(ARGS, remote=fake) == 0
    assert fake.writes == [("dispatch", "release.yml", "v1.0.0")]


def test_missing_release_with_a_run_in_flight_is_pending(repo, capsys) -> None:
    state = _all_released(repo)
    state["releases"].discard("v1.1.0")
    fake = Fake(**state, runs={"v1.1.0": ("in_progress", "", "https://x/run/2")})
    assert vt.main(ARGS, remote=fake) == 0
    assert fake.writes == []
    assert "pending" in capsys.readouterr().out


def test_a_tag_the_remote_did_not_take_is_exit_1(repo) -> None:
    fake = Fake(**_all_released(repo, but=("1.1.0",)), takes=False)
    assert vt.main(ARGS, remote=fake) == 1
    assert ("dispatch", "release.yml", "v1.1.0") not in fake.writes


def test_off_the_default_branch_nothing_is_written(repo, monkeypatch, capsys) -> None:
    monkeypatch.setenv("GITHUB_REF", "refs/pull/7/merge")
    fake = Fake(**_all_released(repo, but=("1.1.0",)))
    assert vt.main(ARGS, remote=fake) == 0
    assert fake.writes == []
    assert "would-tag" in capsys.readouterr().out


def test_since_ignores_history_before_the_floor(repo) -> None:
    fake = Fake(**_all_released(repo, but=("1.0.0", "1.0.1")))   # old versions never released
    assert vt.main([*ARGS, "--since", "1.1.0"], remote=fake) == 0
    assert fake.writes == []
    assert vt.main(ARGS, remote=Fake(**_all_released(repo, but=("1.0.0",)))) == 0  # without: acts


def test_pep440_order() -> None:
    order = ["1.0.0a1", "1.0.0b2", "1.0.0rc1", "1.0", "1.0.0.post1", "1.0.1", "1.10.0"]
    assert sorted(order, key=vt.pep440_key) == order
    assert not vt.is_release("1.0.0.dev0") and not vt.is_release("1.0.0+local")
    with pytest.raises(ValueError):
        vt.pep440_key("1.0.0.dev0")


def test_not_a_checkout_is_exit_2(tmp_path: Path) -> None:
    r = run_tool("version_tag", tmp_path, "--repo", "o/kit")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "could not look" in r.stderr


def test_no_version_anywhere_is_exit_2(new_repo, monkeypatch) -> None:
    r = new_repo("dyn")
    _commit(r, None, "dynamic version")
    monkeypatch.chdir(r)
    assert vt.main(ARGS, remote=Fake()) == 2
