"""Base-driven retry (ci_kit/pr_baseretry.py), both directions, against a fake GitHub that models
the PR's merge base, the main commits it lacks, the commit graph, and who wrote each comment.

2026-11-15: an armed Renovate PR sat BEHIND and red, held as "the author's fix re-queues it", while
the fix had landed on MAIN. MUST FIRE: main went red -> green on the failing check (red at the PR's
base, green since) -> one update. MUST NOT FIRE: main always green (the PR's own bug: the reviewer's
case against 6e69e0d), main still red, a neutral result, the same name from another workflow, a head
already retried, the cap (surfaced instead), a forged marker, an unarmed/draft/conflicted/opted-out
PR, a head the heal ladder took, and never ahead of a green ready PR. Default off reads nothing.
"""
from __future__ import annotations

import json

import pytest

import ci_kit.pr_autoupdate as upd
from ci_kit import pr_baseretry as br
from ci_kit import pr_ladder, pr_queue

ME, MALLORY = "estate-bot", "mallory"
H, H0, H2, X = "a" * 40, "c" * 40, "b" * 40, "d" * 40           # PR heads
MB, A1, L1, L2 = "1" * 40, "3" * 40, "5" * 40, "6" * 40         # main: merge base, before it, lacked (old, new)
CI, OTHER = ".github/workflows/ci.yml", ".github/workflows/nightly.yml"
RED = {"__typename": "CheckRun", "name": "test", "status": "COMPLETED", "conclusion": "FAILURE",
       "startedAt": "2026-11-15T00:00:00Z"}
GREEN = {**RED, "conclusion": "SUCCESS"}


class _Done:
    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, err


def _pr(n: int, *checks: dict, **k) -> dict:
    return {"number": n, "title": f"pr {n}", "url": "u", "headRefOid": H, "headRefName": f"b{n}",
            "baseRefName": "main", "labels": [], "isCrossRepository": False, "isDraft": False,
            "mergeStateStatus": "BEHIND", "mergeable": "MERGEABLE", "author": {"login": "someone"},
            "autoMergeRequest": {"enabledAt": "2026-11-15T00:00:00Z"},
            "statusCheckRollup": list(checks) or [RED], **k}


def _mark(head: str, who: str = ME) -> tuple[str, str]:
    return who, br.MARK.format(head=head, base=MB)


class World:
    """A fake GitHub. `main` = {sha: [(check name, conclusion, workflow path)]} for main's commits; the
    PR head carries `test` red from ci.yml. `parents` = the commit graph (default: the head is an author commit)."""

    def __init__(self, mp: pytest.MonkeyPatch, rows: list[dict], *, main: dict | None = None,
                 parents: dict[str, list[str]] | None = None, notes: dict[int, list[tuple[str, str]]] | None = None,
                 write_rc: int = 0, me: str = ME) -> None:
        self.calls: list[tuple] = []
        self.reads: list[tuple] = []
        self.comments: list[tuple[int, str]] = []
        self.order: list[str] = []
        self.main = {MB: [("test", "FAILURE", CI)], L2: [("test", "SUCCESS", CI)]} if main is None else main
        self.parents = {H: [H0]} if parents is None else parents
        self.notes, self.write_rc, self.me = notes or {}, write_rc, me
        mp.setattr(upd.pr_list, "rows", lambda _: rows)
        mp.setattr(upd, "_gh_read", self.read)
        mp.setattr(upd, "_gh", self.gh)
        mp.setattr(upd.pr_heal, "comments_by", lambda repo, n: list(self.notes.get(n, [])))
        mp.setattr(upd.pr_heal, "comments_of", lambda repo, n: [b for _, b in self.notes.get(n, [])])
        mp.setattr(upd.pr_heal, "write", self.write)

    def _runs(self, sha: str) -> tuple[list[dict], list[dict]]:
        rows = [("test", "FAILURE", CI)] if sha in (H, H2) else self.main.get(sha, [])
        checks, wfs = [], []
        for i, (name, concl, path) in enumerate(rows, 1):
            suite = hash((sha, path)) % 10_000 + 1
            checks.append({"name": name, "status": "completed", "conclusion": concl.lower(), "id": i * 10,
                           "app": "github-actions", "suite": suite,
                           "details_url": "https://github.com/o/r/actions/runs/77/job/88"})
            wfs.append({"suite": suite, "path": path})
        return checks, wfs

    def read(self, *a: str) -> _Done:
        self.reads.append(a)
        path = next((x for x in a if x.startswith("repos/")), "")
        lines = lambda xs: _Done(out=chr(10).join(json.dumps(x) for x in xs))  # noqa: E731
        if "/compare/" in path:
            return _Done(out=chr(10).join([MB, L1, L2]))  # merge base, then the lacked commits oldest first
        if "/commits?sha=" in path:
            return _Done(out=chr(10).join([MB, A1]))
        sha = path.split("/commits/")[-1].split("/")[0] if "/commits/" in path else path.split("head_sha=")[-1][:40]
        if path.endswith("/check-runs?per_page=100"):
            return lines(self._runs(sha)[0])
        if "/actions/runs?head_sha=" in path:
            return lines(self._runs(sha)[1])
        if path.endswith("/statuses?per_page=100"):
            return _Done(out="")
        if path.endswith("/actions/runs/77"):
            return _Done(out=json.dumps({"run_attempt": 1, "conclusion": "failure", "html_url": "run77"}))
        if "/actions/runs/77/jobs" in path:
            return _Done(out=json.dumps({"jobs": [{"id": 88, "name": "test", "conclusion": "failure",
                                                   "steps": [{"name": "pytest", "conclusion": "failure"}]}]}))
        if path.endswith("/annotations"):
            return _Done(out="[]")
        if "/commits/" in path:
            return _Done(out=json.dumps(self.parents.get(sha, [])))
        return _Done(out="{}")

    def gh(self, *a: str, **k) -> _Done:
        self.calls.append(a)
        if a[:3] == ("api", "-X", "PUT"):
            self.order.append("put " + a[3].split("/")[4])
        if a[:2] == ("pr", "view"):
            return _Done(out="OPEN")
        if a[:2] == ("run", "list"):
            return _Done(out="[]")
        return _Done()

    def write(self, *a: str, input_text: str | None = None) -> _Done:
        if a[:2] == ("api", "user"):
            return _Done(out=self.me + chr(10)) if self.me else _Done(rc=1, err="Bad credentials")
        if a[:2] == ("pr", "comment"):
            self.comments.append((int(a[2]), input_text or ""))
            self.order.append("comment " + a[2])
        return _Done(rc=self.write_rc)

    def puts(self) -> list[int]:
        return [int(c[3].split("/")[4]) for c in self.calls if c[:3] == ("api", "-X", "PUT")]

    def compared(self) -> bool:
        return any("/compare/" in x for r in self.reads for x in r)


def _run(w: World, cap: int = 2, slots: int = 1, **k) -> tuple[int, list[str]]:
    summary: list[str] = []
    k.setdefault("rerun_limit", 0)
    rc = upd.run("o/r", summary, [], upd.Options(max_in_flight=slots, base_retry_cap=cap, **k))
    return rc, summary


# ── (a) MUST FIRE: main went red -> green on the failing check ───────────────────────────────────
def test_main_red_at_base_then_green_is_retried_once_marker_first_must_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch, [_pr(7)])
    rc, summary = _run(w)
    assert rc == 0 and w.puts() == [7]
    assert w.order == ["comment 7", "put 7"]  # the record (the budget) BEFORE the update
    assert br.MARK.format(head=H, base=L2) in w.comments[0][1]  # base = main's NEWEST commit it lacked
    assert any("#7: red and behind -> base-driven retry" in s and "1 of 2" in s for s in summary)


def test_red_just_before_the_merge_base_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch, [_pr(7)], main={A1: [("test", "FAILURE", CI)], L2: [("test", "SUCCESS", CI)]})
    assert _run(w)[0] == 0 and w.puts() == [7]


def test_a_marker_that_cannot_be_written_means_no_update(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch, [_pr(7)], write_rc=1)
    rc, summary = _run(w)
    assert rc == 1 and w.puts() == []
    assert any("base-driven retry NOT made" in s for s in summary)


# ── (b) MUST NOT FIRE: no red -> green on main ───────────────────────────────────────────────────
def test_the_prs_own_bug_main_always_green_is_held_must_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    """The review's case: under strict protection main is green by construction, so 'green on newer
    main' is true of EVERY judgment red. Main never red at the base = the old hold, and main's commits
    since are not even read."""
    w = World(monkeypatch, [_pr(7)], main={MB: [("test", "SUCCESS", CI)], L2: [("test", "SUCCESS", CI)]})
    rc, summary = _run(w)
    assert rc == 0 and w.puts() == [] and w.comments == []
    assert any("never red on main at its base" in s for s in summary)
    assert not any(L2 in x for r in w.reads for x in r)


@pytest.mark.parametrize("main,why", [
    ({MB: [("test", "FAILURE", CI)], L2: [("test", "FAILURE", CI)]}, "still failure on main"),
    ({MB: [("test", "FAILURE", CI)], L2: [("test", "NEUTRAL", CI)]}, "no finished result on main"),
    ({MB: [("test", "FAILURE", CI)], L2: [("test", "SKIPPED", CI)]}, "no finished result on main"),
    ({MB: [("test", "FAILURE", CI)]}, "no finished result on main"),
    ({MB: [("test", "NEUTRAL", CI)], L2: [("test", "SUCCESS", CI)]}, "never red on main"),
    ({MB: [("test", "CANCELLED", CI)], L2: [("test", "SUCCESS", CI)]}, "never red on main"),
    # the same NAME from another workflow is not the same check
    ({MB: [("test", "FAILURE", OTHER)], L2: [("test", "SUCCESS", CI)]}, "never red on main"),
    ({MB: [("test", "FAILURE", CI)], L2: [("test", "SUCCESS", OTHER)]}, "no finished result on main"),
])
def test_no_red_to_green_on_main_is_held(monkeypatch: pytest.MonkeyPatch, main: dict, why: str) -> None:
    w = World(monkeypatch, [_pr(7)], main=main)
    rc, summary = _run(w)
    assert rc == 0 and w.puts() == [] and w.comments == []
    assert any("base-driven hold: no evidence main fixed it" in s and why in s for s in summary), summary


# ── (c) no loop: a head already retried is held ──────────────────────────────────────────────────
def test_a_head_already_retried_is_held_without_reading_main(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch, [_pr(7)], notes={7: [_mark(H)]})
    rc, summary = _run(w)
    assert rc == 0 and w.puts() == [] and w.comments == [] and not w.compared()
    assert any("already retried once from this head" in s for s in summary)


def test_retried_still_red_base_unchanged_is_held(monkeypatch: pytest.MonkeyPatch) -> None:
    # the retry's merge made it current: BLOCKED (red, up to date) is not a candidate, nothing is read
    w = World(monkeypatch, [_pr(7, headRefOid=H2, mergeStateStatus="BLOCKED")], notes={7: [_mark(H)]})
    rc, summary = _run(w)
    assert rc == 0 and w.puts() == [] and w.reads[1:] == []
    assert any("#7: held, current with main, checks failing" in s for s in summary)


# ── (d) the cap: counted along the ancestry, surfaced once, then quiet ───────────────────────────
def test_the_cap_surfaces_once_and_holds_must_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = {H2: [H, L1], H: [H0, MB], H0: ["e" * 40]}  # H2 = merge of retried H; H = merge of retried H0
    notes = [_mark(H0), _mark(H)]
    w = World(monkeypatch, [_pr(7, headRefOid=H2)], parents=graph, notes={7: notes})
    rc, summary = _run(w)
    assert rc == 1 and w.puts() == []
    assert len(w.comments) == 1 and br.SPENT.format(head=H2) in w.comments[0][1]
    assert any("base-driven spent" in s for s in summary)
    w2 = World(monkeypatch, [_pr(7, headRefOid=H2)], parents=graph, notes={7: [*notes, (ME, w.comments[0][1])]})
    rc, summary = _run(w2)
    assert rc == 0 and w2.puts() == [] and w2.comments == [] and not w2.compared()
    assert any("base-driven quiet" in s for s in summary)


def test_count_walks_merges_and_stops_at_an_own_commit() -> None:
    g = {H2: [X, L2], X: [H, L1], H: [H0, MB], H0: ["e" * 40]}
    get = lambda s: g.get(s, [])  # noqa: E731
    assert br.count(H2, get, {H, H0}, 5) == 2        # a PLAIN update (X) between retries does not reset it
    assert br.count(H2, get, {H}, 5) == 1
    assert br.count(H2, get, {H, H0}, 1) == 1        # stops at the cap
    g2 = {H2: [H], H: [H0, MB]}                      # H2 is an author commit on top: a new chain
    assert br.count(H2, lambda s: g2.get(s, []), {H, H0}, 5) == 0


def test_a_forged_marker_neither_grants_nor_resets(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = {H2: [H, L1], H: [H0, MB], H0: ["e" * 40]}
    # someone else's markers at the chain heads: not counted, so this is retry 2 of 2, not spent and not 1
    w = World(monkeypatch, [_pr(7, headRefOid=H2)], parents=graph,
              notes={7: [_mark(H0, MALLORY), _mark(H), (MALLORY, "<!-- smart-merge:base-retry head=" + H + " n=0 -->")]})
    rc, summary = _run(w)
    assert rc == 0 and w.puts() == [7] and any("retry 2 of 2" in s for s in summary)
    assert br.trusted([_mark(H, MALLORY)], ME, br._MARK) == set()
    assert br.trusted([_mark(H)], "", br._MARK) == set()  # an unknown identity trusts nothing


def test_an_unknown_token_identity_refuses_the_feature(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch, [_pr(7)], me="")
    rc, summary = _run(w)
    assert rc == 1 and w.puts() == [] and not w.compared()
    assert any("base-driven retry SKIPPED" in s for s in summary)


# ── (e) never touched: unarmed, draft, conflicted, DIRTY, opted out, a fork, a rider, not red ────
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
                               {"mergeable": "CONFLICTING"}, {"labels": [{"name": "conflict"}]},
                               {"autoMergeRequest": None}])
def test_an_excluded_pr_is_never_updated_through_run(monkeypatch: pytest.MonkeyPatch, k: dict) -> None:
    w = World(monkeypatch, [_pr(7, **k)])
    _run(w)
    assert w.puts() == [] and w.comments == [] and not w.compared()


# ── (f) a green ready PR goes first ──────────────────────────────────────────────────────────────
def test_a_green_ready_pr_takes_the_slot_before_a_retry_must_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch, [_pr(3), _pr(9, GREEN)])
    rc, summary = _run(w)
    assert rc == 0 and w.puts() == [9] and w.comments == []  # #3 is lower, but red: it waits, unrecorded
    assert any("#3: held, queued, position 1" in s for s in summary)


def test_plan_orders_retries_after_every_green_pr() -> None:
    q = pr_queue.plan([_pr(1), _pr(2, GREEN), _pr(3, GREEN)], 2, base_retry=frozenset({1}))
    assert [p["number"] for p in q.update] == [2, 3] and [p["number"] for p in q.spare] == [1]
    q = pr_queue.plan([_pr(1), _pr(2, GREEN)], 2, base_retry=frozenset({1}))
    assert [p["number"] for p in q.update] == [2, 1]
    q = pr_queue.plan([_pr(1), _pr(2, GREEN)], 2)  # not decided retryable: the old hold
    assert [p["number"] for p in q.update] == [2] and "author's fix" in q.held[0][1]


# ── (g) ONE ACTOR PER HEAD, in the production configuration (ladder on, smart auto-merge on) ────
LADDER = pr_ladder.Config(on=True)


def test_a_retried_pr_is_not_also_handed_to_the_ladder_must_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch, [_pr(7)])
    healing: list[dict] = []
    summary: list[str] = []
    rc = upd.run("o/r", summary, [], upd.Options(max_in_flight=1, base_retry_cap=2, rerun_limit=2, ladder=LADDER),
                 healing)
    assert rc == 0 and w.puts() == [7] and healing == []
    assert [n for n, _ in w.comments] == [7] and br.MARK.format(head=H, base=L2) in w.comments[0][1]  # no heal marker
    assert not any("smart-merge:heal" in b or "smart-merge:judgment" in b for _, b in w.comments)


def test_a_red_pr_not_retried_still_gets_the_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch, [_pr(7)], main={MB: [("test", "SUCCESS", CI)], L2: [("test", "SUCCESS", CI)]})
    healing: list[dict] = []
    upd.run("o/r", [], [], upd.Options(max_in_flight=1, base_retry_cap=2, rerun_limit=2, ladder=LADDER), healing)
    assert w.puts() == [] and [r["pr"] for r in healing] == [7]


def test_a_head_the_ladder_took_is_never_base_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    took = pr_ladder.HEAL.format(head=H, mode="red", claude=1, at="2026-11-15T00:00:00Z")
    w = World(monkeypatch, [_pr(7)], notes={7: [(ME, took)]})
    rc, summary = _run(w, rerun_limit=2, ladder=LADDER)
    assert w.puts() == [] and not w.compared()
    assert any("the heal ladder took this head" in s for s in summary)


# ── (h) DEFAULT OFF is a no-op; an unreadable cap is refused, never a crash ──────────────────────
def test_default_off_reads_nothing_beyond_the_pr_list(monkeypatch: pytest.MonkeyPatch) -> None:
    assert upd.base_retry_cap("") == (0, "") and upd.Options().base_retry_cap == 0
    w = World(monkeypatch, [_pr(7), _pr(9, GREEN)])
    rc, summary = _run(w, cap=0)
    assert rc == 0 and w.puts() == [9] and w.comments == []
    assert len(w.reads) == 1 and w.reads[0][:2] == ("api", "graphql")  # the PR list, nothing else
    assert any("#7: held, behind, a check is failing on its head (the author's fix re-queues it)" in s for s in summary)


@pytest.mark.parametrize("raw", ["two", "1.5", "-1"])
def test_an_unreadable_cap_is_refused_and_the_pass_goes_on(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    cap, err = upd.base_retry_cap(raw)
    assert cap == 0 and err
    w = World(monkeypatch, [_pr(7), _pr(9, GREEN)])
    summary: list[str] = []
    rc = upd.run("o/r", summary, [], upd.Options(max_in_flight=1, rerun_limit=0, base_retry_cap=cap, base_retry_error=err))
    assert rc == 1 and w.puts() == [9] and not w.compared()
    assert any("base-driven retry OFF this pass" in s for s in summary)
    assert upd.base_retry_cap("3") == (3, "")


def test_a_red_check_main_cannot_speak_for_holds_the_whole_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    # `test` went red -> green on main, but `review` (a commit status at the PR head only) is red too
    review = {"__typename": "StatusContext", "context": "review", "state": "FAILURE"}
    w = World(monkeypatch, [_pr(7, RED, review)])
    rc, summary = _run(w)
    assert w.puts() == [] and any("`review` not identified at its head" in s for s in summary), summary


def test_the_cap_reaches_the_options_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[upd.Options] = []
    monkeypatch.setattr(upd, "run", lambda repo, s, r, opts, h=None: seen.append(opts) or 0)
    for var in ("CI_KIT_BASE_RETRY_CAP", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(var, raising=False)
    assert upd.main(["--repo", "o/r"]) == 0 and seen[-1].base_retry_cap == 0
    monkeypatch.setenv("CI_KIT_BASE_RETRY_CAP", "nope")
    assert upd.main(["--repo", "o/r"]) == 0 and seen[-1].base_retry_cap == 0 and seen[-1].base_retry_error


# ── the shared comment reader keeps each author (merge_train.ops) ────────────────────────────────
def test_the_one_reader_returns_authors_and_comments_of_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    from ci_kit.merge_train import github, ops
    seen: list[tuple] = []
    pages = json.dumps([[ME, "one"], [MALLORY, "two"]]) + chr(10) + json.dumps([[ME, "three"]])
    monkeypatch.setattr(github, "gh", lambda *a, **k: seen.append(a) or _Done(out=pages))
    assert ops.comments_by("o/r", 7) == [(ME, "one"), (MALLORY, "two"), (ME, "three")]
    assert ops.comments_of("o/r", 7) == ["one", "two", "three"]
    assert ".user.login" in seen[0][-1] and "--paginate" in seen[0]
