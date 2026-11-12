"""Smart auto-merge (ci_kit/redclass.py + ci_kit/pr_heal.py), BOTH directions, on recorded GitHub answers.

MUST FIRE: trade-data#2988's lost sealed reviewer (run 43115550872, 2026-11-11) is MACHINERY and is
rerun. MUST NOT FIRE: the same head's migration-contract lint failure (run 43115550424) is
JUDGMENT and is never rerun -- and a head carrying both is judgment as a whole.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import ci_kit.pr_autoupdate as upd
from ci_kit import pr_heal, pr_queue, redclass
from ci_kit.merge_train import policy
from ci_kit.merge_train.ops import bodies

FIX = Path(__file__).parent / "fixtures" / "github"
LOST = json.loads((FIX / "jobs_trade-data_43115550872_machinery.json").read_text(encoding="utf-8"))
LINT = json.loads((FIX / "jobs_trade-data_43115550424_lint.json").read_text(encoding="utf-8"))
LOST_NOTE = ("The self-hosted runner lost communication with the server. Verify the machine is running and has a "
             "healthy network connection.")
HEAD = "7334c9083574c4aa9cd3a35f4a9136539061a58d"


def _job(jobs: dict, name: str) -> dict:
    return next(j for j in jobs["jobs"] if j["name"] == name)


def _classes(jobs: dict, notes: dict[str, list[str]] | None = None) -> list[tuple[str, str, str]]:
    return [(j["name"], *redclass.job_class(j, (notes or {}).get(j["name"], []))) for j in jobs["jobs"]]


# ── redclass: the jobs depth ──────────────────────────────────────────────────────────────────
def test_the_lost_sealed_reviewer_is_machinery_and_its_alls_green_a_consequence_must_fire() -> None:
    cls, why = redclass.job_class(_job(LOST, "reviewer / review"), [], "BlobNotFound ... HTTP 404")
    assert cls == redclass.MACHINERY and "never concluded" in why and "log is gone" in why
    assert redclass.job_class(_job(LOST, "reviewer / review"), [LOST_NOTE])[0] == redclass.MACHINERY
    assert redclass.job_class(_job(LOST, "review gate"))[0] == redclass.CONSEQUENCE
    assert redclass.run_class(_classes(LOST))[0] == redclass.MACHINERY


def test_a_real_lint_failure_is_judgment_even_with_a_runner_lost_note_must_not_fire() -> None:
    job = _job(LINT, "lint + doc checks")
    cls, why = redclass.job_class(job, [LOST_NOTE])
    assert cls == redclass.JUDGMENT and "migration contract" in why
    assert redclass.run_class(_classes(LINT))[0] == redclass.JUDGMENT


def test_an_aggregator_red_alone_is_judgment_never_retried() -> None:
    assert redclass.run_class([("review gate", redclass.CONSEQUENCE, "x")])[0] == redclass.JUDGMENT


def test_setup_steps_need_the_networks_word_or_a_missing_log() -> None:
    job = {"conclusion": "failure", "steps": [{"name": "Set up job", "conclusion": "success"},
                                              {"name": "Run actions/checkout@v7", "conclusion": "failure"}]}
    assert redclass.job_class(job, log_tail="fatal: unable to access: Could not resolve host: github.com")[0] == "machinery"
    assert redclass.job_class(job, log_tail="received unexpected HTTP status: 503 Service Unavailable")[0] == "machinery"
    assert redclass.job_class(job, log_error="HTTP 404: BlobNotFound")[0] == "machinery"
    assert redclass.job_class(job, log_tail="fatal: reference is not a tree: deadbeef")[0] == "judgment"
    test = {"conclusion": "failure", "steps": [{"name": "Run actions/checkout@v7", "conclusion": "success"},
                                               {"name": "pytest", "conclusion": "failure"}]}
    assert redclass.job_class(test, log_tail="ECONNRESET")[0] == "judgment"  # a test's own words prove nothing


def test_cancelled_startup_and_provisioning_timeouts_are_machinery_a_hang_is_not() -> None:
    assert redclass.job_class({"conclusion": "cancelled", "steps": []})[0] == "machinery"
    assert redclass.job_class({"conclusion": "startup_failure"})[0] == "machinery"
    assert redclass.job_class({"conclusion": "timed_out", "steps": []})[0] == "machinery"
    hung = {"conclusion": "timed_out", "steps": [{"name": "pytest", "conclusion": "cancelled"}]}
    assert redclass.job_class(hung)[0] == "judgment"


def test_review_of_1_27_0_judgment_is_never_dressed_as_machinery_must_not_fire() -> None:
    """Reviewer findings 1, 1b, 3: a cancel AFTER an own step failed, a timeout-minutes expiry
    (GitHub concludes it `cancelled`, with an annotation), and a job that failed with no step at all
    (a strategy / runs-on / if: expression the PR broke) are all the PR's. Never rerun."""
    after = {"conclusion": "cancelled", "steps": [{"name": "pytest", "conclusion": "failure"},
                                                  {"name": "upload logs", "conclusion": "cancelled"}]}
    assert redclass.job_class(after)[0] == "judgment"
    hang = {"conclusion": "cancelled", "steps": [{"name": "Set up job", "conclusion": "success"},
                                                 {"name": "pytest", "conclusion": "cancelled"}]}
    note = "The job running on runner r1 has exceeded the maximum execution time of 30 minutes."
    assert redclass.job_class(hang, [note])[0] == "judgment"
    assert redclass.job_class(hang)[0] == "machinery"           # a plain cancel (superseded) still is
    assert redclass.job_class({"conclusion": "failure", "steps": []},
                              ["Error when evaluating 'strategy' for job 'test'"])[0] == "judgment"
    began = {"conclusion": "failure", "steps": [{"name": "Set up job", "conclusion": "success"},
                                                {"name": "Review", "conclusion": None}]}
    assert redclass.job_class(began)[0] == "machinery"         # td#2988's shape: still healed


def test_the_train_reads_the_same_rollup_classifier() -> None:
    roll = [{"name": "unit tests", "status": "COMPLETED", "conclusion": "STARTUP_FAILURE"}]
    assert policy.red_is_infra(roll, ()) is redclass.rollup_infra(roll, ()) is True
    roll.append({"name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"})
    assert policy.red_is_infra(roll, ()) is False


# ── pr_heal: the decision ─────────────────────────────────────────────────────────────────────
def _verdict(cls: str = "machinery", attempt: int = 1) -> pr_heal.Verdict:
    return pr_heal.Verdict(2988, HEAD, [pr_heal.Red(["review gate"], 43115550872, attempt, cls, "why", "u")])


def test_red_checks_counts_cancelled_and_failing_required_checks() -> None:
    roll = [{"name": "review gate", "status": "COMPLETED", "conclusion": "FAILURE"},
            {"name": "unit tests", "status": "COMPLETED", "conclusion": "CANCELLED"},
            {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "other", "status": "COMPLETED", "conclusion": "FAILURE"}]
    assert pr_heal.red_checks(roll, ("review gate", "unit tests", "lint")) == ["review gate", "unit tests"]


def test_a_superseded_red_is_not_red_but_a_newer_red_is() -> None:
    """Survey 2026-11-11: 370 of 385 machinery reds were CANCELLED runs, most superseded by a later
    run at the same head. The later answer decides; order in the rollup must not."""
    old = {"name": "unit tests", "status": "COMPLETED", "conclusion": "CANCELLED", "startedAt": "2026-11-11T21:00:00Z"}
    ok = {"name": "unit tests", "status": "COMPLETED", "conclusion": "SUCCESS", "startedAt": "2026-11-11T21:05:00Z"}
    running = {"name": "unit tests", "status": "IN_PROGRESS", "conclusion": None, "startedAt": "2026-11-11T21:05:00Z"}
    bad = {"name": "unit tests", "status": "COMPLETED", "conclusion": "FAILURE", "startedAt": "2026-11-11T21:09:00Z"}
    assert pr_heal.red_checks([old, ok]) == [] and pr_heal.red_checks([ok, old]) == []
    assert pr_heal.red_checks([running, old]) == []
    assert pr_heal.red_checks([old, ok, bad]) == ["unit tests"]                     # must fire


def test_machinery_is_rerun_up_to_the_limit_then_named_once_must_fire() -> None:
    v = _verdict()
    assert pr_heal.decide(v, [], behind=False, limit=2)[0] == "rerun"
    one = pr_heal.rerun_comment(v, 0, 2)
    assert pr_heal.decide(v, [one], behind=False, limit=2)[0] == "rerun"
    assert pr_heal.decide(v, [one, one], behind=False, limit=2)[0] == "exhausted"
    assert pr_heal.decide(v, [one, one, pr_heal.exhausted_comment(v, 2)], behind=False, limit=2)[0] == "quiet"
    # a hand rerun spends it too: attempt 3 = two reruns, whatever the comments say
    assert pr_heal.decide(_verdict(attempt=3), [], behind=False, limit=2)[0] == "exhausted"
    # a new head starts a new budget
    assert pr_heal.decide(pr_heal.Verdict(2988, "f" * 40, v.reds), [one, one], behind=False, limit=2)[0] == "rerun"


def test_machinery_behind_main_is_the_queues_update_not_a_rerun() -> None:
    assert pr_heal.decide(_verdict(), [], behind=True, limit=2)[0] == "update"


def test_judgment_is_never_rerun_and_named_once_per_head_must_not_fire() -> None:
    v = pr_heal.Verdict(2988, HEAD, [_verdict().reds[0], pr_heal.Red(["lint + doc checks"], 43115550424, 1,
                                                                      "judgment", "step failed: Lint", "u2")])
    assert v.kind == "judgment"
    for behind in (True, False):
        assert pr_heal.decide(v, [], behind=behind, limit=2)[0] == "judgment"
    said = pr_heal.judgment_comment(v)
    assert "kind=lint" in said and "lint + doc checks" in said
    assert pr_heal.decide(v, [said], behind=False, limit=2)[0] == "quiet"
    assert pr_heal.decide(pr_heal.Verdict(1, HEAD, [], ["ext/status"]), [], False, 2)[0] == "judgment"


def test_a_machinery_red_pr_behind_main_is_eligible_a_judgment_one_is_held() -> None:
    def pr(n: int) -> dict:
        return {"number": n, "mergeStateStatus": "BEHIND", "autoMergeRequest": {"enabledAt": f"2026-11-11T0{n}:00:00Z"},
                "statusCheckRollup": [{"name": "review gate", "status": "COMPLETED", "conclusion": "FAILURE"}]}
    q = pr_queue.plan([pr(1), pr(2)], 1, (), machinery=frozenset({2}))
    assert [p["number"] for p in q.update] == [2] and [p["number"] for p, _ in q.held] == [1]


# ── the whole pass, through a fake gh ─────────────────────────────────────────────────────────
class _Done:
    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, err


def _read(lost_attempt: int = 1):
    """A fake read token over the recorded #2988 answers at HEAD."""
    runs = {"43115550872": {"run_attempt": lost_attempt, "conclusion": "failure", "html_url": "u1"},
            "43115550424": {"run_attempt": 1, "conclusion": "failure", "html_url": "u2"}}
    checks = [{"id": 130850491916, "name": "review gate",
               "details_url": "https://github.com/o/r/actions/runs/43115550872/job/130850491916"},
              {"id": 130842483111, "name": "lint + doc checks",
               "details_url": "https://github.com/o/r/actions/runs/43115550424/job/130842483111"}]

    def read(*a: str) -> _Done:
        path = next(x for x in a if x.startswith("repos/") or x == "view")
        if "/check-runs?" in path:
            return _Done(out="\n".join(json.dumps(c) for c in checks))
        if path.endswith("/annotations"):
            return _Done(out=json.dumps([{"message": LOST_NOTE}] if "130842483667" in path else []))
        if "/jobs?" in path:
            return _Done(out=json.dumps(LOST if "43115550872" in path else LINT))
        return _Done(out=json.dumps(runs[path.rsplit("/", 1)[-1]]))
    return read


def _pr(red: tuple[str, ...], state: str = "BLOCKED") -> dict:
    return {"number": 2988, "headRefOid": HEAD, "headRefName": "feat/x", "mergeStateStatus": state, "labels": [],
            "autoMergeRequest": {"enabledAt": "2026-11-11T20:00:00Z"},
            "statusCheckRollup": [{"name": n, "status": "COMPLETED", "conclusion": "FAILURE"} for n in red]}


def test_the_lost_reviewer_head_is_rerun_and_recorded_must_fire() -> None:
    summary: list[str] = []
    heal, errs = pr_heal.assess_armed("o/r", [_pr(("review gate",))], ("review gate",), 2, _read(),
                                      lambda r, n: [], summary)
    assert errs == 0 and heal[2988][0].kind == "machinery"
    writes: list[tuple] = []

    def write(*a: str, input_text: str | None = None) -> _Done:
        writes.append((a, input_text))
        return _Done()
    assert pr_heal.act("o/r", heal, set(), 2, write, summary) == 0
    assert any("actions/runs/43115550872/rerun-failed-jobs" in " ".join(a) for a, _ in writes)
    record = next(t for a, t in writes if a[:2] == ("pr", "comment"))
    assert pr_heal.spent([record], HEAD, [1]) == 1


def test_the_lint_red_head_is_never_rerun_must_not_fire() -> None:
    summary: list[str] = []
    heal, _ = pr_heal.assess_armed("o/r", [_pr(("review gate", "lint + doc checks"))],
                                   ("review gate", "lint + doc checks"), 2, _read(), lambda r, n: [], summary)
    writes: list[tuple] = []
    pr_heal.act("o/r", heal, set(), 2, lambda *a, input_text=None: writes.append(a) or _Done(), summary)
    assert not any("rerun" in " ".join(a) for a in writes)
    assert [a[:2] for a in writes] == [("pr", "comment")]


def test_a_refused_rerun_is_an_error_naming_the_grant() -> None:
    summary: list[str] = []
    heal, _ = pr_heal.assess_armed("o/r", [_pr(("review gate",))], ("review gate",), 2, _read(), lambda r, n: [], summary)
    refused = _Done(1, "", "HTTP 403: Resource not accessible by personal access token")
    assert pr_heal.act("o/r", heal, set(), 2, lambda *a, input_text=None: refused, summary) == 1
    assert "Actions: Read and write" in summary[-1]


def test_a_403_that_is_not_the_grant_does_not_blame_the_grant() -> None:
    summary: list[str] = []
    heal, _ = pr_heal.assess_armed("o/r", [_pr(("review gate",))], ("review gate",), 2, _read(), lambda r, n: [], summary)
    old = _Done(1, "", "HTTP 403: Unable to retry this workflow run because it was created over a month ago")
    assert pr_heal.act("o/r", heal, set(), 2, lambda *a, input_text=None: old, summary) == 1
    assert "Actions: Read and write" not in summary[-1]


def test_an_exhausted_head_that_fell_behind_is_updated_not_held_must_fire() -> None:
    """Reviewer finding 2: the exhausted marker skipped the PR, so pr_queue held it forever -- the
    td#2988 stall. BEHIND, it is assessed again and counted machinery: its update is free."""
    marks = [pr_heal.EXHAUSTED.format(head=HEAD)]
    summary: list[str] = []
    heal, _ = pr_heal.assess_armed("o/r", [_pr(("review gate",), "BEHIND")], ("review gate",), 2, _read(),
                                   lambda r, n: marks, summary)
    assert pr_heal.machinery(heal) == {2988}
    still, _ = pr_heal.assess_armed("o/r", [_pr(("review gate",))], ("review gate",), 2, _read(),
                                    lambda r, n: marks, [])
    assert still == {}                                           # current with main: stays named, quiet


def test_an_unexplained_red_is_an_error_never_a_guess() -> None:
    def boom(*a: str) -> _Done:
        raise subprocess.CalledProcessError(1, "gh", "", "HTTP 502")
    summary: list[str] = []
    heal, errs = pr_heal.assess_armed("o/r", [_pr(("review gate",))], ("review gate",), 2, boom, lambda r, n: [], summary)
    assert heal == {} and errs == 1 and "NOT classified" in summary[-1]


def test_off_means_off() -> None:
    assert pr_heal.assess_armed("o/r", [_pr(("review gate",))], (), 0, pytest.fail, pytest.fail, []) == ({}, 0)


# ── the stale conflict label (td#2983) and the idle slot (td#2980) ─────────────────────────────
def test_a_resolved_conflict_loses_its_label_must_fire() -> None:
    lab = [{"name": "conflict"}]
    prs = [{"number": 2983, "mergeable": "MERGEABLE", "mergeStateStatus": "BLOCKED", "labels": lab},
           {"number": 1, "mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY", "labels": lab},
           {"number": 2, "mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN", "labels": lab},
           {"number": 3, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "labels": []},
           {"number": 4, "mergeable": "MERGEABLE", "mergeStateStatus": "BEHIND", "labels": lab}]
    assert upd.stale_labels(prs, attempted={4}, label="conflict") == [2983]


def test_a_skipped_review_hands_its_slot_to_the_next_in_line(monkeypatch: pytest.MonkeyPatch) -> None:
    def pr(n: int) -> dict:
        return {"number": n, "title": "t", "url": "u", "headRefOid": "a" * 40, "headRefName": f"b{n}", "labels": [],
                "isCrossRepository": False, "mergeStateStatus": "BEHIND", "mergeable": "MERGEABLE",
                "autoMergeRequest": {"enabledAt": f"2026-11-11T0{n}:00:00Z"}, "statusCheckRollup": []}
    rows = [pr(1), pr(2), pr(3)]
    monkeypatch.setattr(upd.pr_list, "rows", lambda _: rows)
    monkeypatch.setattr(upd, "_gh_read", lambda *a: _Done(out="{}"))
    monkeypatch.setattr(upd, "reviewing", lambda repo, branch, wf: branch == "b1")
    puts: list[str] = []
    monkeypatch.setattr(upd, "_gh", lambda *a, **k: puts.append(a[3]) or _Done() if a[:3] == ("api", "-X", "PUT") else _Done())
    summary: list[str] = []
    assert upd.run("o/r", summary, [], upd.Options(max_in_flight=1, review_workflow="rg.yml")) == 0
    assert puts == ["repos/o/r/pulls/2/update-branch"]
    assert "#2: takes the slot #1's running review left free" in summary


def test_comment_pages_are_all_read() -> None:
    assert bodies('["a","b"]\n["c"]') == ["a", "b", "c"]
    assert bodies("") == []
