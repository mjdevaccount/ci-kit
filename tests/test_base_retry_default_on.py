"""v1.43.0: base-driven retry is ON by default in the reusable pr-autoupdate workflow.

ci-kit#114/#120 (2026-11-21..23): two armed Renovate PRs went red on `test` at a stale base, the queue
held them "behind, a check is failing (the author's fix re-queues it)", and nobody was ever coming:
they sat BEHIND overnight until a session merged main by hand (both went green at once). The fix
existed since v1.36.0 but was OFF unless a caller opted in, and only edge-host had (11-14..11-23:
2 retries, both PRs merged, no cap spent). The workflow input default is what every `@v1` caller
runs, so that default is the switch this pins. The library default (`pr_baseretry.CAP`) stays 0:
a direct CLI run with no env reads nothing, as before.
"""
from __future__ import annotations

import pathlib

import yaml

from ci_kit import pr_autoupdate as upd
from ci_kit import pr_baseretry

WF = pathlib.Path(__file__).resolve().parent.parent / ".github/workflows/pr-autoupdate.yml"


def _inputs() -> dict:
    wf = yaml.safe_load(WF.read_text(encoding="utf-8"))
    return (wf.get(True) or wf.get("on"))["workflow_call"]["inputs"]


def test_the_workflow_turns_base_retry_on_by_default_must_fire():
    ins = _inputs()
    assert ins["base-retry-cap"]["default"] == 1
    assert ins["base-retry-bot-authors"]["default"] == "renovate"


def test_the_default_reaches_the_tool_as_a_valid_cap_must_fire():
    # the workflow exports the input as CI_KIT_BASE_RETRY_CAP; its default must parse, not be refused
    cap, err = upd.base_retry_cap(str(_inputs()["base-retry-cap"]["default"]))
    assert (cap, err) == (1, "")


def test_off_stays_reachable_and_the_library_default_is_unchanged_must_not_fire():
    assert upd.base_retry_cap("0") == (0, "")  # a caller's `base-retry-cap: 0` still turns it off
    assert pr_baseretry.CAP == 0  # a CLI run with no env reads nothing
