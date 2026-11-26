"""A tier that CANNOT START is not a tier that failed (ci_kit/pr_fix.py, ci_kit/pr_ladder.py) -- both directions.

THE FAULT (2026-11-15..26): 17 tier-3 attempts on 15 PRs ended `Claude Code could not start:
bubblewrap is required for subprocess env scrubbing and isolation`. Each was recorded `tier=claude
result=failed` and paged as "#N: heal ladder could not fix it", the title an honest decline carries.

MUST FIRE: a runner with no bwrap, no socat, or a bwrap that cannot create a namespace is refused
BEFORE the CLI runs; the CLI's own "could not start" and a run whose every Bash call the sandbox
refused are the same kind; the record's result, its paged marker and the page's title say
`cannot-start` and name the runner.
MUST NOT FIRE: a runner that can build the sandbox runs the CLI; one Bash call that ran means the
sandbox works, whatever else failed; an ordinary failed fix stays `failed` under the old title.

The stream fixtures are the CLI's real events (2.1.329, `--output-format stream-json`, in the
runner's base image under Docker's default seccomp profile, then with bwrap and no socat).
"""
from __future__ import annotations

import json
import subprocess

import pytest
from conftest import git, write

from ci_kit import pr_fix, pr_ladder

NL = chr(10)
H1 = "a" * 40
NO_USERNS = ("bwrap: No permissions to create new namespace, likely because the kernel does not allow "
             "non-privileged user namespaces. See <https://deb.li/bubblewrap> or "
             "<file:///usr/share/doc/bubblewrap/README.Debian.gz>.")
NO_SOCAT = ("Sandbox is required but failed to initialize: Sandbox dependencies not available: socat not installed. "
            "Restart to retry. Running it outside the sandbox (dangerouslyDisableSandbox) is not allowed either.")
NO_BWRAP = ("Claude Code could not start: bubblewrap is required for subprocess env scrubbing and isolation. Install "
            "with: sudo apt-get install -y bubblewrap, set sandbox.bwrapPath in managed settings, or set "
            "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=0 to disable (loses subprocess isolation).")


class _Done:
    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, err


def use(i: str, name: str = "Bash") -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": i, "name": name,
                                                           "input": {"command": "uv run pytest -q"}}]}}


def res(i: str, text: object, error: bool) -> dict:
    return {"type": "user", "message": {"content": [{"type": "tool_result", "content": text, "is_error": error,
                                                      "tool_use_id": i}]}}


def answer(action: str) -> dict:
    return {"type": "result", "subtype": "success", "is_error": False,
            "structured_output": {"action": action, "summary": "s", "reason": "r"}}


def stream(*events: dict) -> str:
    return NL.join(json.dumps(e) for e in events)


# ── the runner is asked first ─────────────────────────────────────────────────────────────────
def _which(present: set[str]):
    return lambda tool, path=None: f"/usr/bin/{tool}" if tool in present else None


def test_a_runner_without_bwrap_or_socat_is_refused_before_the_cli_must_fire() -> None:
    def never(*a, **k):
        raise AssertionError("the probe ran with a tool missing")
    assert "bwrap and socat not installed" in pr_fix.sandbox_check({"PATH": "/usr/bin"}, _which(set()), never)
    why = pr_fix.sandbox_check({"PATH": "/usr/bin"}, _which({"bwrap"}), never)
    assert "socat not installed" in why and "bwrap" not in why.split("not installed")[0]


def test_a_bwrap_that_cannot_create_a_namespace_is_refused_must_fire() -> None:
    seen: list[list[str]] = []

    def run(argv, **k):
        seen.append(argv)
        return _Done(1, err=NO_USERNS)
    why = pr_fix.sandbox_check({"PATH": "/usr/bin"}, _which({"bwrap", "socat"}), run)
    assert "cannot build a sandbox" in why and "No permissions to create new namespace" in why
    # the floor the CLI's sandbox stands on: its own user and pid namespace, the userns lock (the one
    # that fails on a masked /proc/sys with seccomp already lifted), and a fresh /proc
    assert {"--unshare-user", "--unshare-pid", "--disable-userns", "--proc"} <= set(seen[0]) and seen[0][0] == "bwrap"


@pytest.mark.parametrize("boom", [OSError("exec format error"), subprocess.TimeoutExpired("bwrap", 30)])
def test_a_probe_that_did_not_run_is_refused_never_passed_must_fire(boom) -> None:
    def run(argv, **k):
        raise boom
    assert "did not run" in pr_fix.sandbox_check({"PATH": "/usr/bin"}, _which({"bwrap", "socat"}), run)


def test_a_runner_that_builds_the_sandbox_is_not_refused_must_not_fire() -> None:
    assert pr_fix.sandbox_check({"PATH": "/usr/bin"}, _which({"bwrap", "socat"}), lambda argv, **k: _Done(0)) == ""


# ── what the CLI itself said ──────────────────────────────────────────────────────────────────
def test_every_bash_call_refused_by_the_sandbox_is_cannot_start_must_fire() -> None:
    for text in (f"Exit code 1{NL}{NO_USERNS}", NO_SOCAT, [{"type": "text", "text": NO_SOCAT}]):
        said = pr_fix.sandbox_refused(stream(use("t1"), res("t1", text, True), use("t2"), res("t2", text, True)))
        assert "ran none of its 2 Bash command(s)" in said


def test_one_bash_call_that_ran_means_the_sandbox_works_must_not_fire() -> None:
    refused = f"Exit code 1{NL}{NO_USERNS}"
    assert pr_fix.sandbox_refused(stream(use("t1"), res("t1", refused, True), use("t2"), res("t2", "3 passed", False))) == ""
    # an ordinary red command is the command's own failure
    assert pr_fix.sandbox_refused(stream(use("t1"), res("t1", f"Exit code 1{NL}FAILED tests/test_x.py::test_y", True))) == ""
    # a repository that is ABOUT bwrap: the words in a file it read, or in a command that ran
    assert pr_fix.sandbox_refused(stream(use("r1", "Read"), res("r1", NO_USERNS, False))) == ""
    assert pr_fix.sandbox_refused(stream(use("t1"), res("t1", NO_USERNS, False))) == ""
    assert pr_fix.sandbox_refused(stream(answer("declined"))) == ""           # no Bash call: nothing to read
    assert pr_fix.sandbox_refused("not json" + NL + "[1, 2]") == ""


def test_the_judge_names_cannot_start_over_the_models_own_answer_must_fire() -> None:
    state, _, why = pr_fix.judge("", NO_BWRAP, 1)                             # the 17 recorded failures, verbatim
    assert state == "failed" and why.startswith("KIND=cannot-start exit=1:") and "bubblewrap is required" in why
    refused = f"Exit code 1{NL}{NO_USERNS}"
    for said in ("declined", "fixed"):   # a decline that only says "my commands failed", a fix nothing proved
        state, _, why = pr_fix.judge(stream(use("t1"), res("t1", refused, True), answer(said)), "", 0)
        assert state == "failed" and why.startswith("KIND=cannot-start exit=0:") and "ran none" in why


def test_the_judge_leaves_an_ordinary_run_alone_must_not_fire() -> None:
    ran = stream(use("t1"), res("t1", "3 passed", False), answer("fixed"))
    assert pr_fix.judge(ran, "", 0)[0] == "fixed"
    assert pr_fix.judge(stream(answer("declined")), "", 0)[0] == "declined"
    state, _, why = pr_fix.judge("", "boom", 1)
    assert state == "failed" and why.startswith("KIND=error")
    # a run that reached a result is judged by it: "could not start" in its stderr is not the CLI's startup
    assert pr_fix.judge(stream(answer("declined")), "the dev server could not start", 0)[0] == "declined"


# ── the fix step: refused before a model session is spent ─────────────────────────────────────
def _prepared(new_repo, tmp_path) -> tuple:
    r = new_repo()
    write(r / "a.py", "x = 1" + NL)
    git(r, "add", "-A")
    git(r, "commit", "-qm", "base")
    base = git(r, "rev-parse", "HEAD").stdout.strip()
    write(r / "a.py", "x = 2" + NL)
    git(r, "commit", "-qam", "pr")
    head = git(r, "rev-parse", "HEAD").stdout.strip()
    state = tmp_path / "fix.json"
    state.write_text(json.dumps(pr_fix.Prepared("red", base, head).__dict__), encoding="utf-8")
    return r, state


def _run(monkeypatch, capsys, new_repo, tmp_path, check: str, cli) -> str:
    r, state = _prepared(new_repo, tmp_path)
    monkeypatch.chdir(r)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "not-a-token")
    monkeypatch.setattr(pr_fix, "sandbox_check", lambda environ: check)
    monkeypatch.setattr(pr_fix, "run_cli", cli)
    assert pr_fix.main(["run", "--state-file", str(state), "--out", str(tmp_path / "heal"), "--model", "m"]) == 0
    return capsys.readouterr().out


def test_the_fix_step_never_starts_the_cli_on_a_runner_that_cannot_must_fire(monkeypatch, capsys, new_repo, tmp_path) -> None:
    def cli(*a):
        raise AssertionError("the CLI ran on a runner that cannot build its sandbox")
    out = _run(monkeypatch, capsys, new_repo, tmp_path, "bwrap not installed on this runner", cli)
    assert "state=failed" in out and "why=KIND=cannot-start: bwrap not installed on this runner" in out
    assert "::error::tier 3 cannot start on this runner" in out


def test_the_fix_step_runs_the_cli_on_a_runner_that_can_must_not_fire(monkeypatch, capsys, new_repo, tmp_path) -> None:
    out = _run(monkeypatch, capsys, new_repo, tmp_path, "", lambda *a: ("declined", "", "needs a test changed"))
    assert "state=declined" in out and "cannot-start" not in out and "::error::" not in out


def test_the_probe_is_never_handed_the_credential(monkeypatch, capsys, new_repo, tmp_path) -> None:
    seen: list[dict] = []
    r, state = _prepared(new_repo, tmp_path)
    monkeypatch.chdir(r)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "not-a-token")
    monkeypatch.setattr(pr_fix, "sandbox_check", lambda environ: seen.append(environ) or "no")
    pr_fix.main(["run", "--state-file", str(state), "--out", str(tmp_path / "heal"), "--model", "m"])
    assert seen and "CLAUDE_CODE_OAUTH_TOKEN" not in seen[0] and "ANTHROPIC_API_KEY" not in seen[0]


# ── the record and the page ───────────────────────────────────────────────────────────────────
def test_cannot_start_is_its_own_result_never_failed_must_fire() -> None:
    assert pr_ladder.CANNOT_START == pr_fix.CANNOT_START
    why = f"KIND=cannot-start exit=1: {NO_BWRAP}"
    tier, result, what, page = pr_ladder.outcome(True, "eligible", "nothing", "", "failed", why, "", "", True)
    assert (tier, result, page) == ("claude", "cannot-start", True)
    assert "TIER 3 CANNOT START on this runner" in what and "not this PR's failure" in what
    # the markers the pass and a search read still parse
    assert pr_ladder.marks([pr_ladder.HEALED.format(head=H1, tier=tier, result=result)], pr_ladder._HEALED) == [(H1, tier, result)]
    assert pr_ladder.marks([pr_ladder.PAGED.format(head=H1, why=result)], pr_ladder._PAGED) == [(H1, result)]


def test_an_ordinary_failed_fix_stays_failed_must_not_fire() -> None:
    for why in ("KIND=error exit=1: boom", "KIND=rate-limit exit=1: usage limit", "the CLI said KIND=cannot-start once"):
        assert pr_ladder.outcome(True, "eligible", "nothing", "", "failed", why, "", "", True)[1] == "failed"
    assert pr_ladder.outcome(True, "eligible", "nothing", "", "declined", "KIND=cannot-start", "", "", True)[1] == "declined"


def _report(result_why: str) -> tuple[list[tuple[str, str]], str]:
    pages: list[tuple[str, str]] = []
    posted: list[str] = []
    o = pr_ladder.outcome(True, "eligible", "nothing", "", "failed", result_why, "", "", True)
    errors = pr_ladder.report("o/r", 7, H1, o, "unit tests", "u", "run", lambda *a, input_text=None: posted.append(input_text) or _Done(),
                              lambda r, n: [], lambda title, body, url: pages.append((title, body)) or "published (200)")
    assert errors == 0
    return pages, posted[0]


def test_the_page_names_the_runner_not_the_pr_must_fire() -> None:
    pages, comment = _report(f"KIND=cannot-start: {NO_USERNS}")
    title, body = pages[0]
    assert title == "r: heal tier 3 CANNOT START (the runner, every PR)" and "could not fix it" not in title
    assert "Seen on #7" in body and "Every tier-3 heal in this repo fails this way" in body
    assert f"<!-- smart-merge:healed head={H1} tier=claude result=cannot-start -->" in comment
    assert f"<!-- smart-merge:paged head={H1} why=cannot-start -->" in comment


def test_an_ordinary_failure_keeps_its_page_must_not_fire() -> None:
    pages, comment = _report("KIND=error exit=1: boom")
    assert pages[0][0] == "r #7: heal ladder could not fix it"
    assert "result=failed" in comment and "cannot-start" not in comment


def test_a_cannot_start_turns_the_heal_run_red(monkeypatch, capsys) -> None:
    from ci_kit import ntfy, pr_heal
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr(pr_heal, "write", lambda *a, input_text=None: _Done())
    monkeypatch.setattr(pr_heal, "comments_of", lambda r, n: [])
    monkeypatch.setattr(ntfy, "page", lambda title, body, url: "published (200)")
    argv = ["report", "--repo", "o/r", "--pr", "7", "--head", H1, "--go", "true", "--plan-why", "eligible", "--regen", "nothing",
            "--claude", "true", "--fix", "failed"]
    assert pr_ladder.main([*argv, "--fix-why", "KIND=cannot-start: bwrap not installed on this runner"]) == 1
    assert "result=cannot-start" in capsys.readouterr().out
