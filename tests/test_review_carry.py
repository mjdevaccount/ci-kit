"""The review gate's verdict carry (ci_kit.review.carry), proven in BOTH directions.

The carry must refuse a changed diff as surely as it carries an unchanged one: a guard only ever
seen passing may be passing for the wrong reason. Moved from trade-data's
tests/test_pr_merge_automation.py with the carry itself (v1.15.0).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ci_kit.review import carry
from ci_kit.review.carry import carry_decision, own_diff_hash, sibling_lines


def test_carry_only_a_pass_on_an_identical_own_diff() -> None:
    rec = {"pr": 7, "hash": "abc", "head_sha": "deadbeef", "result": "pass"}
    assert carry_decision("abc", rec) == (True, "verdict carried from deadbeef")
    assert carry_decision("abd", rec)[0] is False
    assert carry_decision("abc", {**rec, "result": "block"})[0] is False
    assert carry_decision("abc", {**rec, "hash": ""})[0] is False
    assert carry_decision("abc", None)[0] is False


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.py").write_text("".join(f"line{i}\n" for i in range(40)))
    (tmp_path / "b.py").write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _commit(repo: Path, path: str, text: str, msg: str) -> str:
    (repo / path).write_text(text)
    _git(repo, "commit", "-qam", msg)
    return _git(repo, "rev-parse", "HEAD")


def test_the_hash_survives_a_base_only_update_and_moves_on_an_own_change(repo: Path) -> None:
    a = (repo / "a.py").read_text()
    _git(repo, "checkout", "-qb", "pr")
    head1 = _commit(repo, "a.py", a.replace("line20\n", "line20 CHANGED\n"), "pr change")
    base1 = _git(repo, "rev-parse", "main")
    h1 = own_diff_hash(base1, head1)
    # main moves in ANOTHER file, then (the updater) merges main into the PR
    _git(repo, "checkout", "-q", "main")
    base2 = _commit(repo, "b.py", "x = 2\n", "main moves")
    _git(repo, "checkout", "-q", "pr")
    _git(repo, "merge", "-q", "--no-edit", "main")
    assert own_diff_hash(base2, _git(repo, "rev-parse", "HEAD")) == h1  # carried
    # the PR's OWN diff changes -> a different hash -> full review
    head3 = _commit(repo, "a.py", (repo / "a.py").read_text().replace("line30\n", "x\n"), "more")
    assert own_diff_hash(base2, head3) != h1


def test_main_editing_a_line_adjacent_to_a_pr_hunk_forces_a_re_review(repo: Path) -> None:
    a = (repo / "a.py").read_text()
    _git(repo, "checkout", "-qb", "pr")
    head1 = _commit(repo, "a.py", a.replace("line20\n", "line20 CHANGED\n"), "pr change")
    h1 = own_diff_hash(_git(repo, "rev-parse", "main"), head1)
    _git(repo, "checkout", "-q", "main")
    base2 = _commit(repo, "a.py", a.replace("line22\n", "line22 MAIN\n"), "main edits context")
    _git(repo, "checkout", "-q", "pr")
    _git(repo, "merge", "-q", "--no-edit", "main")
    assert own_diff_hash(base2, _git(repo, "rev-parse", "HEAD")) != h1


def test_siblings_are_the_open_prs_sharing_a_file_and_never_this_one() -> None:
    prs = [{"number": 1, "title": "me", "files": [{"path": "a.py"}]},
           {"number": 2, "title": "rename" + chr(10) + "flag", "files": [{"path": "a.py"}, {"path": "c.py"}]},
           {"number": 3, "title": "elsewhere", "files": [{"path": "z.py"}]},
           {"number": 4, "title": "no files", "files": None}]
    assert sibling_lines(1, {"a.py", "b.py"}, prs) == ["- #2 (rename flag): a.py"]


def test_a_change_to_a_binary_files_bytes_changes_the_hash(repo: Path) -> None:
    # reviewer 2026-09-26: with `index` lines stripped and no --binary, two different binaries
    # hashed the same and the verdict carried over an unreviewed change
    base = _git(repo, "rev-parse", "main")
    _git(repo, "checkout", "-qb", "pr")
    (repo / "f.bin").write_bytes(bytes([0, 1, 2, 3]) * 50)
    _git(repo, "add", "f.bin")
    _git(repo, "commit", "-qm", "bin 1")
    h1 = own_diff_hash(base, _git(repo, "rev-parse", "HEAD"))
    (repo / "f.bin").write_bytes(bytes([0, 9, 2, 3]) * 50)
    _git(repo, "commit", "-qam", "bin 2")
    assert own_diff_hash(base, _git(repo, "rev-parse", "HEAD")) != h1


class _Done:
    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_the_newest_recorded_verdict_wins_so_a_newer_block_beats_an_older_pass(
        monkeypatch: pytest.MonkeyPatch) -> None:
    records = {"30": None, "20": {"pr": 5, "hash": "H", "head_sha": "b", "result": "block"},
               "10": {"pr": 5, "hash": "H", "head_sha": "a", "result": "pass"}}

    def fake(cmd: list[str], **kw: object) -> _Done:
        if cmd[1:3] == ["run", "list"]:
            assert "completed" in cmd  # a block's run is a FAILURE: scanning only successes hid it
            return _Done(0, json.dumps([{"databaseId": int(r)} for r in ("40", "30", "20", "10")]))
        rid, dest = cmd[3], Path(cmd[cmd.index("-D") + 1])
        if records.get(rid) is None:
            return _Done(1, err="no artifact matches any of the names or patterns provided")
        (dest / "verdict.json").write_text(json.dumps(records[rid]))
        return _Done(0)

    monkeypatch.setattr(carry.subprocess, "run", fake)
    rec = carry.find_record("o/r", "br", 5, current_run="40")
    assert rec is not None and rec["result"] == "block"
    assert carry_decision("H", rec)[0] is False


def test_a_broken_artifact_lookup_is_not_read_as_no_history(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(cmd: list[str], **kw: object) -> _Done:
        if cmd[1:3] == ["run", "list"]:
            return _Done(0, json.dumps([{"databaseId": 7}]))
        return _Done(1, err="HTTP 403: Resource not accessible by integration")

    monkeypatch.setattr(carry.subprocess, "run", fake)
    with pytest.raises(subprocess.CalledProcessError):
        carry.find_record("o/r", "br", 5, current_run="1")


def test_main_moving_lines_far_above_a_pr_hunk_in_the_same_file_still_carries(repo: Path) -> None:
    a = (repo / "a.py").read_text()
    _git(repo, "checkout", "-qb", "pr")
    head1 = _commit(repo, "a.py", a.replace("line30\n", "line30 CHANGED\n"), "pr change")
    h1 = own_diff_hash(_git(repo, "rev-parse", "main"), head1)
    _git(repo, "checkout", "-q", "main")
    base2 = _commit(repo, "a.py", "new0\nnew1\n" + a, "main inserts at the top")
    _git(repo, "checkout", "-q", "pr")
    _git(repo, "merge", "-q", "--no-edit", "main")
    assert own_diff_hash(base2, _git(repo, "rev-parse", "HEAD")) == h1


def test_the_lookup_reads_the_callers_workflow_not_the_kits(monkeypatch: pytest.MonkeyPatch) -> None:
    # a reusable workflow's runs are listed under the CALLER's workflow file; listing the kit's
    # name would find no history and silently re-review every update of main
    seen: list[list[str]] = []

    def fake(cmd: list[str], **kw: object) -> _Done:
        seen.append(cmd)
        return _Done(0, "[]")

    monkeypatch.setattr(carry.subprocess, "run", fake)
    assert carry.find_record("o/r", "br", 5, current_run="1", workflow="ci-review.yml") is None
    assert seen[0][seen[0].index("--workflow") + 1] == "ci-review.yml"
