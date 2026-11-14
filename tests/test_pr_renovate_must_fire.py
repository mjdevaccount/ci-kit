"""Renovate's automerge PRs JOIN the PR queue with no hand step (ci_kit/pr_renovate.py), and a PR
the preset holds -- the database group, a major, `fails-green`, `autoland-off`, a hand disarm --
never does. Both directions, each through the real pass (pr_autoupdate.run + the queue hold).

THE CASE (trade-data 2026-11-14): #3103 (Renovate lock file maintenance, 17:17Z) read `queue/position`
pending -- "not in the PR queue ... arm auto-merge to join it" -- until a session armed it by hand;
#3081, #3092, #3093 needed the same. Mutation each test dies to is named in it.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from ci_kit import pr_autoupdate, pr_hold, pr_list, pr_renovate

BOT = {"__typename": "Bot", "login": "renovate"}
ON = "This PR contains the following updates:\n\n🚦 **Automerge**: Enabled.\n\n♻ **Rebasing**: ..."
OFF = "🚦 **Automerge**: Disabled by config. Please merge this manually once you are satisfied.\n"
# upstream release notes quoting the marker inline, AFTER Renovate's own Disabled line
QUOTED = OFF + "\n### Release notes\n> 🚦 **Automerge**: Enabled.\n- **Automerge**: Enabled.\n"
MANUAL = {"reasonCode": "manually_disabled", "createdAt": "2026-11-14T17:20:00Z", "actor": {"login": "mjdevaccount"}}
CLOSED = {"reasonCode": "closed", "createdAt": "2026-11-14T17:20:00Z", "actor": {"login": "mjdevaccount"}}


def _pr(n: int, *, author: dict | None = None, armed: bool = False, merge: str = "BEHIND", labels: tuple[str, ...] = (),
        title: str = "chore(deps): lock file maintenance", **k) -> dict:
    return {"id": f"PR_node{n}", "number": n, "title": title, "url": f"https://x/pull/{n}", "headRefOid": f"{n:040d}",
            "headRefName": f"renovate/b{n}", "isDraft": False, "isCrossRepository": False, "baseRefName": "main",
            "author": BOT if author is None else author, "mergeStateStatus": merge, "mergeable": "MERGEABLE",
            "autoMergeRequest": {"enabledAt": "x"} if armed else None, "labels": [{"name": lb} for lb in labels],
            "statusCheckRollup": [], **k}


# ---- the pure policy ------------------------------------------------------------------------------

def test_an_automerge_renovate_pr_is_armed_must_fire() -> None:
    # dies to: verdict() never returning True (nothing would ever join)
    assert pr_renovate.verdict(_pr(3103), ON, None) == (True, "Renovate's verdict: Automerge Enabled")
    assert pr_renovate.verdict(_pr(3103), ON, CLOSED)[0]          # closed and reopened is not a hold


@pytest.mark.parametrize(("p", "body", "disabled", "why"), [
    (_pr(1), OFF, None, "Automerge: Disabled"),                        # database images, langfuse, a major
    (_pr(1, labels=("deps", "major", "fails-green")), OFF, None, "Disabled"),
    (_pr(1), QUOTED, None, "Disabled"),                                # a quoted Enabled further down
    (_pr(1), "Configure Renovate", None, "no `**Automerge**:` line"),  # onboarding: nothing to read
    (_pr(1, labels=("autoland-off",)), ON, None, "autoland-off"),       # the estate's opt-out label
    (_pr(1, title="chore(deps): x [AUTOLAND-OFF]"), ON, None, "autoland-off"),
    (_pr(1), ON, MANUAL, "turned off by hand"),                        # a session's --disable-auto
    (_pr(1, merge="DIRTY"), ON, None, "conflicts"),
    (_pr(1, mergeable="CONFLICTING", merge="UNKNOWN"), ON, None, "conflicts"),
    (_pr(1, merge="CLEAN"), ON, None, "mergeable as it stands"),       # GitHub would merge on arming
    (_pr(1, merge="UNSTABLE"), ON, None, "mergeable as it stands"),    # ... past a red non-required check
])
def test_a_held_renovate_pr_is_never_armed_must_not_fire(p: dict, body: str, disabled: dict | None, why: str) -> None:
    # dies to: dropping any one guard in verdict()
    arm, said = pr_renovate.verdict(p, body, disabled)
    assert not arm and why in said


def test_only_the_renovate_app_unarmed_same_repo_non_draft_non_train_is_a_candidate() -> None:
    assert pr_renovate.candidate(_pr(1))
    for p in (_pr(1, author={"__typename": "User", "login": "renovate"}),   # a User who took the name
              _pr(1, author={"__typename": "Bot", "login": "dependabot"}),
              _pr(1, author={}), _pr(1, armed=True), _pr(1, isDraft=True), _pr(1, isCrossRepository=True),
              _pr(1, headRefName="train/42")):
        assert not pr_renovate.candidate(p), p


def test_the_pr_list_carries_the_node_id_and_author_the_arm_needs() -> None:
    # dies to: pr_list dropping `id` / `author` (every candidate check would read None: nothing joins)
    assert "id number" in pr_list.QUERY and "author { __typename login }" in pr_list.QUERY
    node = {"id": "PR_x", "number": 1, "author": BOT, "labels": {"nodes": []}, "commits": {"nodes": []}}
    row = pr_list.rows({"data": {"repository": {"pullRequests": {"nodes": [node]}}}})[0]
    assert row["id"] == "PR_x" and row["author"] == BOT


# ---- through the real pass: pr_autoupdate.run with the queue hold on ------------------------------

class World:
    """The GitHub a pass sees. `bodies` = PR -> (body, last AutoMergeDisabledEvent); records writes."""

    def __init__(self, bodies: dict[int, tuple[str, dict | None]], *, refuse_arm: bool = False,
                 detail_fails: bool = False) -> None:
        self.bodies, self.refuse_arm, self.detail_fails, self.writes = bodies, refuse_arm, detail_fails, []

    def read(self, *a: str) -> subprocess.CompletedProcess:
        if a[:2] == ("api", "graphql") and not any(x.startswith("number=") for x in a):
            return subprocess.CompletedProcess(a, 0, "{}", "")      # the PR list (pr_list.rows is the fake)
        if a[:2] == ("api", "graphql"):
            if self.detail_fails:
                raise subprocess.CalledProcessError(1, "gh", "", "HTTP 502")
            n = int(next(x for x in a if x.startswith("number=")).split("=")[1])
            body, ev = self.bodies[n]
            pr = {"body": body, "timelineItems": {"nodes": [ev] if ev else []}}
            return subprocess.CompletedProcess(a, 0, json.dumps({"data": {"repository": {"pullRequest": pr}}}), "")
        path = next(x for x in a if x.startswith("repos/"))
        if path.endswith("/status"):
            return subprocess.CompletedProcess(a, 0, json.dumps({"statuses": []}), "")
        return subprocess.CompletedProcess(a, 0, json.dumps({"default_branch": "main"}), "")

    def write(self, *a: str, **_: object) -> subprocess.CompletedProcess:
        self.writes.append(a)
        if a[:2] == ("api", "graphql") and "enablePullRequestAutoMerge" in " ".join(a):
            if self.refuse_arm:
                return subprocess.CompletedProcess(a, 1, "", "HTTP 403: Resource not accessible by personal access token")
            ok = {"data": {"enablePullRequestAutoMerge": {"pullRequest": {"autoMergeRequest": {"enabledAt": "now"}}}}}
            return subprocess.CompletedProcess(a, 0, json.dumps(ok), "")
        return subprocess.CompletedProcess(a, 0, "", "")

    def armed(self) -> list[str]:
        return [next(x for x in w if x.startswith("id=")) for w in self.writes
                if w[:2] == ("api", "graphql") and "enablePullRequestAutoMerge" in " ".join(w)]

    def posted(self) -> dict[int, tuple[str, str]]:
        out = {}
        for w in self.writes:
            line = " ".join(w)
            if "/statuses/" in line:
                n = int(line.split("/statuses/")[1].split(" ")[0])
                out[n] = (line.split("state=")[1].split(" ")[0], line.split("description=")[1])
        return out


def _run(monkeypatch: pytest.MonkeyPatch, prs: list[dict], world: World, **opts) -> tuple[int, list[str]]:
    monkeypatch.setattr(pr_autoupdate, "_gh_read", world.read)
    monkeypatch.setattr(pr_autoupdate.pr_heal, "write", world.write)
    monkeypatch.setattr(pr_autoupdate.pr_list, "rows", lambda raw: prs)
    monkeypatch.setattr(pr_autoupdate.pr_heal, "assess_armed", lambda *a, **k: ({}, 0))
    monkeypatch.setattr(pr_autoupdate.pr_heal, "act", lambda *a, **k: 0)
    monkeypatch.setattr(pr_autoupdate, "unlabel_resolved", lambda *a, **k: 0)
    monkeypatch.setattr(pr_autoupdate, "_update_one", lambda *a, **k: ("updated", "updated"))
    monkeypatch.setattr(pr_autoupdate, "reviewing", lambda *a: False)
    monkeypatch.setattr(pr_hold, "page", lambda *a: "paged")
    summary: list[str] = []
    rc = pr_autoupdate.run("o/r", summary, [], pr_autoupdate.Options(**{"max_in_flight": 1, "hold": True, **opts}))
    return rc, summary


def test_replay_td3103_an_automerge_renovate_pr_joins_the_queue_with_no_hand_step_must_fire(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # 17:17Z 11-14: #3103 (lock file maintenance, Automerge Enabled) open, unarmed; #3104 a session's
    # armed PR. The pass ARMS #3103 and, in the same pass, the hold gives it `queue/position` success
    # as the lowest-numbered member -- before the change it read pending "not in the PR queue".
    # dies to: run() not calling join(), or join() not marking the row armed for the rest of the pass
    world = World({3103: (ON, None)})
    rc, summary = _run(monkeypatch, [_pr(3103), _pr(3104, author={"__typename": "User", "login": "mjdevaccount"},
                                                     armed=True, headRefName="fix/x")], world)
    assert rc == 0
    assert world.armed() == ["id=PR_node3103"]
    assert world.posted()[3103] == ("success", pr_hold.HEAD)
    assert world.posted()[3104][0] == "pending"
    assert any("#3103: Renovate PR armed" in s for s in summary)
    # the arm is the GraphQL mutation, NEVER `gh pr merge --auto` (which merges a CLEAN/UNSTABLE PR at once)
    assert not [w for w in world.writes if w[:2] == ("pr", "merge")]
    arm = next(w for w in world.writes if "enablePullRequestAutoMerge" in " ".join(w))
    assert "mergeMethod: SQUASH" in " ".join(arm) and f"oid={3103:040d}" in arm


def test_the_database_group_and_every_held_update_stay_out_of_the_queue_must_not_fire(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # #3101 database images (Automerge Disabled), #3102 automerge but `autoland-off`, #3105 automerge but
    # disarmed by a session: none is armed, each reads pending "not in the PR queue", and the head is
    # the human PR #3104. dies to: join() arming on anything but Renovate's own Enabled verdict
    world = World({3101: (OFF, None), 3102: (ON, None), 3105: (ON, MANUAL)})
    prs = [_pr(3101, title="chore(deps): update database images"), _pr(3102, labels=("autoland-off",)),
           _pr(3104, author={"__typename": "User", "login": "mjdevaccount"}, armed=True, headRefName="fix/x"),
           _pr(3105)]
    rc, summary = _run(monkeypatch, prs, world)
    assert rc == 0 and world.armed() == []
    posted = world.posted()
    assert posted[3104] == ("success", pr_hold.HEAD)
    for n in (3101, 3102, 3105):
        assert posted[n][0] == "pending" and "not in the PR queue" in posted[n][1]
        assert any(s.startswith(f"#{n}: Renovate PR left out of the queue") for s in summary)


def test_a_refused_arm_or_an_unreadable_pr_is_a_red_run_never_silent_must_fire(monkeypatch: pytest.MonkeyPatch,
                                                                               capsys: pytest.CaptureFixture) -> None:
    # TELEMETRY: an automerge PR that stays out of the queue must turn the run red (mcp
    # ci-schedule-audit reads the streak). dies to: join() swallowing either failure
    rc, summary = _run(monkeypatch, [_pr(3103)], World({3103: (ON, None)}, refuse_arm=True))
    assert rc == 1 and "Pull requests: Read and write" in capsys.readouterr().out
    assert any("NOT armed" in s for s in summary)
    rc, summary = _run(monkeypatch, [_pr(3103)], World({3103: (ON, None)}, detail_fails=True))
    assert rc == 1 and any("NOT decided" in s for s in summary)


def test_no_queue_no_arm_renovate_merges_itself_there_must_not_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    # a repo without the serial queue keeps Renovate's own merge on EVERY check (the preset's header):
    # arming there would land it on the REQUIRED checks only. dies to: dropping the max_in_flight gate
    world = World({3103: (ON, None)})
    rc, _ = _run(monkeypatch, [_pr(3103)], world, max_in_flight=0, hold=False)
    assert rc == 0 and world.armed() == []
