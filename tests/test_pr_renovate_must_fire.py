"""Renovate's automerge PRs JOIN the PR queue with no hand step (ci_kit/pr_renovate.py), and a PR
the preset holds -- the database group, a major, `fails-green`, `autoland-off`, a hand disarm --
never does. Both directions, each through the real pass (pr_autoupdate.run + the queue hold).

THE CASE (a consumer, 2026-11-14): #3103 (Renovate lock file maintenance, 17:17Z) read `queue/position`
pending -- "not in the PR queue ... arm auto-merge to join it" -- until a session armed it by hand;
#3081, #3092, #3093 needed the same. Mutation each test dies to is named in it.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from ci_kit import pr_autoupdate, pr_hold, pr_list, pr_renovate

BOT = {"__typename": "Bot", "login": "renovate"}

def _body(verdict: str, notes: str = "- fix: a bug") -> str:
    """Renovate's real layout (a live consumer PR; default prBodyTemplate): table, RELEASE NOTES (upstream
    text), THEN Renovate's own `### Configuration` block with its `🚦 **Automerge**:` line."""
    return ("This PR contains the following updates:\n\n| Package | Change |\n|---|---|\n| x | 1 -> 2 |\n\n---\n\n"
            f"### Release Notes\n\n<details>\n\n{notes}\n\n</details>\n\n---\n\n### Configuration\n\n"
            "📅 **Schedule**: (in timezone America/New_York)\n\n- Branch creation\n  - Only on Saturday\n"
            "- Automerge\n  - At any time (no schedule defined)\n\n"
            f"🚦 **Automerge**: {verdict}\n\n♻ **Rebasing**: Whenever PR becomes conflicted.\n\n"
            "🔕 **Ignore**: Close this PR and you won't be reminded about this update again.\n\n---\n\n"
            " - [ ] <!-- rebase-check -->If you want to rebase/retry this PR, check this box\n")


ON = _body("Enabled.")
OFF = _body("Disabled by config. Please merge this manually once you are satisfied.")
# UPSTREAM release notes (written by the package's maintainer, BEFORE Renovate's own block) carrying the
# marker, a fake config heading and all: a first-match read armed a held database update on it (review)
QUOTED = _body("Disabled by config. Please merge this manually once you are satisfied.",
               notes="- **Automerge**: Enabled.\n> 🚦 **Automerge**: Enabled.\n### Configuration\n🚦 **Automerge**: Enabled.")
MANUAL = {"reasonCode": "manually_disabled", "createdAt": "2026-11-14T17:20:00Z", "actor": {"login": "octocat"}}
CLOSED = {"reasonCode": "closed", "createdAt": "2026-11-14T17:20:00Z", "actor": {"login": "octocat"}}


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
    (_pr(1), QUOTED, None, "Disabled"),                                # upstream notes say Enabled
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


def test_only_renovates_own_config_block_is_read_must_fire() -> None:
    # dies to: reading the FIRST `**Automerge**:` line (the upstream notes precede the config block)
    assert pr_renovate.automerge_verdict(QUOTED).startswith("Disabled")
    assert pr_renovate.automerge_verdict(ON) == "Enabled."
    assert pr_renovate.automerge_verdict("🚦 **Automerge**: Enabled.") is None   # no config block: unreadable


def test_a_refusal_for_a_state_read_too_early_is_a_race_not_a_red() -> None:
    assert pr_renovate.raced("GraphQL: Pull request Pull request is in clean status (enablePullRequestAutoMerge)")
    assert pr_renovate.raced("GraphQL: Head sha didn't match expected head sha")
    assert not pr_renovate.raced("HTTP 403: Resource not accessible by personal access token")


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
                 detail_fails: bool = False, refuse_patch: bool = False, land: str = "ok",
                 running: dict[str, list[str]] | None = None, behind: dict[str, int] | None = None) -> None:
        self.bodies, self.refuse_arm, self.detail_fails, self.writes = bodies, refuse_arm, detail_fails, []
        self.refuse_patch, self.inputs = refuse_patch, {}
        # land: "ok" | "refused" | "raced" | "merged" (GitHub's answer to mergePullRequest); running: head ->
        # its unfinished runs; behind: head -> commits its base has that it lacks (compare's behind_by)
        self.land, self.running, self.behind, self.updates = land, running or {}, behind or {}, []

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
        if "/actions/runs?head_sha=" in path:  # pr_renovate._runs_done: the --jq names of unfinished runs
            oid = path.split("head_sha=")[1].split("&")[0]
            return subprocess.CompletedProcess(a, 0, json.dumps(self.running.get(oid, [])), "")
        if "/compare/" in path:  # pr_renovate._behind: the --jq .behind_by
            return subprocess.CompletedProcess(a, 0, str(self.behind.get(path.rsplit("...", 1)[1], 0)), "")
        if path.endswith("/status"):
            return subprocess.CompletedProcess(a, 0, json.dumps({"statuses": []}), "")
        return subprocess.CompletedProcess(a, 0, json.dumps({"default_branch": "main"}), "")

    def write(self, *a: str, input_text: str | None = None) -> subprocess.CompletedProcess:
        self.writes.append(a)
        if input_text is not None:
            self.inputs[len(self.writes) - 1] = input_text
        if a[:2] == ("api", "graphql") and "enablePullRequestAutoMerge" in " ".join(a):
            if self.refuse_arm:
                return subprocess.CompletedProcess(a, 1, "", "HTTP 403: Resource not accessible by personal access token")
            ok = {"data": {"enablePullRequestAutoMerge": {"pullRequest": {"autoMergeRequest": {"enabledAt": "now"}}}}}
            return subprocess.CompletedProcess(a, 0, json.dumps(ok), "")
        if a[:2] == ("api", "graphql") and "mergePullRequest" in " ".join(a):
            if self.land == "refused":
                return subprocess.CompletedProcess(a, 1, "", "GraphQL: Repository rule violations found (mergePullRequest)")
            if self.land == "raced":
                # GitHub's verbatim answer when the head moved after the read (reviewer, 2026-11-28)
                return subprocess.CompletedProcess(
                    a, 1, "", "GraphQL: Head branch was modified. Review and try the merge again. (mergePullRequest)")
            if self.land == "merged":
                return subprocess.CompletedProcess(a, 1, "", "GraphQL: Pull Request is already merged (mergePullRequest)")
            ok = {"data": {"mergePullRequest": {"pullRequest": {"state": "MERGED", "merged": True}}}}
            return subprocess.CompletedProcess(a, 0, json.dumps(ok), "")
        if "PATCH" in a and self.refuse_patch:
            return subprocess.CompletedProcess(a, 1, "", "HTTP 403: Resource not accessible by personal access token")
        return subprocess.CompletedProcess(a, 0, "", "")

    def bodies_patched(self) -> dict[int, str]:
        # the body travels on stdin (--input -), never as one argv string (Linux's 128 KiB arg cap)
        return {int(w[3].rsplit("/", 1)[1]): json.loads(self.inputs[i])["body"]
                for i, w in enumerate(self.writes) if w[:3] == ("api", "-X", "PATCH") and "--input" in w}

    def landed(self) -> list[str]:
        return [next(x for x in w if x.startswith("id=")) for w in self.writes
                if w[:2] == ("api", "graphql") and "mergePullRequest" in " ".join(w)]

    def armed(self) -> list[str]:
        return [next(x for x in w if x.startswith("id=")) for w in self.writes
                if w[:2] == ("api", "graphql") and "enablePullRequestAutoMerge" in " ".join(w)]

    def posted(self) -> dict[int, tuple[str, str]]:
        # read each `-f key=value` ARGUMENT, never a joined line: on Actions pr_hold also passes
        # target_url (GITHUB_RUN_ID set), which a split on the joined line read into the description
        out = {}
        for w in self.writes:
            path = next((x for x in w if "/statuses/" in x), None)
            if path:
                field = {x.split("=", 1)[0]: x.split("=", 1)[1] for x in w if "=" in x}
                out[int(path.rsplit("/", 1)[1])] = (field["state"], field["description"])
        return out


def _run(monkeypatch: pytest.MonkeyPatch, prs: list[dict], world: World, **opts) -> tuple[int, list[str]]:
    # the same pass on a laptop and on Actions: the runner's env adds a target_url to every status
    for var in ("GITHUB_RUN_ID", "GITHUB_SERVER_URL", "GITHUB_REPOSITORY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(pr_autoupdate, "_gh_read", world.read)
    monkeypatch.setattr(pr_autoupdate.pr_heal, "write", world.write)
    monkeypatch.setattr(pr_autoupdate.pr_list, "rows", lambda raw: prs)
    monkeypatch.setattr(pr_autoupdate.pr_heal, "assess_armed", lambda *a, **k: ({}, 0))
    monkeypatch.setattr(pr_autoupdate.pr_heal, "act", lambda *a, **k: 0)
    monkeypatch.setattr(pr_autoupdate, "unlabel_resolved", lambda *a, **k: 0)
    monkeypatch.setattr(pr_autoupdate, "_update_one", lambda *a, **k: ("updated", "updated"))
    monkeypatch.setattr(pr_autoupdate, "reviewing", lambda *a: False)
    monkeypatch.setattr(pr_autoupdate, "_put_kind", lambda repo, p: (world.updates.append(p["number"]), ("updated", ""))[1])
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
    rc, summary = _run(monkeypatch, [_pr(3103), _pr(3104, author={"__typename": "User", "login": "octocat"},
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
           _pr(3104, author={"__typename": "User", "login": "octocat"}, armed=True, headRefName="fix/x"),
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


BOX = " - [ ] <!-- rebase-check -->If you want"
TICKED = " - [x] <!-- rebase-check -->If you want"


def test_ticked_ticks_renovates_own_box_once_must_fire() -> None:
    # dies to: an unanchored tick (a box quoted in upstream notes), or re-ticking an asked PR
    quoted = _body("Enabled.", notes=BOX + " (quoted upstream)")
    out = pr_renovate.ticked(quoted)
    assert out is not None and out.count(TICKED) == 1 and out.count(BOX) == 1
    assert out.index(TICKED) > out.index("### Configuration\n\n")
    assert pr_renovate.ticked(out) is None, "an already-asked PR is not asked again"
    assert pr_renovate.ticked("no config block\n" + BOX) is None


def test_a_conflicting_kit_pr_asks_renovate_to_recreate_it_must_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    # The preset's kit rule is rebaseWhen "never" in queue repos (the Renovate<->relock ping-pong), so
    # Renovate never moves a conflicting kit branch by itself and the queue skips an unarmed PR: the
    # pass ticks the PR's rebase box, Renovate recreates the branch on current main, the next pass arms
    # it. dies to: dropping the _ask_rebase call (nothing PATCHed), or arming a DIRTY PR
    world = World({3110: (ON, None), 3111: (OFF, None)})
    rc, summary = _run(monkeypatch, [_pr(3110, merge="DIRTY"), _pr(3111, merge="DIRTY")], world)
    assert rc == 0 and world.armed() == []
    patched = world.bodies_patched()
    assert list(patched) == [3110], "only an automerge PR is asked; a held (Disabled) one is left alone"
    assert TICKED in patched[3110] and BOX not in patched[3110]
    assert any(s.startswith("#3110:") and "rebase box ticked" in s for s in summary)
    # the next pass, Renovate not yet run: the box is ticked, so nothing is written again
    world = World({3110: (patched[3110], None)})
    rc, summary = _run(monkeypatch, [_pr(3110, merge="DIRTY")], world)
    assert rc == 0 and world.bodies_patched() == {} and any("already ticked" in s for s in summary)


def test_a_rebase_box_that_cannot_be_ticked_is_a_red_run_must_fire(monkeypatch: pytest.MonkeyPatch,
                                                                    capsys: pytest.CaptureFixture) -> None:
    # TELEMETRY: unticked, a conflicting never-rebased kit PR sits forever. dies to: swallowing the refusal
    rc, summary = _run(monkeypatch, [_pr(3110, merge="DIRTY")], World({3110: (ON, None)}, refuse_patch=True))
    assert rc == 1 and "rebase box could not be ticked" in capsys.readouterr().out
    assert any("NOT ticked" in s for s in summary)


def test_an_armed_kit_pr_that_went_dirty_is_asked_to_rebase_too_must_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    # THE COMMON CASE (reviewer, 2026-11-14): every automerge Renovate PR is armed on its first pass, and a
    # conflict never disarms it, so a kit PR that goes DIRTY later is armed -- no candidate. With
    # rebaseWhen "never" nothing else moves it. dies to: ticking only unarmed candidates
    world = World({3120: (ON, None), 3121: (ON, None), 3122: (OFF, None), 3123: (ON, None)})
    prs = [_pr(3120, armed=True, merge="DIRTY"), _pr(3121, armed=True, merge="BEHIND"),
           _pr(3122, armed=True, merge="DIRTY"), _pr(3123, armed=True, merge="DIRTY", labels=("autoland-off",))]
    rc, summary = _run(monkeypatch, prs, world)
    assert rc == 0 and world.armed() == []
    assert list(world.bodies_patched()) == [3120], "an armed BEHIND, a Disabled and an opted-out PR are left alone"
    assert any(s.startswith("#3120: armed Renovate PR") and "rebase box ticked" in s for s in summary)


def test_a_crlf_body_still_reads_must_fire() -> None:
    # a body saved from the web UI comes back CRLF; `$` alone missed the heading (reviewer, a live consumer PR)
    crlf = ON.replace("\n", "\r\n")
    assert pr_renovate.automerge_verdict(crlf) == "Enabled."
    assert pr_renovate.ticked(crlf) is not None


# ---- an IDLE queue lands a CLEAN Renovate PR itself (v1.60.0, pr_renovate.land_idle) ---------------
# THE CASE (trade-infra, 2026-11-28): #977 (chainguard minio digest) and #978 (ci-kit v1.59.0), both
# "Automerge: Enabled", CLEAN with every check green, sat under "mergeable as it stands (CLEAN) ... armed
# once main moves" with `queue: 0/1 in flight, 0 to attempt` on every pass (run 46145295455, 12:36Z) until
# a session merged them by hand at 13:08Z. No queue hold in that repo (hold=False); main did not move.
GREEN = [{"__typename": "CheckRun", "name": "gate", "status": "COMPLETED", "conclusion": "SUCCESS"}]
STABILITY = {"__typename": "StatusContext", "context": "renovate/stability-days", "state": "PENDING"}
HUMAN = {"__typename": "User", "login": "octocat"}


def test_replay_ti977_978_an_idle_queue_lands_the_lowest_clean_renovate_pr_must_fire(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # dies to: land_idle not called by run(), join() not marking the CLEAN row, or landing BOTH in one pass
    # (the first merge moves main; the second would merge on a base its checks never saw)
    world = World({977: (ON, None), 978: (ON, None)})
    prs = [_pr(978, merge="CLEAN", statusCheckRollup=GREEN), _pr(977, merge="CLEAN", statusCheckRollup=GREEN)]
    rc, summary = _run(monkeypatch, prs, world, hold=False)
    assert rc == 0, summary
    assert world.landed() == ["id=PR_node977"] and world.armed() == []
    land = next(w for w in world.writes if "mergePullRequest" in " ".join(w))
    assert "mergeMethod: SQUASH" in " ".join(land) and f"oid={977:040d}" in land   # pinned to the head read
    assert any(s.startswith("#977: Renovate PR LANDED") for s in summary)
    assert not [w for w in world.writes if w[:2] == ("pr", "merge")]


@pytest.mark.parametrize(("prs", "bodies", "running", "why"), [
    # UNSTABLE: a non-required check pending (stability-days, the supply-chain age gate) or red
    ([_pr(976, merge="UNSTABLE", statusCheckRollup=[*GREEN, STABILITY])], {976: (ON, None)}, {}, "HELD BY POLICY"),
    # CLEAN by GitHub, but a check here still reads pending (a status GitHub has not folded in yet)
    ([_pr(977, merge="CLEAN", statusCheckRollup=[*GREEN, STABILITY])], {977: (ON, None)}, {}, "HELD BY POLICY"),
    # the preset holds it (python base image, a major): Disabled is never landed, CLEAN or not
    ([_pr(968, merge="CLEAN", statusCheckRollup=GREEN)], {968: (OFF, None)}, {}, "Automerge: Disabled"),
    ([_pr(977, merge="CLEAN", statusCheckRollup=GREEN, labels=("autoland-off",))], {977: (ON, None)}, {},
     "opted out"),
    # a relock (or any workflow) still running on the head would push after the merge
    ([_pr(977, merge="CLEAN", statusCheckRollup=GREEN)], {977: (ON, None)}, {f"{977:040d}": ["relock"]},
     "still running on its head: relock"),
    # the queue is NOT idle: an armed human PR is next in line -- its order wins
    ([_pr(977, merge="CLEAN", statusCheckRollup=GREEN),
      _pr(980, author=HUMAN, armed=True, merge="BEHIND", headRefName="fix/x", statusCheckRollup=GREEN)],
     {977: (ON, None)}, {}, "not landed: #980"),
    # a merge train is open
    ([_pr(977, merge="CLEAN", statusCheckRollup=GREEN), _pr(990, author=HUMAN, headRefName="train/1")],
     {977: (ON, None)}, {}, "merge train #990 is open"),
])
def test_an_unready_held_or_busy_case_is_never_landed_must_not_fire(monkeypatch: pytest.MonkeyPatch, prs: list[dict],
                                                                    bodies: dict, running: dict, why: str) -> None:
    # dies to: landing on UNSTABLE / a pending check / Disabled / autoland-off / a run still on the head, or
    # past a PR the queue is driving or an open train
    world = World(bodies, running=running)
    rc, summary = _run(monkeypatch, prs, world, hold=False)
    assert rc == 0 and world.landed() == [], summary
    assert any(why in s for s in summary), summary


def test_a_pr_held_only_by_stability_days_reads_held_by_policy_and_is_never_armed_must_fire(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # trade-infra#976 (langfuse 4.88.0, 2026-11-28): every check green but `renovate/stability-days` pending.
    # Read as "mergeable as it stands" it looked STUCK (the operator: "Why are we waiting on this shit???"),
    # and once main moved it would have been ARMED -- GitHub then merges on the REQUIRED checks only, past
    # the age gate. dies to: age_held() missing, or the hold sitting after the arm decision
    for merge in ("UNSTABLE", "BEHIND", "BLOCKED"):
        world = World({976: (ON, None)})
        rc, summary = _run(monkeypatch, [_pr(976, merge=merge, statusCheckRollup=[*GREEN, STABILITY])], world,
                           hold=False)
        assert rc == 0 and world.armed() == [] and world.landed() == [], (merge, summary)
        assert any(s.startswith("#976: Renovate PR left out of the queue -- HELD BY POLICY") for s in summary)
    # once the status passes it joins like any other (must-not-fire direction: no permanent hold)
    passed = {**STABILITY, "state": "SUCCESS"}
    assert pr_renovate.verdict(_pr(976, merge="BEHIND", statusCheckRollup=[*GREEN, passed]), ON, None)[0]


def test_a_refused_land_is_a_red_run_a_raced_one_is_not_must_fire(monkeypatch: pytest.MonkeyPatch,
                                                                   capsys: pytest.CaptureFixture) -> None:
    # TELEMETRY: a CLEAN automerge PR that stays unmerged turns the run red (the red streak an outside
    # schedule audit reads); a head/base that moved since the read is the next pass's to decide.
    # dies to: land_idle swallowing the refusal, or reddening a race
    rc, summary = _run(monkeypatch, [_pr(977, merge="CLEAN", statusCheckRollup=GREEN)],
                       World({977: (ON, None)}, land="refused"), hold=False)
    assert rc == 1 and "could not be landed" in capsys.readouterr().out
    assert any("NOT landed" in s for s in summary)
    rc, summary = _run(monkeypatch, [_pr(977, merge="CLEAN", statusCheckRollup=GREEN)],
                       World({977: (ON, None)}, land="raced"), hold=False)
    assert rc == 0 and any("decided afresh next pass" in s for s in summary)


def test_a_raced_or_already_merged_land_is_not_a_red_must_not_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    # dies to: _LAND_RACE dropping "head branch was modified" (GitHub's verbatim wording, which the arm's
    # _RACE never matched) or "already merged" (a session or reconciler merged it between read and write)
    for land in ("raced", "merged"):
        rc, summary = _run(monkeypatch, [_pr(977, merge="CLEAN", statusCheckRollup=GREEN)],
                           World({977: (ON, None)}, land=land), hold=False)
        assert rc == 0 and any("decided afresh next pass" in s for s in summary), (land, summary)


def test_a_clean_head_behind_a_non_strict_base_is_updated_not_landed_must_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    # trade-infra / local-mcp are strict:false: GitHub never says BEHIND there, so CLEAN is no proof the
    # head was tested on today's main. After #977 lands, #978 would merge on a base its checks never saw.
    # dies to: _behind() not read, or a behind head landed anyway
    world = World({978: (ON, None)}, behind={f"{978:040d}": 1})
    rc, summary = _run(monkeypatch, [_pr(978, merge="CLEAN", statusCheckRollup=GREEN)], world, hold=False)
    assert rc == 0 and world.landed() == [] and world.updates == [978], summary
    assert any("1 commit(s) behind its base" in s for s in summary), summary
    # must-not-fire: level with base -> landed, no update
    world = World({978: (ON, None)}, behind={f"{978:040d}": 0})
    rc, summary = _run(monkeypatch, [_pr(978, merge="CLEAN", statusCheckRollup=GREEN)], world, hold=False)
    assert rc == 0 and world.landed() == ["id=PR_node978"] and world.updates == [], summary


def test_idle_ignores_a_dirty_or_failing_armed_pr_but_not_a_waiting_one_must_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    # dies to: a DIRTY armed PR in `update` blocking every land until its author resolves it (for ever),
    # a failing held one blocking, or a held one WAITING its turn (green, BLOCKED on a review) being jumped
    red = [{"__typename": "CheckRun", "name": "gate", "status": "COMPLETED", "conclusion": "FAILURE"}]
    for other, lands in ((dict(merge="DIRTY", statusCheckRollup=GREEN), True),
                         (dict(merge="BLOCKED", statusCheckRollup=red), True),
                         (dict(merge="BLOCKED", statusCheckRollup=GREEN), False)):
        world = World({977: (ON, None)})
        prs = [_pr(977, merge="CLEAN", statusCheckRollup=GREEN),
               _pr(980, author=HUMAN, armed=True, headRefName="fix/x", **other)]
        rc, summary = _run(monkeypatch, prs, world, hold=False)
        assert rc == 0 and (world.landed() == ["id=PR_node977"]) == lands, (other, summary)
        if not lands:
            assert any("armed #980 is held, waiting its turn" in s for s in summary), summary


@pytest.mark.parametrize(("rollup", "required"), [
    ([], ()),                                                                         # no checks at all
    ([{**GREEN[0], "conclusion": "CANCELLED"}], ()),                                  # a cancelled run
    ([{**GREEN[0], "status": "IN_PROGRESS", "conclusion": None}], ()),
    ([{"__typename": "StatusContext", "context": "ci", "state": "PENDING"}, *GREEN], ()),
    (GREEN, ("gate", "build")),                                                       # a required check absent
])
def test_only_a_strictly_green_head_is_landed_must_not_fire(monkeypatch: pytest.MonkeyPatch, rollup: list,
                                                            required: tuple) -> None:
    # dies to: idle_landable trusting pr_queue.check_state, which reads [] and CANCELLED as green
    world = World({977: (ON, None)})
    rc, summary = _run(monkeypatch, [_pr(977, merge="CLEAN", statusCheckRollup=rollup)], world, hold=False,
                       required=required)
    assert world.landed() == [], summary
    assert pr_renovate.idle_landable(_pr(977, merge="CLEAN", statusCheckRollup=GREEN), ("gate",))   # the green direction
