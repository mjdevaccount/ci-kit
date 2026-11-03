"""ci-api-diff: a breaking spec diff -> 1, an additive one -> 0, a diff it cannot read -> 2, driven as a
caller runs it -- from inside a git checkout with a PR branch off main, against the REAL oasdiff
binary (pinned, sha256-verified, cached under CI_KIT_CACHE_DIR), no mock.
"""
from __future__ import annotations

import json

from conftest import git, run_tool, write

from ci_kit.api_diff import FIXTURES

SPEC = "docs/api/openapi.json"


def _pr(new_repo, base: dict | None, head: dict, *, extra: dict[str, str] | None = None):
    """main carries `base` (or no spec at all); branch `pr` carries `head` in the working tree."""
    r = new_repo()
    write(r / "README.md", "x\n")
    if base is not None:
        write(r / SPEC, json.dumps(base))
    git(r, "add", "-A")
    git(r, "commit", "-qm", "base")
    git(r, "checkout", "-qb", "pr")
    write(r / SPEC, json.dumps(head))
    for rel, text in (extra or {}).items():
        write(r / rel, text)
    git(r, "add", "-A")
    git(r, "commit", "-qm", "pr")
    return r


def _run(repo, *args):
    return run_tool("api_diff", repo, "--base-ref", "main", *args)


def test_a_breaking_diff_fires_naming_each_break(new_repo) -> None:
    r = _run(_pr(new_repo, FIXTURES["base"], FIXTURES["breaking"]))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "BREAKING" in r.stdout and "GET /other" in r.stdout and "api-path-removed-without-deprecation" in r.stdout


def test_an_additive_diff_passes_and_lists_what_it_saw(new_repo) -> None:
    r = _run(_pr(new_repo, FIXTURES["base"], FIXTURES["additive"]))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "none breaking" in r.stdout and "GET /new" in r.stdout, r.stdout


def test_the_err_ignore_file_accepts_a_listed_break_only(new_repo) -> None:
    ignore = {"ci/api-breaks.txt": "GET /other api path removed without deprecation\n"}
    r = _run(_pr(new_repo, FIXTURES["base"], FIXTURES["breaking"], extra=ignore), "--err-ignore", "ci/api-breaks.txt")
    # the removed route is accepted; the removed response field on /items is not
    assert r.returncode == 1, r.stdout + r.stderr
    assert "GET /other" not in r.stdout and "GET /items" in r.stdout, r.stdout


def test_a_spec_new_at_the_merge_base_cannot_break(new_repo) -> None:
    r = _run(_pr(new_repo, None, FIXTURES["base"]))
    assert r.returncode == 0 and "NEW at the merge base" in r.stdout, r.stdout + r.stderr


def test_no_merge_base_is_2_not_clean(new_repo) -> None:
    r = run_tool("api_diff", _pr(new_repo, FIXTURES["base"], FIXTURES["breaking"]), "--base-ref", "origin/nope")
    assert r.returncode == 2 and "DID NOT FINISH LOOKING" in r.stdout + r.stderr, r.stdout + r.stderr


def test_an_unparseable_spec_is_2(new_repo) -> None:
    repo = _pr(new_repo, FIXTURES["base"], FIXTURES["base"])
    write(repo / SPEC, '{"openapi":')
    r = _run(repo)
    assert r.returncode == 2 and "DID NOT FINISH LOOKING" in r.stdout + r.stderr, r.stdout + r.stderr


def test_a_missing_binary_is_2(new_repo, monkeypatch) -> None:
    monkeypatch.setenv("CI_KIT_OASDIFF", str(new_repo("elsewhere") / "no-such-oasdiff"))
    r = _run(_pr(new_repo, FIXTURES["base"], FIXTURES["additive"]))
    assert r.returncode == 2 and "DID NOT FINISH LOOKING" in r.stdout + r.stderr, r.stdout + r.stderr


def test_selftest_passes() -> None:
    from ci_kit.api_diff import selftest
    assert selftest() == []
