"""The heal ladder's policy (ci_kit/pr_ladder.py) and its seat in the updater pass -- both directions.

MUST FIRE: an armed judgment red or conflict is dispatched ONCE per head, recorded before it is
handed on; tier 3 rides along within its per-pass and per-PR budgets; a dispatch that never reported
is paged once; a spent PR is paged once; a conflicted Renovate PR gets its rebase box ticked.
MUST NOT FIRE: an unarmed, `autoland-off`, draft, fork or database-group PR is never the ladder's;
tier 3 never runs on a Renovate MAJOR, an `autoland-off` PR or an unarmed one; a judgment red never
gets a machinery rerun with the ladder on; a head already dispatched is not dispatched again.
"""
from __future__ import annotations

import datetime as dt
import json

import ci_kit.pr_autoupdate as upd
from ci_kit import pr_fix, pr_heal, pr_ladder, pr_list
from ci_kit.pr_ladder import Candidate, Config

NOW = dt.datetime(2026, 11, 14, 18, 0, tzinfo=dt.UTC)
H1, H2 = "a" * 40, "b" * 40
ON = Config(on=True)


class _Done:
    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, err


def pr(n: int = 7, head: str = H1, **kw) -> dict:
    base = {"number": n, "headRefOid": head, "headRefName": "feat/x", "title": "feat: x", "labels": [],
            "autoMergeRequest": {"enabledAt": "2026-11-14T00:00:00Z"}, "author": {"login": "mjdevaccount"},
            "url": f"https://github.com/o/r/pull/{n}"}
    return {**base, **kw}


def renovate(n: int = 9, title: str = "chore(deps): update dependency kucherenko/jscpd to v5.4.3", **kw) -> dict:
    return pr(n, **{"autoMergeRequest": None, "author": {"login": "app/renovate"}, "headRefName": "renovate/jscpd",
                    "title": title, "renovateAutomerge": True, **kw})


def heal_mark(head: str, claude: int = 1, at: str = "2026-11-14T17:50:00Z", mode: str = "red") -> str:
    return pr_ladder.HEAL.format(head=head, mode=mode, claude=claude, at=at)


# ── who is the ladder's ───────────────────────────────────────────────────────────────────────
def test_only_an_armed_same_repo_non_draft_opted_in_pr_is_the_ladders_must_not_fire() -> None:
    assert pr_ladder.blocked(pr()) == ""
    assert pr_ladder.blocked(pr(autoMergeRequest=None)) == "not armed"
    assert "autoland-off" in pr_ladder.blocked(pr(labels=[{"name": "autoland-off"}]))
    assert "autoland-off" in pr_ladder.blocked(pr(title="feat: x [autoland-off]"))
    assert pr_ladder.blocked(pr(isDraft=True)) and pr_ladder.blocked(pr(isCrossRepository=True))
    assert "database" in pr_ladder.blocked(renovate(labels=[{"name": "dba"}]))
    assert pr_ladder.blocked(renovate(renovateAutomerge=False)) == "not armed"   # Renovate will not merge it
    assert pr_ladder.blocked(renovate()) == ""                                     # Renovate merges it itself


def test_tier_3_never_runs_on_a_renovate_major_autoland_off_or_unarmed_pr_must_not_fire() -> None:
    assert pr_ladder.claude_blocked(pr()) == ""
    for p in (renovate(labels=[{"name": "major"}]), renovate(title="chore(deps): update postgres to v19"),
              pr(labels=[{"name": "autoland-off"}]), pr(autoMergeRequest=None)):
        assert pr_ladder.claude_blocked(p)
        action, claude, _ = pr_ladder.decide(p, [], ON, 1, NOW)
        assert not claude
    assert pr_ladder.claude_blocked(renovate()) == ""     # a minor/patch bump: the ladder in full


# ── once per head, the budgets ────────────────────────────────────────────────────────────────
def test_a_head_is_dispatched_once_and_the_record_comes_first_must_fire() -> None:
    writes: list[tuple] = []

    def write(*a: str, input_text: str | None = None) -> _Done:
        writes.append((a, input_text))
        return _Done()
    rows, errs, taken = pr_ladder.dispatch("o/r", [Candidate(pr(), "red", "unit")], ON, lambda r, n: [], write, [],
                                           lambda *a: "sent", NOW)
    assert errs == 0 and taken == {7} and rows == [{"pr": 7, "head": H1, "mode": "red", "claude": True,
                                                    "checks": "unit", "runs": ""}]
    record = writes[0][1]
    assert pr_ladder.marks([record], pr_ladder._HEAL) == [(H1, "red", "1", "2026-11-14T18:00:00Z")]
    again, _, _ = pr_ladder.dispatch("o/r", [Candidate(pr(), "red")], ON, lambda r, n: [record], write, [],
                                     lambda *a: "sent", NOW)
    assert again == []                                                     # quiet at the same head
    new_head, _, _ = pr_ladder.dispatch("o/r", [Candidate(pr(head=H2), "red")], ON, lambda r, n: [record], write, [],
                                        lambda *a: "sent", NOW)
    assert [r["head"] for r in new_head] == [H2]


def test_an_unrecorded_dispatch_is_not_dispatched_must_not_fire() -> None:
    rows, errs, _ = pr_ladder.dispatch("o/r", [Candidate(pr(), "red")], ON, lambda r, n: [],
                                       lambda *a, input_text=None: _Done(1, "", "HTTP 502"), [], lambda *a: "x", NOW)
    assert rows == [] and errs == 1


def test_tier_3_per_pass_slot_defers_the_next_pr_and_per_pr_cap_drops_to_tiers_1_2() -> None:
    rows, _, _ = pr_ladder.dispatch("o/r", [Candidate(pr(7), "red"), Candidate(pr(8), "red")], ON,
                                    lambda r, n: [], lambda *a, input_text=None: _Done(), [], lambda *a: "x", NOW)
    assert [(r["pr"], r["claude"]) for r in rows] == [(7, True)]          # #8 waits for the next pass
    spent = [heal_mark("c" * 40), heal_mark("d" * 40)]
    action, claude, why = pr_ladder.decide(pr(), spent, ON, 1, NOW)
    assert (action, claude) == ("dispatch", False) and "spent 2/2" in why
    assert pr_ladder.decide(pr(), [], Config(on=True, claude_per_pass=0), 1, NOW)[:2] == ("dispatch", False)


def test_a_dispatch_with_no_record_is_paged_once_then_quiet_must_fire() -> None:
    old = heal_mark(H1, at="2026-11-14T15:00:00Z")
    pages: list[str] = []
    writes: list[str] = []
    _, errs, _ = pr_ladder.dispatch("o/r", [Candidate(pr(), "red", "unit")], ON, lambda r, n: [old],
                                    lambda *a, input_text=None: writes.append(input_text) or _Done(), [],
                                    lambda t, b, u: pages.append(t) or "sent", NOW)
    assert len(pages) == 1 and errs == 1                                  # loud: the ladder broke
    assert pr_ladder.decide(pr(), [old, writes[0]], ON, 1, NOW)[0] == "quiet"
    healed = pr_ladder.HEALED.format(head=H1, tier="regen", result="pushed")
    assert pr_ladder.decide(pr(), [old, healed], ON, 1, NOW)[0] == "quiet"  # it reported: nothing to page


def test_a_spent_pr_is_paged_once() -> None:
    marks = [heal_mark(c * 40, claude=0) for c in "cdef01"]
    assert pr_ladder.decide(pr(), marks, ON, 1, NOW)[0] == "capped"
    assert pr_ladder.decide(pr(), [*marks, pr_ladder.PAGED.format(head=H1, why="capped")], ON, 1, NOW)[0] == "quiet"


# ── Renovate ──────────────────────────────────────────────────────────────────────────────────
BODY = "x\n\n - [ ] <!-- rebase-check -->If you want to rebase/retry this PR, check this box\n"


def test_a_conflicted_renovate_pr_gets_its_rebase_box_ticked_once_must_fire() -> None:
    writes: list[tuple] = []

    def write(*a: str, input_text: str | None = None) -> _Done:
        writes.append((a, input_text))
        return _Done(out=BODY) if a[:2] == ("pr", "view") else _Done()
    rows, errs, taken = pr_ladder.dispatch("o/r", [Candidate(renovate(), "conflict")], ON, lambda r, n: [], write, [],
                                           lambda *a: "x", NOW)
    assert rows == [] and errs == 0 and taken == {9}                     # no merge pushed onto Renovate's branch
    record = next(t for a, t in writes if a[:2] == ("pr", "comment"))
    patch = next(t for a, t in writes if a[:1] == ("api",))
    assert "- [x] <!-- rebase-check -->" in json.loads(patch)["body"]
    assert [a[0] for a, _ in writes].index("pr", 1) < [a[0] for a, _ in writes].index("api")  # recorded first
    assert pr_ladder.decide(renovate(), [record], ON, 1, NOW, "conflict")[0] == "quiet"
    pages: list[str] = []
    late = NOW + dt.timedelta(hours=3)
    _, errs, _ = pr_ladder.dispatch("o/r", [Candidate(renovate(), "conflict")], ON, lambda r, n: [record],
                                    lambda *a, input_text=None: _Done(), [], lambda t, b, u: pages.append(t) or "sent",
                                    late)
    assert len(pages) == 1 and errs == 0      # Renovate did not rebuild it: paged, not the pass's own red


def test_a_red_renovate_pr_takes_the_ladder_like_any_other() -> None:
    assert pr_ladder.decide(renovate(), [], ON, 1, NOW, "red")[:2] == ("dispatch", True)
    assert pr_ladder.tick_rebase("no box here") is None
    assert pr_ladder.tick_rebase(BODY.replace("[ ]", "[x]")) is None     # already ticked: nothing to ask


def test_pr_list_carries_renovates_automerge_flag_not_its_body() -> None:
    node = {"number": 1, "body": "🚦 **Automerge**: Enabled.", "labels": {"nodes": []}, "commits": {"nodes": []}}
    rows = pr_list.rows({"data": {"repository": {"pullRequests": {"nodes": [node, {**node, "body": "Disabled"}]}}}})
    assert [r["renovateAutomerge"] for r in rows] == [True, False] and "body" not in rows[0]


def test_the_updater_offers_renovate_prs_only_with_the_ladder_on() -> None:
    prs = [pr(1), renovate(2), renovate(3, renovateAutomerge=False), renovate(4, mergeStateStatus="DIRTY")]
    assert upd.renovate_pool(prs, upd.Options()) == []
    on = upd.Options(ladder=ON)
    assert [p["number"] for p in upd.renovate_pool(prs, on)] == [2, 4]


# ── the judgment / machinery split holds with the ladder on ───────────────────────────────────
def test_with_the_ladder_on_a_judgment_red_is_never_rerun_and_gets_no_judgment_comment_must_not_fire() -> None:
    v = pr_heal.Verdict(7, H1, laddered=True)
    assert v.kind == "judgment"
    heal = {7: (v, [heal_mark(H1)], pr())}
    writes: list[tuple] = []
    pr_heal.act("o/r", heal, set(), 2, lambda *a, input_text=None: writes.append(a) or _Done(), [], frozenset({7}))
    assert not any("rerun" in " ".join(a) for a in writes) and writes == []


def test_ladder_candidates_take_judgment_and_conflicts_never_machinery() -> None:
    mach = pr_heal.Verdict(5, H1, [pr_heal.Red(["ci"], 1, 1, "machinery", "lost runner", "u")])
    judg = pr_heal.Verdict(7, H1, laddered=True)
    heal = {7: (judg, [], pr(7)), 5: (mach, [], pr(5))}
    cands = upd.ladder_candidates(heal, [pr(9)])
    assert sorted((c.pr["number"], c.mode) for c in cands) == [(7, "red"), (9, "conflict")]


# ── tier 3's harness ──────────────────────────────────────────────────────────────────────────
def test_the_fixer_runs_restricted_with_no_bypass_and_no_tokens() -> None:
    a = pr_fix.argv("claude-opus-5-5", ("uv run", "pytest"))
    assert "--restricted" in a and "--strict-mcp-config" in a and "dontAsk" in a
    assert not any("bypass" in x.lower() or "dangerously" in x.lower() for x in a)
    assert "Bash(uv run:*)" in a and "Bash" not in a[a.index("--allowedTools") + 1:a.index("--permission-mode")]
    env = pr_fix.env({"GH_TOKEN": "t", "GITHUB_TOKEN": "t", "PR_UPDATE_TOKEN": "t", "NTFY_URL": "n",
                      "CI_KIT_GIT_SSH_KEY": "s", "CLAUDE_CODE_OAUTH_TOKEN": "o", "ANTHROPIC_API_KEY": "k", "PATH": "/bin"})
    assert not {"GH_TOKEN", "GITHUB_TOKEN", "PR_UPDATE_TOKEN", "NTFY_URL", "CI_KIT_GIT_SSH_KEY"} & set(env)
    assert pr_fix.env({"ANTHROPIC_API_KEY": "k"}) .get("ANTHROPIC_API_KEY") == "k"   # the key alone: kept
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "o" and "ANTHROPIC_API_KEY" not in env   # ONE credential


def test_the_fixer_prompt_forbids_test_edits_and_names_the_scope() -> None:
    text = pr_fix.prompt(repo="o/r", pr=7, title="t", mode="conflict", base_ref="main", conflicted=["a.py"],
                         mine=["a.py", "b.py"], regen="", context="unit failed")
    assert "a.py" in text and "b.py" in text and "unit failed" in text
    assert "Never change a test" in pr_fix.RULES


def test_a_fixer_with_no_structured_answer_is_failed_never_fixed() -> None:
    assert pr_fix.judge("", "boom", 1)[0] == "failed"


# ── the record ────────────────────────────────────────────────────────────────────────────────
def test_the_outcome_pages_whenever_nothing_was_pushed() -> None:
    assert pr_ladder.outcome(True, "", "regen", "", "", "", "pushed", "gen.txt", True)[:2] == ("regen", "pushed")
    assert pr_ladder.outcome(True, "", "regen", "", "", "", "pushed", "x", True)[3] is False
    assert pr_ladder.outcome(True, "", "uncovered", "a.py", "declined", "needs a test change", "", "", True)[3] is True
    assert pr_ladder.outcome(True, "", "nothing", "", "", "", "", "", False)[1:2] == ("exhausted",)
    assert pr_ladder.outcome(True, "", "regen", "", "", "", "refused", "tests/x.py", True)[3] is True
    assert pr_ladder.outcome(False, "", "", "", "", "", "", "", True)[1] == "failed"      # a plan that never ran
    assert pr_ladder.outcome(False, "head moved", "", "", "", "", "", "", True)[1:2] == ("skipped",)
