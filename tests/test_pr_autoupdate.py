"""The updater's pure policy: what an API answer means, and what it does about it."""
from __future__ import annotations

import json
import subprocess

import pytest

import ci_kit.pr_autoupdate as upd
from ci_kit.pr_autoupdate import Options, Outcome, actions_for, classify, page_text, reviewing, tracker_row


def test_classify() -> None:
    assert classify(0, "").kind == "updated"
    assert classify(1, '{"message": "merge conflict between base and head"}').kind == "conflict"
    assert classify(1, '{"message": "There are no new commits on the base branch."}').kind == "current"
    assert classify(1, '{"message": "expected head sha didn\'t match current head ref."}').kind == "current"
    assert classify(1, '{"message": "Bad credentials"}').kind == "error"


def test_conflict_is_paged_once() -> None:
    """The label is the dedup: a PR already labelled is neither relabelled nor paged again."""
    assert actions_for(Outcome("conflict"), labelled=False) == (True, True, False)
    assert actions_for(Outcome("conflict"), labelled=True) == (False, False, False)
    assert actions_for(Outcome("updated"), labelled=True) == (False, False, True)
    assert actions_for(Outcome("error"), labelled=False) == (False, False, False)


def test_page_names_the_calling_repo() -> None:
    pr = {"number": 7, "title": "t"}
    title, body = page_text("example-org/web-app", pr, "", Options())
    assert title == "web-app #7: conflicts with main"
    assert "lockfile" not in body and "tracker" not in body.lower()
    _, body = page_text("example-org/api", pr, "| #7 | row |", Options(tracker=2065, relock=True))
    assert "lockfile" in body and "Tracker #2065: | #7 | row |" in body


def test_no_review_workflow_never_asks_gh() -> None:
    """A repo with no review gate must not query one: `gh run list` on a missing file errors (exit 2)."""
    assert reviewing("o/r", "b", "") is False


def test_the_page_quotes_the_prs_tracker_row_not_a_heading() -> None:
    body = "## #2075 heading" + chr(10) + "| B9 | #2074 | merged |" + chr(10) + "| C2 | #2075 | open, ci.yml |"
    assert tracker_row(body, 2075) == "| C2 | #2075 | open, ci.yml |"
    assert tracker_row(body, 9999) == ""


# ---- run(): the loop, driven through a fake gh (driven through a fake gh)


class _Done:
    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, err


_PR = {"number": 9, "title": "t", "url": "u", "autoMergeRequest": {"x": 1},
       "isCrossRepository": False, "labels": [], "headRefOid": "abc", "headRefName": "b9"}


def gql(prs: list[dict]) -> str:
    """`gh pr list`-shaped rows -> the GraphQL answer ci_kit.pr_list.rows reads back."""
    return json.dumps({"data": {"repository": {"pullRequests": {"nodes": [{
        **{k: v for k, v in p.items() if k not in ("labels", "statusCheckRollup")},
        "labels": {"nodes": p.get("labels") or []},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {"nodes": p.get("statusCheckRollup") or []}}}}]},
    } for p in prs]}}}})


def _fake_gh(calls: list[tuple], *, edit_rc: int | list[int] = 0, running: bool = False, state: str = "OPEN",
             label_exists: bool = True, create_rc: int = 0):
    """`edit_rc` as a list answers successive `pr edit` calls in turn (the last one repeats)."""
    edits = list(edit_rc) if isinstance(edit_rc, list) else [edit_rc]

    def fake(*a: str, check: bool = True, env: dict | None = None) -> _Done:
        calls.append(a)
        if a[:2] == ("api", "graphql"):
            return _Done(0, gql([_PR]))
        if a[:2] == ("pr", "view"):
            return _Done(0, state + chr(10))
        if a[:2] == ("run", "list"):
            return _Done(0, json.dumps([{"databaseId": 1}] if running else []))
        if a[0] == "api" and "/labels/" in a[1]:
            return _Done(0, "{}") if label_exists else _Done(1, "", "HTTP 404: Not Found")
        if a[0] == "api":
            return _Done(1, '{"message":"merge conflict between base and head"}')
        if a[:2] == ("label", "create"):
            return _Done(create_rc)
        if a[:2] == ("pr", "edit"):
            return _Done(edits.pop(0) if len(edits) > 1 else edits[0])
        return _Done(0, "| B | #9 | open |")
    return fake


def _creates(calls: list[tuple]) -> list[tuple]:
    return [c for c in calls if c[:2] == ("label", "create")]


def test_a_missing_label_is_created_and_the_add_retried_once(monkeypatch: pytest.MonkeyPatch,
                                                              capsys: pytest.CaptureFixture[str]) -> None:
    # live 2026-10-30: home-host had no `conflict` label -> six red runs, armed PRs behind main
    calls: list[tuple] = []
    monkeypatch.setattr(upd, "_gh", _fake_gh(calls, edit_rc=[1, 0], label_exists=False))
    pages: list[str] = []
    monkeypatch.setattr(upd, "page", lambda *a: pages.append(a[0]) or "published")
    summary: list[str] = []
    relock: list[int] = []
    assert upd.run("o/r", summary, relock, Options()) == 0
    assert relock == [9] and len(pages) == 1
    create = _creates(calls)
    assert len(create) == 1 and create[0][2] == "conflict" and "o/r" in create[0]
    assert "--color" in create[0] and "d93f0b" in create[0]
    assert sum(c[:2] == ("pr", "edit") for c in calls) == 2
    assert "::notice::o/r: created the missing `conflict` label" in capsys.readouterr().out
    assert summary[-1].startswith("#9: conflict") and "`conflict` label created" in summary[-1]


def test_a_label_that_cannot_be_created_is_an_error_with_no_page(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(upd, "_gh", _fake_gh(calls, edit_rc=1, label_exists=False, create_rc=1))
    monkeypatch.setattr(upd, "page", lambda *a: pytest.fail("paged without the dedup label"))
    relock: list[int] = []
    assert upd.run("o/r", [], relock, Options()) == 1
    assert relock == [] and len(_creates(calls)) == 1
    assert sum(c[:2] == ("pr", "edit") for c in calls) == 1  # no retry once the create failed


def test_an_add_that_succeeds_never_looks_up_or_creates_the_label(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(upd, "_gh", _fake_gh(calls, label_exists=False))
    monkeypatch.setattr(upd, "page", lambda *a: "published")
    assert upd.run("o/r", [], [], Options()) == 0
    assert _creates(calls) == [] and not any(c[0] == "api" and "/labels/" in c[1] for c in calls)


def test_an_existing_label_is_not_recreated_when_the_add_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # the label exists, so the failure is something else: one retry, no create, then the error path
    calls: list[tuple] = []
    monkeypatch.setattr(upd, "_gh", _fake_gh(calls, edit_rc=1, label_exists=True))
    monkeypatch.setattr(upd, "page", lambda *a: pytest.fail("paged without the dedup label"))
    assert upd.run("o/r", [], [], Options()) == 1
    assert _creates(calls) == [] and sum(c[:2] == ("pr", "edit") for c in calls) == 2


def test_a_label_that_cannot_be_added_is_an_error_with_no_page_and_no_relock(monkeypatch: pytest.MonkeyPatch) -> None:
    pages: list[str] = []
    monkeypatch.setattr(upd, "_gh", _fake_gh([], edit_rc=1))
    monkeypatch.setattr(upd, "page", lambda *a: pages.append(a[0]) or "published")
    relock: list[int] = []
    assert upd.run("o/r", [], relock, Options()) == 1
    assert pages == [] and relock == []


def test_a_new_conflict_is_paged_once_and_queued_for_relock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upd, "_gh", _fake_gh([]))
    pages: list[str] = []
    monkeypatch.setattr(upd, "page", lambda *a: pages.append(a[1]) or "published")
    relock: list[int] = []
    assert upd.run("o/r", [], relock, Options(tracker=2065)) == 0
    assert relock == [9] and len(pages) == 1 and "| B | #9 | open |" in pages[0]


def test_a_pr_merged_during_the_update_is_skipped_not_labelled_or_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    # live proof 2026-09-26: a PR merged mid-run, the PUT said "merge conflict", the run went red
    calls: list[tuple] = []
    monkeypatch.setattr(upd, "_gh", _fake_gh(calls, state="MERGED"))
    monkeypatch.setattr(upd, "page", lambda *a: pytest.fail("paged a merged PR"))
    summary: list[str] = []
    relock: list[int] = []
    assert upd.run("o/r", summary, relock, Options()) == 0
    assert relock == [] and not any(c[:2] == ("pr", "edit") for c in calls)
    assert summary[-1] == "#9: merged during the update, skipped"


def test_a_pr_mid_review_is_not_updated(monkeypatch: pytest.MonkeyPatch) -> None:
    # an update cancels the in-flight, unrecorded review, so a PR under a busy main would never get a verdict
    calls: list[tuple] = []
    monkeypatch.setattr(upd, "_gh", _fake_gh(calls, running=True))
    summary: list[str] = []
    assert upd.run("o/r", summary, [], Options(review_workflow="review-gate.yml")) == 0
    assert not any(c[:3] == ("api", "-X", "PUT") for c in calls)
    assert "skipped, review gate running" in summary[-1]


def test_without_a_review_workflow_a_running_review_is_not_consulted(monkeypatch: pytest.MonkeyPatch) -> None:
    # the other direction: no review gate configured, the update proceeds (and hits the fake conflict)
    calls: list[tuple] = []
    monkeypatch.setattr(upd, "_gh", _fake_gh(calls, running=True))
    monkeypatch.setattr(upd, "page", lambda *a: "published")
    assert upd.run("o/r", [], [], Options()) == 0
    assert any(c[:3] == ("api", "-X", "PUT") for c in calls) and not any(c[:2] == ("run", "list") for c in calls)


def test_a_failed_gh_call_names_gh_stderr_in_the_summary(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # 2026-10-24: the run said only "returned non-zero exit status 1"; the cause lives in stderr
    def boom(*a: object, **k: object) -> None:
        raise subprocess.CalledProcessError(1, "gh pr list", "", "GraphQL: Resource not accessible by personal access token")
    monkeypatch.setattr(upd, "run", boom)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert upd.main(["--repo", "o/r"]) == 2
    assert "gh said: GraphQL: Resource not accessible by personal access token" in capsys.readouterr().out


def test_the_pr_list_reads_with_the_read_token_and_falls_back_to_the_push_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # 2026-10-24: a fine-grained PAT is refused the check runs in statusCheckRollup
    seen: list[str | None] = []

    def fake(args: list[str], check: bool, capture_output: bool, text: bool, env: dict[str, str] | None):
        seen.append((env or {}).get("GH_TOKEN"))
        rc = 1 if env is not None else 0
        if rc and check:
            raise subprocess.CalledProcessError(rc, args)
        return subprocess.CompletedProcess(args, rc, "[]", "refused" if rc else "")

    monkeypatch.setattr(upd.subprocess, "run", fake)
    monkeypatch.setenv("CI_KIT_READ_TOKEN", "ghs_read")
    assert upd._gh_read("pr", "list").stdout == "[]"
    assert seen == ["ghs_read", None]
    seen.clear()
    monkeypatch.delenv("CI_KIT_READ_TOKEN")
    upd._gh_read("pr", "list")
    assert seen == [None]
