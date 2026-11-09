"""ci-gitleaks: the range is the event's own commits, a leak is 1, a scan that looked at nothing is 2.

gitleaks itself is stubbed at `_run` (its real output lines are used): what is under test is the
event -> rev range mapping and the verdict, both of which used to live inside gitleaks-action.
"""
from __future__ import annotations

import json
import pathlib
import subprocess

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


@pytest.mark.parametrize(("rc", "out", "matched", "want"), [
    (0, "INF 0 commits scanned.\n", 1, 0),      # delete/rename-only PR: git sees 1, nothing to find
    (0, CLEAN, 0, 2),                           # the range matched nothing
    (LEAK_RC, "WRN leaks found: 1\n", 1, 1),    # a leak still wins
    (1, "FTL bad revision\n", 3, 2),            # gitleaks failing still is not a pass
])
def test_range_verdict_judges_git_count(rc, out, matched, want):
    assert verdict("range", rc, out, matched)[0] == want


def _git(d, *a):
    subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True)


def test_commits_in_counts_a_delete_only_commit(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "f").write_text("x\n")
    _git(tmp_path, "add", "f")
    _git(tmp_path, "commit", "-qm", "add")
    base = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    _git(tmp_path, "rm", "-q", "f")
    _git(tmp_path, "commit", "-qm", "delete")
    assert gitleaks.commits_in(str(tmp_path), f"--no-merges --first-parent {base}..HEAD") == 1
    assert gitleaks.commits_in(str(tmp_path), f"--no-merges --first-parent {base}..{base}") == 0
    with pytest.raises(SystemExit) as e:  # an unknown rev (an orphaned force-push base) is blind
        gitleaks.commits_in(str(tmp_path), f"--no-merges --first-parent {'f' * 40}..HEAD")
    assert e.value.code == 2


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
    monkeypatch.setattr(gitleaks, "commits_in", lambda source, opts: 2)
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


# 2026-11-09: a baseline entry the scan can never produce is refused (TradingEdge.Scraper#156).
SCRAPER = "/repo/tools/cookie-capture-extension/manifest.json:generic-api-key:6"
LIVE = "tools/cookie-capture-extension/manifest.json:generic-api-key:6"


@pytest.mark.parametrize("mode", ["range", "history", "tree"])
def test_old_docker_mount_prefix_is_dead(mode):
    for entry in (SCRAPER, f"{A}:{SCRAPER}"):
        dead = gitleaks.dead_entries(f"# old scan\n\n{entry}\n", mode, ".")
        assert len(dead) == 1 and "/repo/" in dead[0], (mode, entry, dead)


@pytest.mark.parametrize("mode", ["range", "history", "tree"])
def test_repo_relative_entries_are_live(mode):
    text = f"# baseline\n{LIVE}\n{A}:{LIVE}\n{A}:docs/a:b.md:curl-auth-user:200\n"
    assert gitleaks.dead_entries(text, mode, ".") == []


def test_absolute_entry_lives_only_under_an_absolute_tree_source():
    assert gitleaks.dead_entries(SCRAPER, "tree", "/repo") == []           # finance-ops' own docker shape
    assert gitleaks.dead_entries("/repository/x:r:1", "tree", "/repo")     # a sibling prefix is not under it
    assert gitleaks.dead_entries(SCRAPER, "history", "/repo")              # git modes report repo-relative paths
    assert gitleaks.dead_entries("C:/repo/x:r:1", "range", ".")            # a Windows absolute path too


def test_malformed_entry_is_dead():
    assert gitleaks.dead_entries("just-a-word\n", "range", ".")


def _main_with_ignore(tmp_path, monkeypatch, body):
    ign = tmp_path / ".gitleaksignore"
    if body is not None:
        ign.write_text(body)
    monkeypatch.setenv("CI_KIT_GITLEAKS", "/bin/gitleaks-stub")
    ran = []
    monkeypatch.setattr(gitleaks, "_run", lambda cmd: (ran.append(cmd), (0, "INF no leaks found\n"))[1])
    return ran, ["--mode", "tree", "--ignore-path", str(ign)]


@pytest.mark.parametrize("body", [f"{SCRAPER}\n", None])   # a dead entry; a missing file
def test_main_refuses_a_dead_baseline_before_scanning(tmp_path, monkeypatch, body):
    ran, argv = _main_with_ignore(tmp_path, monkeypatch, body)
    with pytest.raises(SystemExit) as e:
        gitleaks.main(argv)
    assert e.value.code == 2 and not ran                       # refused, and gitleaks never ran


def test_main_scans_with_a_live_baseline(tmp_path, monkeypatch):
    ran, argv = _main_with_ignore(tmp_path, monkeypatch, f"{LIVE}\n")
    assert gitleaks.main(argv) == 0 and ran


# Reviewer 572 (2026-11-09): every file gitleaks LOADS is judged, and four more dead shapes.
def test_trailing_comment_and_bad_line_field_are_dead():
    assert gitleaks.dead_entries(f"{LIVE} # rotated 11-08\n", "range", ".")
    assert gitleaks.dead_entries("a.md:rule:two\n", "range", ".")
    assert gitleaks.dead_entries(f"{LIVE}\n", "range", ".") == []


def test_backslash_paths_follow_gitleaks_rewrite():
    # gitleaks rewrites \ to / for 3/4-field entries: a relative one stays live, an absolute one dies
    assert gitleaks.dead_entries("tools\\cookie\\manifest.json:generic-api-key:6\n", "range", ".") == []
    assert gitleaks.dead_entries(f"{A}:tools\\x.json:generic-api-key:6\n", "range", ".") == []
    assert gitleaks.dead_entries("\\repo\\tools\\x.json:generic-api-key:6\n", "range", ".")
    assert gitleaks.dead_entries("C:\\repo\\x.json:generic-api-key:6\n", "range", ".")
    # a 5-field entry is not rewritten, so a backslash left in it can never match a '/' path
    assert gitleaks.dead_entries(f"{A}:docs\\a:b.md:curl-auth-user:200\n", "range", ".")


def test_bom_kills_a_fingerprint_but_not_a_comment():
    assert gitleaks.dead_entries(f"\ufeff# baseline\n{LIVE}\n", "range", ".") == []
    assert gitleaks.dead_entries("\ufeff\n", "range", ".") == []
    assert gitleaks.dead_entries(f"\ufeff{LIVE}\n", "range", ".")


def _stub_scan(monkeypatch):
    monkeypatch.setenv("CI_KIT_GITLEAKS", "/bin/gitleaks-stub")
    ran = []
    monkeypatch.setattr(gitleaks, "_run", lambda cmd: (ran.append(cmd), (0, "INF no leaks found\n"))[1])
    return ran


@pytest.mark.parametrize("where", ["cwd", "source"])
def test_unnamed_default_baselines_are_judged(tmp_path, monkeypatch, where):
    """No --ignore-path (trade-infra's caller): gitleaks still loads ./ and <source>/.gitleaksignore."""
    src, cwd = tmp_path / "src", tmp_path / "cwd"
    src.mkdir()
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    (cwd if where == "cwd" else src).joinpath(".gitleaksignore").write_text(f"{SCRAPER}\n", encoding="utf-8")
    ran = _stub_scan(monkeypatch)
    with pytest.raises(SystemExit) as e:
        gitleaks.main(["--mode", "tree", "--source", str(src)])
    assert e.value.code == 2 and not ran
    (cwd if where == "cwd" else src).joinpath(".gitleaksignore").write_text(f"{LIVE}\n", encoding="utf-8")
    assert gitleaks.main(["--mode", "tree", "--source", str(src)]) == 0 and ran


def test_no_baseline_anywhere_is_fine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ran = _stub_scan(monkeypatch)
    assert gitleaks.main(["--mode", "tree", "--source", "."]) == 0 and ran


def test_a_folder_ignore_path_reads_the_file_inside(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "cfg"
    d.mkdir()
    ran = _stub_scan(monkeypatch)
    with pytest.raises(SystemExit) as e:                    # a named folder holding no baseline
        gitleaks.main(["--mode", "tree", "--ignore-path", str(d)])
    assert e.value.code == 2 and not ran
    (d / ".gitleaksignore").write_text(f"{SCRAPER}\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:                    # its dead entry is judged
        gitleaks.main(["--mode", "tree", "--ignore-path", str(d)])
    assert e.value.code == 2 and not ran
    (d / ".gitleaksignore").write_text(f"{LIVE}\n", encoding="utf-8")
    assert gitleaks.main(["--mode", "tree", "--ignore-path", str(d)]) == 0 and ran


def test_named_file_and_source_default_are_both_judged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    named = tmp_path / "base.ignore"
    named.write_text(f"{LIVE}\n", encoding="utf-8")
    (src / ".gitleaksignore").write_text(f"{SCRAPER}\n", encoding="utf-8")
    ran = _stub_scan(monkeypatch)
    with pytest.raises(SystemExit) as e:
        gitleaks.main(["--mode", "tree", "--source", str(src), "--ignore-path", str(named)])
    assert e.value.code == 2 and not ran


def test_same_file_twice_is_read_once(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".gitleaksignore").write_text(f"{LIVE}\n", encoding="utf-8")
    assert len(gitleaks.loaded_baselines("", ".")) == 1      # ./ and <source>=. are one file
