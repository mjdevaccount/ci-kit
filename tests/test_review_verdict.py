"""The review gate's verdict policy: every branch, both colours (ci_kit.review.verdict).

The gate exists because a review that cannot block is advisory. So the tests prove it DOES block
-- on DO NOT SHIP, on a self-contradicting SHIP, and on every shape of "did not finish looking" --
and that it passes the two verdicts that are passes.
"""
from __future__ import annotations

import json

import pytest

from ci_kit.review.verdict import MARKER, SCHEMA, decide, main


def _f(severity: str = "blocking", verdict: str = "CONFIRMED") -> dict:
    return {"severity": severity, "verdict": verdict, "file": "ruff.toml", "line": 50,
            "summary": "ban has no test", "failure_scenario": "delete the entry; CI stays green"}


def _raw(ship: str, findings: list[dict], not_reviewed: str = "") -> str:
    return json.dumps({"ship": ship, "findings": findings, "not_reviewed": not_reviewed})


@pytest.mark.parametrize("ship", ["SHIP", "SHIP WITH NOTED RISK"])
def test_the_two_passing_verdicts_pass(ship: str) -> None:
    v = decide(_raw(ship, [_f("risk", "PLAUSIBLE"), _f("note")]), "guard path")
    assert v.result == "pass"
    assert ship in v.title


def test_do_not_ship_blocks() -> None:
    v = decide(_raw("DO NOT SHIP", [_f()]), "guard path")
    assert v.result == "block"
    assert "1 confirmed blocking" in v.title
    assert "ban has no test" in v.body


def test_do_not_ship_on_plausible_findings_blocks_without_claiming_zero_confirmed() -> None:
    v = decide(_raw("DO NOT SHIP", [_f("blocking", "PLAUSIBLE")]), "guard path")
    assert v.result == "block"
    assert "0 confirmed" not in v.title
    assert "DO NOT SHIP" in v.title


def test_a_ship_that_lists_a_confirmed_blocker_blocks() -> None:
    # the verdict is the summary, the finding is the evidence; the summary may not overrule it
    v = decide(_raw("SHIP", [_f()]), "3 commits")
    assert v.result == "block"
    assert "cannot overrule" in v.body


def test_a_plausible_blocker_alone_does_not_block_a_ship() -> None:
    assert decide(_raw("SHIP WITH NOTED RISK", [_f("blocking", "PLAUSIBLE")]), "x").result == "pass"


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", "{not json", "[]", '{"ship": "LGTM", "findings": [], "not_reviewed": ""}',
     '{"ship": "SHIP", "not_reviewed": ""}',
     _raw("SHIP", [{"severity": "blocking"}]),
     _raw("SHIP", [{**_f(), "severity": "critical"}]),
     # severity + verdict valid, a text key missing: the body renderer used to KeyError on it
     _raw("DO NOT SHIP", [{k: v for k, v in _f().items() if k != "failure_scenario"}]),
     _raw("SHIP", [{**_f("note"), "summary": None}])],
    ids=["none", "empty", "blank", "unparseable", "not-object", "bad-ship", "no-findings",
         "finding-missing-fields", "finding-bad-severity", "finding-missing-text",
         "finding-null-summary"],
)
def test_every_did_not_finish_shape_is_blind_and_red(raw: str | None) -> None:
    v = decide(raw, "guard path")
    assert v.result == "blind"
    assert "not a pass" in v.body


def test_body_carries_the_marker_and_the_reasons() -> None:
    v = decide(_raw("SHIP", []), "guard path: ruff.toml; 4 commits")
    assert v.body.startswith(MARKER)
    assert "ruff.toml; 4 commits" in v.body
    assert "_No findings._" in v.body


def test_main_writes_body_and_github_output(tmp_path, monkeypatch) -> None:
    out, body = tmp_path / "gh_out", tmp_path / "body.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("REVIEW_JSON", _raw("DO NOT SHIP", [_f()]))
    assert main(["--reasons", "label", "--body-out", str(body)]) == 0
    assert "result=block" in out.read_text(encoding="utf-8")
    assert MARKER in body.read_text(encoding="utf-8")


def test_main_with_no_review_json_is_blind(tmp_path, monkeypatch) -> None:
    out = tmp_path / "gh_out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.delenv("REVIEW_JSON", raising=False)
    main(["--body-out", str(tmp_path / "b.md")])
    assert "result=blind" in out.read_text(encoding="utf-8")


def test_printed_schema_is_the_parsers_schema(capsys) -> None:
    assert main(["--print-schema"]) == 0
    assert json.loads(capsys.readouterr().out) == SCHEMA


def test_blind_advice_is_a_fresh_commit_never_a_rerun() -> None:
    # a re-run of failed jobs keeps the reusable workflow's OLD ref (td#2948, 2026-11-09: the rerun
    # resolved review-gate.yml@v1 to the pre-setup-node SHA and went BLIND again on exit 127)
    body = decide(None, "x").body
    assert "push a fresh commit" in body
    assert "re-run the job" not in body
