# ruff: noqa: F811 -- every test takes the imported `world` fixture by name
"""A train the pass closes takes its unfinished workflow runs with it (ops.close_train).

THE CASE (trade-data 2026-11-12): train #2998 was closed at 02:24:40Z (recomposed as #2999, main
had moved), but its CI run 43140976318 kept unit-testing on the ONE heavy runner for ~25 min while
real PRs queued behind it, until a human cancelled it. Closing a PR and deleting its branch cancels
nothing. Every close path goes through close_train; a cancel GitHub refuses turns the pass red.
"""
from __future__ import annotations

import pytest
from _trainrepo import Repo, edit
from test_merge_train_pass import GREEN, FakeGH, _opts, _riders, world  # noqa: F401 -- `world` is the shared fixture

from ci_kit.merge_train import cli, ops

TRAIN = 100


def _train_with_runs(repo: Repo, fake: FakeGH, riders: int = 2) -> str:
    """Compose train #100 and give its branch the runs a live train has, plus runs to leave alone."""
    _riders(repo, fake, riders)
    assert cli.run(_opts(), cli.Pass()) == 0
    branch = fake.prs[TRAIN]["headRefName"]
    fake.runs = [
        {"id": 501, "status": "in_progress", "head_branch": branch, "path": ".github/workflows/ci.yml"},
        {"id": 502, "status": "queued", "head_branch": branch, "path": ".github/workflows/review-gate.yml"},
        {"id": 503, "status": "completed", "head_branch": branch, "path": ".github/workflows/ci.yml"},
        # a queued merge-train pass is never dropped, even when GitHub files it under the train branch
        {"id": 504, "status": "queued", "head_branch": branch, "path": ".github/workflows/merge-train.yml@refs/heads/main"},
        # an OPEN PR's run is never touched
        {"id": 505, "status": "in_progress", "head_branch": "pr1", "path": ".github/workflows/ci.yml"},
    ]
    return branch


def _cancelled(fake: FakeGH) -> set[int]:
    return {int(c[3].split("/actions/runs/")[1].split("/")[0]) for c in fake.calls
            if c[:3] == ("api", "-X", "POST") and c[3].endswith("/cancel")}


def _stale(repo: Repo, fake: FakeGH) -> None:
    repo.advance_main({"c.txt": edit(3, "moved")})


def _red(repo: Repo, fake: FakeGH) -> None:
    fake.prs[TRAIN]["statusCheckRollup"] = [{**GREEN[0], "conclusion": "FAILURE"}, GREEN[1]]


@pytest.mark.parametrize("close_it", [_stale, _red], ids=["stale", "red"])
def test_closing_a_train_cancels_its_unfinished_runs_must_fire(world: tuple[Repo, FakeGH], close_it) -> None:
    repo, fake = world
    branch = _train_with_runs(repo, fake)
    close_it(repo, fake)
    p = cli.Pass()
    assert cli.run(_opts(), p) == 0, p.summary
    assert fake.prs[TRAIN]["state"] == "closed"
    assert _cancelled(fake) == {501, 502}
    assert f"{branch}: cancelled 2 unfinished run(s): 501, 502" in p.summary


def test_a_superseded_train_cancels_its_runs(world: tuple[Repo, FakeGH]) -> None:
    # two trains this workflow composed are open: the older one is closed as superseded
    repo, fake = world
    old = _train_with_runs(repo, fake)
    fake.prs[TRAIN]["state"] = "hidden"            # out of the open list while a second train composes
    for n in (1, 2):
        fake.prs[n]["labels"].clear()
    assert cli.run(_opts(), cli.Pass()) == 0 and fake.prs[TRAIN + 1]["state"] == "open"
    fake.prs[TRAIN]["state"] = "open"
    p = cli.Pass()
    cli.run(_opts(), p)
    assert fake.prs[TRAIN]["state"] == "closed" and fake.prs[TRAIN + 1]["state"] == "open"
    assert _cancelled(fake) == {501, 502} and f"{old}: cancelled 2 unfinished run(s): 501, 502" in p.summary


def test_a_cancel_github_refuses_turns_the_pass_red_must_fire(world: tuple[Repo, FakeGH]) -> None:
    repo, fake = world
    _train_with_runs(repo, fake)
    fake.cancel_fail = {501: "gh: Resource not accessible by personal access token (HTTP 403)",
                        502: "gh: Cannot cancel a workflow run that is completed. (HTTP 409)"}  # finished meanwhile
    _stale(repo, fake)
    p = cli.Pass()
    assert cli.run(_opts(), p) == 1
    errors = [s for s in p.summary if s.startswith("ERROR:")]
    assert len(errors) == 1 and "cancel run 501" in errors[0] and "HTTP 403" in errors[0]
    assert fake.prs[TRAIN]["state"] == "closed"


def test_a_train_whose_close_failed_keeps_its_runs_must_fire(world: tuple[Repo, FakeGH]) -> None:
    # never cancel the runs of an OPEN PR: a close that failed leaves the train open, retried next pass
    repo, fake = world
    _train_with_runs(repo, fake)
    fake.close_fail = {TRAIN}
    _stale(repo, fake)
    p = cli.Pass()
    assert cli.run(_opts(), p) == 1
    assert fake.prs[TRAIN]["state"] == "open" and not _cancelled(fake)


def test_cancel_refuses_a_branch_that_is_not_a_train_must_fire(world: tuple[Repo, FakeGH]) -> None:
    _, fake = world
    fake.runs = [{"id": 9, "status": "in_progress", "head_branch": "pr1", "path": "ci.yml"}]
    p = cli.Pass()
    assert ops.cancel_runs(_opts(), p, "pr1") == [] and p.errors == 1 and not _cancelled(fake)


def test_a_failed_runs_listing_is_reported_not_silent(world: tuple[Repo, FakeGH], monkeypatch) -> None:
    _, fake = world
    real = fake._answer

    def refuse(a, stdin):
        return (1, "gh: API rate limit exceeded (HTTP 403)") if a[0] == "api" and "/actions/runs?" in a[1] else real(a, stdin)
    monkeypatch.setattr(fake, "_answer", refuse)
    p = cli.Pass()
    assert ops.cancel_runs(_opts(), p, "train/77") == [] and p.errors == 1
    assert "list the runs on train/77" in p.summary[0] and "rate limit" in p.summary[0]


def test_dry_run_cancels_nothing(world: tuple[Repo, FakeGH]) -> None:
    _, fake = world
    p = cli.Pass()
    assert ops.cancel_runs(_opts(dry_run=True), p, "train/77") == [] and not fake.calls
    assert p.summary == ["(dry run) would cancel unfinished runs on train/77"]
