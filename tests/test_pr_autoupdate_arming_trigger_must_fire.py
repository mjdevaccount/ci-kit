"""Arming auto-merge starts a queue pass -- must fire, both directions (v1.31.0).

pr-autoupdate ran on a push to main or a check workflow finishing. Arming is neither, so a newly
armed PR sat until something else moved (Scraper#161, home-host#245, trade-infra#888 waited for a
hand dispatch, 2026-11-14). ci-kit's own caller now listens for `pull_request_target:
auto_merge_enabled`. The job's guard is EVALUATED here against each event shape, so the guard is
proven to admit what it must and to refuse what it must:

- an arm on a same-repo PR runs a pass; an arm on a FORK's PR does not (pull_request_target hands
  the job the base's secrets -- the token job must never run for a fork);
- a push and a dispatch run; a PR's CI finishing runs unless cancelled; main's own CI does not.

The reusable job must never check out code: pull_request_target is safe only because nothing from
the PR runs.
"""
from __future__ import annotations

import pathlib
import re

import pytest
import yaml

WF = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"
SELF = WF / "pr-autoupdate-self.yml"
REUSABLE = WF / "pr-autoupdate.yml"
REPO = "mjdevaccount/ci-kit"


def _on(doc: dict) -> dict:
    return doc.get("on", doc.get(True))  # PyYAML reads a bare `on` key as True


def _guard(text: str) -> str:
    return " ".join(str(yaml.safe_load(text)["jobs"]["update"]["if"]).split())


def evaluate(expr: str, ctx: dict[str, str]) -> bool:
    """The subset of GitHub's expression language the guard uses: dotted context paths, string
    literals, == / !=, && / || and parentheses. Anything else is a test failure, not a guess."""
    py = expr.replace("&&", " and ").replace("||", " or ")

    def path(m: re.Match) -> str:
        if m.group(0) in ("and", "or"):
            return m.group(0)
        if m.group(0) not in ctx:
            raise AssertionError(f"guard reads {m.group(0)}, which this test does not model")
        return repr(ctx[m.group(0)])

    py = re.sub(r"'[^']*'|[A-Za-z_][\w.]*", lambda m: m.group(0) if m.group(0).startswith("'")
                else path(m), py)
    return bool(eval(py, {"__builtins__": {}}))  # noqa: S307 -- the guard text, pinned in-repo


def ctx(event: str, *, run_event: str = "", conclusion: str = "", head_repo: str = "") -> dict:
    return {
        "github.event_name": event,
        "github.repository": REPO,
        "github.event.workflow_run.event": run_event,
        "github.event.workflow_run.conclusion": conclusion,
        "github.event.pull_request.head.repo.full_name": head_repo,
    }


RUNS = [
    ("an arm on a same-repo PR", ctx("pull_request_target", head_repo=REPO)),
    ("a push to main", ctx("push")),
    ("a hand dispatch", ctx("workflow_dispatch")),
    ("a PR's CI finished", ctx("workflow_run", run_event="pull_request", conclusion="success")),
    ("a PR's CI failed", ctx("workflow_run", run_event="pull_request", conclusion="failure")),
]
SKIPS = [
    ("an arm on a FORK's PR", ctx("pull_request_target", head_repo="someone/ci-kit")),
    ("main's own CI finished", ctx("workflow_run", run_event="push", conclusion="success")),
    ("a superseded PR run", ctx("workflow_run", run_event="pull_request", conclusion="cancelled")),
]


def test_arming_is_a_trigger_and_nothing_else_from_pull_request_target():
    trigger = _on(yaml.safe_load(SELF.read_text(encoding="utf-8")))["pull_request_target"]
    # exactly the arm: `opened`/`synchronize` would run a token job on every PR push for nothing
    assert trigger == {"types": ["auto_merge_enabled"]}


@pytest.mark.parametrize(("why", "event"), RUNS, ids=[r[0] for r in RUNS])
def test_guard_admits(why: str, event: dict):
    assert evaluate(_guard(SELF.read_text(encoding="utf-8")), event), f"{why} must run a pass"


@pytest.mark.parametrize(("why", "event"), SKIPS, ids=[s[0] for s in SKIPS])
def test_guard_refuses(why: str, event: dict):
    assert not evaluate(_guard(SELF.read_text(encoding="utf-8")), event), f"{why} must not run"


def test_dropping_the_fork_clause_is_caught_must_fire():
    text = SELF.read_text(encoding="utf-8")
    clause = "&& github.event.pull_request.head.repo.full_name == github.repository"
    assert clause in _guard(text)
    mutant = _guard(text).replace(clause, "")
    assert evaluate(mutant, SKIPS[0][1]), "the mutant must admit a fork, or this test proves nothing"


def test_the_token_job_checks_out_no_code():
    job = yaml.safe_load(REUSABLE.read_text(encoding="utf-8"))["jobs"]["update"]
    uses = [str(s.get("uses", "")) for s in job["steps"]]
    assert not any(u.startswith("actions/checkout") for u in uses), uses
    assert "pull_request_target: { types: [auto_merge_enabled] }" in REUSABLE.read_text(encoding="utf-8")
