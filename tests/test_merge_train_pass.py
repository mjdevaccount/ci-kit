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
from ci_kit.merge_train import EJECTED, LANDED, RIDING, TRAIN_LABEL, cli, github
from ci_kit.merge_train import git as tgit

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

    def add(self, n: int, head: str, **k) -> None:
        self.prs[n] = {"number": n, "title": f"feat: {n}", "body": f"body {n}", "headRefName": f"pr{n}",
                       "headRefOid": head, "isCrossRepository": False, "baseRefName": "main", "isDraft": False,
                       "mergeable": "MERGEABLE", "mergeStateStatus": "BEHIND", "state": "open",
                       "autoMergeRequest": {"enabledAt": f"2026-11-11T00:00:{n:02d}Z"}, "labels": set(),
                       "statusCheckRollup": GREEN, "createdAt": NOW, "url": f"u/{n}", **k}

    def _gql(self) -> str:
        nodes = [{**{k: v for k, v in p.items() if k not in ("labels", "statusCheckRollup", "state", "body")},
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
        if a[:2] == ("api", "graphql"):
            return self._gql()
        if a[:2] == ("api", "user"):
            return json.dumps({"login": "me", "id": 1})
        if a[0] == "api" and "/actions/workflows/" in a[1]:
            head = a[1].split("head_sha=")[1].split("&")[0]
            n = next((p["number"] for p in self.prs.values() if p["headRefOid"] == head), 0)
            return json.dumps({"workflow_runs": [{"id": 1, "created_at": "x", "head_sha": head, "event": "pull_request",
                                                  "status": "completed", "head_branch": f"pr{n}",
                                                  "conclusion": "success" if self.reviewed_ok else "failure",
                                                  "pull_requests": [{"number": n, "head": {"sha": head}}]}]})
        if a[0] == "api" and "/issues/" in a[1] and a[1].endswith("comments?per_page=100"):
            return "[]"
        if a[0] == "api" and "/commits?per_page=100" in a[1]:
            m = int(a[1].split("/pulls/")[1].split("/")[0])
            head = self.prs[m]["headRefOid"]
            root = git(self.repo.work, "merge-base", self.prs[m]["root"], head)
            shas = git(self.repo.work, "rev-list", "--reverse", f"{root}..{head}").split()
            return json.dumps([tgit.message(self.repo.work, s) for s in shas])
        if a[0] == "api" and "/pulls/" in a[1]:
            p = self.prs[int(a[1].rsplit("/", 1)[1])]
            return json.dumps({"state": p["state"], "title": p["title"], "body": p["body"],
                               "head": {"sha": p["headRefOid"], "ref": p["headRefName"], "repo": {"full_name": "o/r"}}})
        if a[0] == "api" and "/labels/" in a[1]:
            return "{}"
        if a[:2] == ("pr", "list"):
            return json.dumps(self.merged)
        if a[:2] == ("pr", "edit"):
            p = self.prs[int(a[2])]
            (p["labels"].add if a[5] == "--add-label" else p["labels"].discard)(a[6])
            return ""
        if a[:2] == ("pr", "close"):
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
                     labels={TRAIN_LABEL}, statusCheckRollup=[], createdAt=NOW, root=git(self.repo.origin, "rev-parse", "main"))
            return f"https://github.com/o/r/pull/{m}\n"
        return ""  # pr comment, pr merge, label create

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
    assert any(c[:3] == ("pr", "merge", "100") and "--rebase" in c and "--auto" in c for c in fake.calls)
    # the pushed branch is a train the review gate passes
    cons, problems = tgit.verify(repo.origin, repo.main, train["headRefOid"], lambda *a: None, lambda *a: (True, ""))
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
