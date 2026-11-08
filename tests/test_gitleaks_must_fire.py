"""ci-gitleaks: the range is the event's own commits, a leak is 1, a scan that looked at nothing is 2.

gitleaks itself is stubbed at `_run` (its real output lines are used): what is under test is the
event -> rev range mapping and the verdict, both of which used to live inside gitleaks-action.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from ci_kit import gitleaks
from ci_kit.gitleaks import LEAK_RC, range_opts, scan_args, verdict

A, B = "a" * 40, "b" * 40
CLEAN = "INF 3 commits scanned.\nINF scanned ~100 bytes in 10ms\nINF no leaks found\n"


def test_pr_range_is_base_to_head():
    ev = {"pull_request": {"base": {"sha": A}, "head": {"sha": B}}}
    assert range_opts("pull_request", ev) == f"--no-merges --first-parent {A}..{B}"


def test_push_range_and_new_branch():
    assert range_opts("push", {"before": A, "after": B}) == f"--no-merges --first-parent {A}..{B}"
    assert range_opts("push", {"before": "0" * 40, "after": B}) == f"--no-merges --first-parent {B}"


def test_merge_group_range():
    ev = {"merge_group": {"base_sha": A, "head_sha": B}}
    assert range_opts("merge_group", ev) == f"--no-merges --first-parent {A}..{B}"


def test_other_events_scan_every_ref():
    assert range_opts("workflow_dispatch", {}) is None
    assert range_opts("schedule", {}) is None


def test_pr_without_shas_is_blind():
    with pytest.raises(SystemExit) as e:
        range_opts("pull_request", {"pull_request": {"base": {"sha": "nope"}}})
    assert e.value.code == 2


def test_scan_args_per_mode():
    assert "--no-git" in scan_args("tree", ".", "", "", None)
    assert "--log-opts=--full-history --diff-filter=tuxdb HEAD" in scan_args("history", ".", "", "", None)
    r = scan_args("range", ".", "cfg.toml", ".gitleaksignore", f"{A}..{B}")
    assert f"--log-opts={A}..{B}" in r and ["--config", "cfg.toml"] == r[r.index("--config"):r.index("--config") + 2]
    assert not any(x.startswith("--log-opts") for x in scan_args("range", ".", "", "", None))
    assert f"--exit-code={LEAK_RC}" in r


@pytest.mark.parametrize(("mode", "rc", "out", "want"), [
    ("range", 0, CLEAN, 0),
    ("range", LEAK_RC, "WRN leaks found: 1\n", 1),          # a leak is a finding
    ("range", 1, "FTL could not run git log\n", 2),         # gitleaks failing is NOT a leak
    ("history", 0, "INF 0 commits scanned.\n", 2),          # looked at nothing
    ("history", 0, "INF no leaks found\n", 2),              # no scanned line at all
    ("tree", 0, "INF no leaks found\n", 0),                 # tree scans files, not commits
])
def test_verdict(mode, rc, out, want):
    assert verdict(mode, rc, out)[0] == want


def test_main_end_to_end_with_stubbed_scanner(tmp_path, monkeypatch):
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps({"pull_request": {"base": {"sha": A}, "head": {"sha": B}}}))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(ev))
    monkeypatch.setenv("CI_KIT_GITLEAKS", "/bin/gitleaks-stub")
    seen = {}

    def fake_run(cmd):
        seen["cmd"] = cmd
        return LEAK_RC, "WRN leaks found: 1\n"

    monkeypatch.setattr(gitleaks, "_run", fake_run)
    assert gitleaks.main(["--mode", "range"]) == 1
    assert seen["cmd"][0] == str(pathlib.Path("/bin/gitleaks-stub"))
    assert f"--log-opts=--no-merges --first-parent {A}..{B}" in seen["cmd"]
    monkeypatch.setattr(gitleaks, "_run", lambda cmd: (0, CLEAN))
    assert gitleaks.main(["--mode", "range"]) == 0


def test_range_without_event_payload_is_blind(monkeypatch):
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    with pytest.raises(SystemExit) as e:
        gitleaks.main(["--mode", "range"])
    assert e.value.code == 2


def test_pin_covers_linux_both_arches():
    assert {"linux-x64", "linux-arm64"} <= set(gitleaks.GITLEAKS.sha256)
    assert set(gitleaks.GITLEAKS.sha256) == set(gitleaks.GITLEAKS.assets)
