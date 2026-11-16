"""`update-held` OFF (the default) = exactly what main's updater does: a differential pin.

WHY. The held pass is opt-in; a caller that never sets it must see no change at all -- not one gh
call, summary line, exit code or output more or less. Each scenario below drives `main()` end to
end through ONE fake `subprocess.run` (every gh call any module makes lands in the record) and
compares against the record main's own `ci_kit/pr_autoupdate.py` produced on the same input,
PINNED in tests/fixtures/pr_autoupdate_off_main.json (recorded from `git show origin/main:ci_kit/pr_autoupdate.py` before this
change; a pin rather than a live `git show` so a shallow checkout cannot skip it).

The ONE normalised difference: the PR-list GraphQL query text, which gained `committedDate` (the
held pass's young-head guard reads it from the same call -- no call added). Its args are pinned
with the query replaced by `<QUERY>`, and `test_the_query_differs_from_mains_only_by_committed_date`
pins that the field is the whole delta.
"""
from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

import ci_kit.pr_autoupdate as upd
from ci_kit import pr_list

MAIN_QUERY_MINUS = "commit { statusCheckRollup"  # main's text where this change inserted `committedDate `


class _Done:
    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, err


def _pr(n: int, armed: bool, state: str = "BEHIND") -> dict:
    return {"id": f"P{n}", "number": n, "title": "t", "url": f"u{n}", "headRefOid": "a" * 40, "headRefName": f"b{n}",
            "isCrossRepository": False, "isDraft": False, "mergeStateStatus": state, "mergeable": "MERGEABLE",
            "baseRefName": "main", "createdAt": "2026-11-01T00:00:00Z",
            "author": {"__typename": "User", "login": "someone"},
            "autoMergeRequest": {"enabledAt": "2026-11-01T00:00:00Z"} if armed else None, "body": ""}


def _gql(prs: list[dict]) -> str:
    return json.dumps({"data": {"repository": {"pullRequests": {"nodes": [{
        **p, "labels": {"nodes": []},
        "commits": {"nodes": [{"commit": {"committedDate": "2026-11-01T00:00:00Z",
                                          "statusCheckRollup": {"contexts": {"nodes": []}}}}]},
    } for p in prs]}}}})


_API = {  # REST reads by path fragment, most specific first; anything else answers "[]"
    "/protection/required_status_checks": json.dumps({"strict": True, "contexts": []}),
    "/actions/workflows": json.dumps({"total_count": 0, "workflows": []}),
    "/actions/runs": json.dumps({"total_count": 0, "workflow_runs": []}),
    "/rules/branches/": "[]",
    "/compare/": json.dumps({"behind_by": 1, "ahead_by": 0}),
}
_API_REPO = json.dumps({"default_branch": "main"})

SCENARIOS: dict[str, tuple[list[str], list[dict], tuple[int, str]]] = {
    # name: (argv extras, open PRs, the PUT's answer)
    "no-queue": ([], [_pr(1, True), _pr(2, False)], (0, "")),
    "queue-of-one": (["--max-in-flight", "1"], [_pr(1, True), _pr(2, True), _pr(3, False)], (0, "")),
    "armed-conflict": ([], [_pr(4, True, "DIRTY"), _pr(5, False)], (1, '{"message": "merge conflict"}')),
}


def record(mod, scenario: str, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys) -> dict:
    """Drive `mod.main` through one scenario: {calls, rc, stdout, output}. Shared with the pin's recorder."""
    argv, prs, put = SCENARIOS[scenario]
    calls: list[list[str]] = []

    def fake(cmd, *a, **k) -> _Done:
        args = [("query=<QUERY>" if str(x).startswith("query=") else str(x)) for x in cmd]
        calls.append(args)
        if args[1:3] == ["api", "graphql"]:
            return _Done(0, _gql(prs))
        if args[1:4] == ["api", "-X", "PUT"]:
            return _Done(*put)
        if args[1:3] == ["pr", "view"]:
            return _Done(0, "OPEN\n")
        path = args[2] if len(args) > 2 and args[1] == "api" else ""
        if path == "repos/o/r":
            return _Done(0, _API_REPO)
        for part, body in _API.items():
            if part in path:
                return _Done(0, body)
        return _Done(0, "[]")
    for k in ("CI_KIT_UPDATE_HELD", "NTFY_URL", "CI_KIT_READ_TOKEN", "CI_KIT_HEAL", "CI_KIT_QUEUE_HOLD",
              "CI_KIT_TRAIN_WORKFLOW", "CI_KIT_SCARCE_POOLS", "CI_KIT_REQUIRED_CHECKS", "CI_KIT_BASE_RETRY_CAP",
              "CI_KIT_BASE_RETRY_BOT_AUTHORS", "CI_KIT_BASE_RETRY_MAX_PER_PASS", "CI_KIT_CANCEL_DOOMED"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CI_KIT_EVENT", "schedule")
    out, step = tmp_path / f"{scenario}.out", tmp_path / f"{scenario}.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step))
    monkeypatch.setattr(subprocess, "run", fake)
    capsys.readouterr()
    rc = mod.main(["--repo", "o/r", *argv])
    return {"calls": calls, "rc": rc, "stdout": capsys.readouterr().out, "output": out.read_text(encoding="utf-8")}


_FIXTURE = json.loads((pathlib.Path(__file__).parent / "fixtures" / "pr_autoupdate_off_main.json").read_text(encoding="utf-8"))
PINNED: dict[str, dict] = _FIXTURE["pinned"]
MAIN_QUERY: str = _FIXTURE["main_query"]


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_off_is_identical_to_main(scenario: str, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys) -> None:
    got = record(upd, scenario, monkeypatch, tmp_path, capsys)
    want = PINNED[scenario]
    assert got["calls"] == want["calls"]                 # every gh call, in order, same args
    assert got["stdout"] == want["stdout"]               # the summary (also the step summary)
    assert got["rc"] == want["rc"]                       # the exit code
    assert got["output"] == want["output"]               # relock= / heal= outputs


def test_the_query_differs_from_mains_only_by_committed_date() -> None:
    assert pr_list.QUERY.replace("commit { committedDate statusCheckRollup", MAIN_QUERY_MINUS) == MAIN_QUERY
