"""Base-driven retry (ci_kit/pr_baseretry.py), both directions.

2026-11-15: an armed Renovate PR sat BEHIND and red, held as "a check is failing on its head (the
author's fix re-queues it)". The cause was a time-dependent test on MAIN, fixed on main by another
PR -- nothing ever pushed to the held PR, so nothing re-queued it. MUST FIRE: main green on the
failing check since the PR's merge base -> one update. MUST NOT FIRE: main red/absent on it, a head
already retried, the cap reached (surfaced instead), an unarmed/draft/conflicted/opted-out PR, and
never ahead of a green ready PR.
"""
from __future__ import annotations

import json

import pytest

import ci_kit.pr_autoupdate as upd
from ci_kit import pr_baseretry as br
from ci_kit import pr_queue

H, H2, B1, B2, OLD = "a" * 40, "b" * 40, "1" * 40, "2" * 40, "9" * 40
RED = {"__typename": "CheckRun", "name": "test", "status": "COMPLETED", "conclusion": "FAILURE",
       "startedAt": "2026-11-15T00:00:00Z"}
GREEN = {**RED, "conclusion": "SUCCESS"}


class _Done:
    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, err


def _pr(n: int, *checks: dict, **k) -> dict:
    return {"number": n, "title": f"pr {n}", "url": "u", "headRefOid": H, "headRefName": f"b{n}",
            "baseRefName": "main", "labels": [], "isCrossRepository": False, "isDraft": False,
            "mergeStateStatus": "BEHIND", "mergeable": "MERGEABLE",
            "autoMergeRequest": {"enabledAt": "2026-11-15T00:00:00Z"},
            "statusCheckRollup": list(checks) or [RED], **k}


def _mark(head: str, n: int) -> str:
    return br.MARK.format(head=head, base=OLD, n=n)


class World:
    """A fake GitHub: the PR list, main's commits the PR lacks, the head's parents, main's check results."""

    def __init__(self, mp: pytest.MonkeyPatch, rows: list[dict], *, main: str = "SUCCESS",
                 parents: list[str] | None = None, bodies: dict[int, list[str]] | None = None,
                 write_rc: int = 0) -> None:
        self.calls: list[tuple] = []
        self.reads: list[tuple] = []
        self.comments: list[tuple[int, str]] = []
        self.order: list[str] = []
        self.main, self.parents, self.bodies, self.write_rc = main, parents or [OLD], bodies or {}, write_rc
        mp.setattr(upd.pr_list, "rows", lambda _: rows)
        mp.setattr(upd, "_gh_read", self.read)
        mp.setattr(upd, "_gh", self.gh)
        mp.setattr(upd.pr_heal, "comments_of", lambda repo, n: list(self.bodies.get(n, [])))
        mp.setattr(upd.pr_heal, "write", self.write)

    def read(self, *a: str) -> _Done:
        self.reads.append(a)
        path = next((x for x in a if x.startswith("repos/")), "")
        if "/compare/" in path:
            return _Done(out=B1 + chr(10) + B2 + chr(10))  # oldest first, as the API lists them
        if path.endswith("/check-runs?per_page=100"):
            if self.main == "absent":
                return _Done(out="")
            return _Done(out=json.dumps({"name": "test", "status": "completed", "conclusion": self.main.lower(),
                                         "id": 5}))
        if path.endswith("/status"):
            return _Done(out="")
        if "/commits/" in path:
            return _Done(out=json.dumps(self.parents))
        return _Done(out="{}")

    def gh(self, *a: str, **k) -> _Done:
        self.calls.append(a)
        if a[:3] == ("api", "-X", "PUT"):
            self.order.append("put " + a[3].split("/")[4])
        if a[:2] == ("pr", "view"):
            return _Done(out="OPEN")
        return _Done()

    def write(self, *a: str, input_text: str | None = None) -> _Done:
        if a[:2] == ("pr", "comment"):
            self.comments.append((int(a[2]), input_text or ""))
            self.order.append("comment " + a[2])
        return _Done(rc=self.write_rc)

    def puts(self) -> list[int]:
        return [int(c[3].split("/")[4]) for c in self.calls if c[:3] == ("api", "-X", "PUT")]


def _run(w: World, cap: int = 2, slots: int = 1) -> tuple[int, list[str]]:
    summary: list[str] = []
    rc = upd.run("o/r", summary, [], upd.Options(max_in_flight=slots, rerun_limit=0, base_retry_cap=cap))
    return rc, summary


# ── (a) MUST FIRE: red + behind, main green on the failing check since the merge base ────────────────
def test_red_behind_and_green_on_main_is_retried_once_marker_first_must_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch, [_pr(7)])
    rc, summary = _run(w)
    assert rc == 0 and w.puts() == [7]
    assert w.order == ["comment 7", "put 7"]  # the record (the budget) BEFORE the update
    assert br.MARK.format(head=H, base=B2, n=1) in w.comments[0][1]  # base = main's NEWEST commit it lacked
    assert any("#7: red and behind -> base-driven retry" in s and "1 of 2" in s for s in summary)


def test_off_is_the_old_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch, [_pr(7)])
    rc, summary = _run(w, cap=0)
    assert rc == 0 and w.puts() == [] and w.reads[1:] == []  # nothing read past the PR list
    assert any("the author's fix re-queues it" in s for s in summary)


def test_a_marker_that_cannot_be_written_means_no_update(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch, [_pr(7)], write_rc=1)
    rc, summary = _run(w)
    assert rc == 1 and w.puts() == []
    assert any("base-driven retry NOT made" in s for s in summary)


# ── (b) MUST NOT FIRE: main red, or never ran, on the failing check ─────────────────────────────────
@pytest.mark.parametrize("main", ["FAILURE", "absent"])
def test_no_green_on_main_is_held(monkeypatch: pytest.MonkeyPatch, main: str) -> None:
    w = World(monkeypatch, [_pr(7)], main=main)
    rc, summary = _run(w)
    assert rc == 0 and w.puts() == [] and w.comments == []
    assert any("base-driven hold: no evidence the base fixed it" in s and "`test`" in s for s in summary)


# ── (c) no loop: a head already retried is held; one not BEHIND is not even a candidate ─────────────
def test_a_head_already_retried_is_held_without_reading_github(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch, [_pr(7)], bodies={7: [_mark(H, 1)]})
    rc, summary = _run(w)
    assert rc == 0 and w.puts() == [] and w.comments == []
    assert not any("/compare/" in x for r in w.reads for x in r)
    assert any("already retried once from this head" in s for s in summary)


def test_retried_still_red_base_unchanged_is_held(monkeypatch: pytest.MonkeyPatch) -> None:
    # the retry's merge made it current: BLOCKED (red, up to date) is not a candidate, nothing is read
    w = World(monkeypatch, [_pr(7, headRefOid=H2, mergeStateStatus="BLOCKED")], bodies={7: [_mark(H, 1)]})
    rc, summary = _run(w)
    assert rc == 0 and w.puts() == [] and w.reads[1:] == []
    assert any("#7: held, current with main, checks failing" in s for s in summary)


# ── (d) the cap: surfaced once, then quiet ──────────────────────────────────────────────────────────
def test_the_cap_surfaces_once_and_holds_must_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    # H2 is the updater's merge of retry 2's head H: consecutive. main moved again and is green.
    w = World(monkeypatch, [_pr(7, headRefOid=H2)], parents=[H, B1], bodies={7: [_mark(OLD, 1), _mark(H, 2)]})
    rc, summary = _run(w)
    assert rc == 1 and w.puts() == []
    assert len(w.comments) == 1 and br.SPENT.format(head=H2, n=2) in w.comments[0][1]
    assert any("base-driven spent" in s for s in summary)
    w2 = World(monkeypatch, [_pr(7, headRefOid=H2)], parents=[H, B1],
               bodies={7: [_mark(OLD, 1), _mark(H, 2), w.comments[0][1]]})
    rc, summary = _run(w2)
    assert rc == 0 and w2.puts() == [] and w2.comments == []
    assert any("base-driven quiet" in s for s in summary)


def test_an_author_commit_resets_the_count() -> None:
    bodies = [_mark(OLD, 1), _mark(H, 2)]
    assert br.chain(bodies, H2, [H, B1]) == 2       # the updater's merge of the last retried head
    assert br.chain(bodies, H2, [H]) == 0           # a one-parent commit on top: the author pushed
    assert br.chain(bodies, H2, [OLD, B1]) == 0     # a merge of some other head (a plain queue update)
    assert br.chain([], H2, [H, B1]) == 0


def test_decide_both_directions() -> None:
    ev = br.Evidence([B2, B1], [OLD], {"test": "green"})
    assert br.decide(H, [], ev, 2)[0::2] == ("retry", 1)
    assert br.decide(H, [], br.Evidence([B2], [OLD], {"test": "green", "lint": "red"}), 2)[0] == "hold"
    assert br.decide(H, [], br.Evidence([], [OLD], {"test": "green"}), 2)[0] == "hold"
    assert br.decide(H2, [_mark(H, 2)], br.Evidence([B2], [H, B1], {"test": "green"}), 2)[0] == "spent"
    assert br.decide(H2, [_mark(H, 2)], br.Evidence([B2], [H, B1], {"test": "green"}), 3)[0::2] == ("retry", 3)


# ── (e) never touched: unarmed, draft, conflicted, DIRTY, opted out, a fork, a rider, not red ───────
@pytest.mark.parametrize("why,k", [
    ("not armed", {"autoMergeRequest": None}),
    ("a draft", {"isDraft": True}),
    ("a draft", {"isCrossRepository": True}),
    ("riding", {"labels": [{"name": "conflict"}]}),
    ("riding", {"labels": [{"name": "queue:parked"}]}),
    ("`autoland-off`", {"labels": [{"name": "autoland-off"}]}),
    ("not cleanly behind", {"mergeStateStatus": "DIRTY"}),
    ("not cleanly behind", {"mergeable": "CONFLICTING"}),
    ("not cleanly behind", {"mergeStateStatus": "BLOCKED"}),
    ("not red", {"statusCheckRollup": [GREEN]}),
])
def test_never_a_candidate(why: str, k: dict) -> None:
    assert br.candidate(_pr(7, **k), (), "conflict", frozenset()).startswith(why)
    assert br.candidate(_pr(7), (), "conflict", frozenset({7})).startswith("machinery")
    assert br.candidate(_pr(7), (), "conflict", frozenset()) == ""


@pytest.mark.parametrize("k", [{"isDraft": True}, {"labels": [{"name": "autoland-off"}]},
                               {"mergeable": "CONFLICTING"}, {"labels": [{"name": "conflict"}]}])
def test_an_excluded_pr_is_never_updated_through_run(monkeypatch: pytest.MonkeyPatch, k: dict) -> None:
    w = World(monkeypatch, [_pr(7, **k)])
    _run(w)
    assert w.puts() == [] and w.comments == [] and not any("/compare/" in x for r in w.reads for x in r)


# ── (f) a green ready PR goes first ────────────────────────────────────────────────────────────────
def test_a_green_ready_pr_takes_the_slot_before_a_retry_must_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch, [_pr(3), _pr(9, GREEN)])
    rc, summary = _run(w)
    assert rc == 0 and w.puts() == [9] and w.comments == []  # #3 is lower, but red: it waits
    assert any("#3: held, queued, position 1" in s for s in summary)


def test_plan_orders_retries_after_every_green_pr() -> None:
    q = pr_queue.plan([_pr(1), _pr(2, GREEN), _pr(3, GREEN)], 2, base_retry=frozenset({1}))
    assert [p["number"] for p in q.update] == [2, 3] and [p["number"] for p in q.spare] == [1]
    q = pr_queue.plan([_pr(1), _pr(2, GREEN)], 2, base_retry=frozenset({1}))
    assert [p["number"] for p in q.update] == [2, 1]
    q = pr_queue.plan([_pr(1), _pr(2, GREEN)], 2)  # not decided retryable: the old hold
    assert [p["number"] for p in q.update] == [2] and "author's fix" in q.held[0][1]
