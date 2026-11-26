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
import os
import subprocess

import pytest
from conftest import git, write
from test_heal_ladder_must_fire import NOW, ON, heal_mark, pr

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


# ── review (cab3410): what the first cut got wrong ────────────────────────────────────────────
# A command that RAN and went red, in a repository whose own target wraps bwrap. Its output has a
# line that starts `bwrap: ` -- after the lines the command itself printed first.
RED_SELFTEST = NL.join(("Exit code 2", "sandbox-selftest: starting",
                        "bwrap: Can't find source path /nope/rootfs: No such file or directory",
                        "make: *** [Makefile:4: sandbox-test] Error 1"))


def test_a_red_command_that_printed_its_own_bwrap_line_is_the_prs_must_not_fire() -> None:
    red = stream(use("t1"), res("t1", RED_SELFTEST, True), use("t2"), res("t2", RED_SELFTEST, True))
    assert pr_fix.sandbox_refused(red) == ""
    for said in ("declined", "fixed"):         # the model's own answer stands: this is one PR's red
        assert pr_fix.judge(red + NL + json.dumps(answer(said)), "", 0)[0] == said
    # the other measured sentence, quoted by a command's own log, is not the refusal either
    quoted = NL.join(("Exit code 1", "FAILED tests/test_sandbox.py::test_message", "Sandbox is required but failed to initialize"))
    assert pr_fix.sandbox_refused(stream(use("t1"), res("t1", quoted, True))) == ""


def test_the_refusal_is_the_body_beginning_with_the_sandboxs_words_must_fire() -> None:
    for text in (NO_USERNS, f"Exit code 1{NL}{NO_USERNS}", f"Exit code 137{NL}bwrap: pivot_root: Operation not permitted",
                 NO_SOCAT, f"Exit code 1{NL}{NO_SOCAT}"):
        assert "ran none of its 1 Bash command(s)" in pr_fix.sandbox_refused(stream(use("t1"), res("t1", text, True))), text


def test_the_probe_runs_bounded_in_the_scrubbed_env_with_no_credential(monkeypatch, capsys, new_repo, tmp_path) -> None:
    """Through main() and the REAL sandbox_check: only `which` and `run` are stood in."""
    seen: list[dict] = []
    paths: list[object] = []
    real = pr_fix.sandbox_check

    def which(tool, path=None):
        paths.append(path)
        return f"/usr/bin/{tool}"

    def run(argv, **kw):
        seen.append({"argv": argv, **kw})
        return _Done(1, err=NO_USERNS)
    r, state = _prepared(new_repo, tmp_path)
    monkeypatch.chdir(r)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "not-a-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-key")
    monkeypatch.setenv("GH_TOKEN", "not-a-gh-token")   # the scrub must be proven here, not by a variable CI happens to export
    monkeypatch.setattr(pr_fix, "sandbox_check", lambda environ: real(environ, which, run))
    monkeypatch.setattr(pr_fix, "run_cli", lambda *a: (_ for _ in ()).throw(AssertionError("the CLI ran")))
    assert pr_fix.main(["run", "--state-file", str(state), "--out", str(tmp_path / "heal"), "--model", "m"]) == 0
    assert "why=KIND=cannot-start: bubblewrap cannot build a sandbox" in capsys.readouterr().out
    (call,) = seen
    assert call["argv"] == list(pr_fix.SANDBOX_PROBE)
    assert call["timeout"] == 30                       # a probe that hangs must not hold the sealed runner
    env = call["env"]                                  # the env the CLI's commands would get, never this process's
    assert env["PATH"] == os.environ["PATH"] and paths == [env["PATH"]] * len(pr_fix.SANDBOX_TOOLS)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
    assert not [k for k in env if pr_fix.SECRETISH.search(k)], sorted(env)
    assert "not-a-token" not in env.values() and "not-a-key" not in env.values()
    assert "GH_TOKEN" not in env and "not-a-gh-token" not in env.values()


def test_a_cli_that_is_not_installed_is_the_runners_must_fire() -> None:
    state, _, why = pr_fix.judge("", "FileNotFoundError: [Errno 2] No such file or directory: 'claude'", pr_fix.NOT_RUN)
    assert state == "failed" and why.startswith("KIND=cannot-start exit=not-run:")
    assert "CLI is not installed on this runner" in why and "FileNotFoundError" in why
    assert pr_ladder.outcome(True, "eligible", "nothing", "", "failed", why, "", "", True)[1] == "cannot-start"


def test_a_cli_that_ran_and_died_is_not_called_uninstalled_must_not_fire() -> None:
    for code in ("timeout", 1, 137):
        state, _, why = pr_fix.judge("", "timed out after 2400s", code)
        assert state == "failed" and not why.startswith("KIND=cannot-start"), why


def _down(head: str) -> list[str]:
    return [heal_mark(head), pr_ladder.HEALED.format(head=head, tier="claude", result="cannot-start")]


def test_a_cannot_start_does_not_spend_the_prs_tier_3_must_fire() -> None:
    """Two heads met a runner that could not start tier 3; the runner is fixed; the PR moves again."""
    bodies = [*_down("c" * 40), *_down("d" * 40)]
    action, claude, why = pr_ladder.decide(pr(), bodies, ON, 1, NOW)
    assert (action, claude, why) == ("dispatch", True, "tiers 1-3")
    # ... and one honest tier-3 attempt beside them is one spent, not three
    honest = [heal_mark("e" * 40), pr_ladder.HEALED.format(head="e" * 40, tier="claude", result="declined")]
    assert pr_ladder.decide(pr(), [*bodies, *honest], ON, 1, NOW)[:2] == ("dispatch", True)


def test_a_cannot_start_head_is_not_retried_and_real_attempts_still_count_must_not_fire() -> None:
    # the SAME head stays quiet: a runner that is still broken is not re-dispatched every pass
    action, claude, why = pr_ladder.decide(pr(head="c" * 40), _down("c" * 40), ON, 1, NOW)
    assert (action, claude) == ("quiet", False) and "already dispatched at this head" in why
    # attempts that RAN still spend the cap, whatever they ended as
    for result in ("failed", "declined", "refused", "pushed"):
        ran = [x for h in ("c" * 40, "d" * 40) for x in (heal_mark(h), pr_ladder.HEALED.format(head=h, tier="claude", result=result))]
        action, claude, why = pr_ladder.decide(pr(), ran, ON, 1, NOW)
        assert (action, claude) == ("dispatch", False) and "spent 2/2" in why, result
    # a dispatch with no record yet (in flight, or died) is counted: only a RECORDED cannot-start is forgiven
    assert pr_ladder.decide(pr(), [heal_mark("c" * 40), heal_mark("d" * 40)], ON, 1, NOW)[:2] == ("dispatch", False)
    # the total cap is the push-loop brake and is not this rule's: six heads are six heads
    six = [x for c in "cdef01" for x in _down(c * 40)]
    assert pr_ladder.decide(pr(), six, ON, 1, NOW)[0] == "capped"


# A command outside --allowedTools, as the pinned CLI (2.1.329) reports it under the env scrub with
# no approval surface: captured beside two sandbox refusals in one session (scripted loopback API).
DENIED = ("Permission for this tool use was denied. It requires approval, and this session has no approval surface "
          "\u2014 nobody can answer a permission prompt here \u2014 so it was denied automatically. The action was NOT "
          "performed; do not claim it succeeded, and do not retry it: this action, and anything else that requires "
          "approval, will be denied the same way for the rest of this session. Tell the user what was blocked and why "
          "you needed it, then continue with the parts of the task that do not require approval. What required "
          "approval: This command requires approval")
REFUSED = f"Exit code 1{NL}{NO_USERNS}"


def denials(*ids: str, action: str = "declined") -> dict:
    return {**answer(action), "permission_denials": [{"tool_name": "Bash", "tool_use_id": i,
                                                      "tool_input": {"command": "curl -s http://127.0.0.1:9/x"}} for i in ids]}


def test_a_denied_command_beside_refusals_is_still_cannot_start_must_fire() -> None:
    captured = stream(use("t1"), res("t1", DENIED, True), use("t2"), res("t2", REFUSED, True),
                      use("t3"), res("t3", REFUSED, True), denials("t1"))
    assert "ran none of its 2 Bash command(s)" in pr_fix.sandbox_refused(captured)
    assert pr_fix.judge(captured, "", 0)[2].startswith("KIND=cannot-start exit=0:")
    # each half of the capture is enough alone: the result event's list (a denial reworded by a later
    # CLI), and the sentence (a session that timed out before its result event)
    by_id = stream(use("t1"), res("t1", "This command requires approval", True), use("t2"), res("t2", REFUSED, True), denials("t1"))
    assert "ran none of its 1 Bash command(s)" in pr_fix.sandbox_refused(by_id)
    by_text = stream(use("t1"), res("t1", DENIED, True), use("t2"), res("t2", REFUSED, True))
    assert "ran none of its 1 Bash command(s)" in pr_fix.sandbox_refused(by_text)


def test_denials_alone_or_beside_a_command_that_ran_say_nothing_must_not_fire() -> None:
    alone = stream(use("t1"), res("t1", DENIED, True), use("t2"), res("t2", DENIED, True), denials("t1", "t2"))
    assert pr_fix.sandbox_refused(alone) == "" and pr_fix.judge(alone, "", 0)[0] == "declined"
    ran = stream(use("t1"), res("t1", DENIED, True), use("t2"), res("t2", REFUSED, True),
                 use("t3"), res("t3", "3 passed", False), denials("t1"))
    assert pr_fix.sandbox_refused(ran) == ""
    # a denial the list names for ANOTHER call does not excuse this one's red
    other = stream(use("t1"), res("t1", f"Exit code 1{NL}FAILED tests/test_x.py::test_y", True), use("t2"), res("t2", REFUSED, True),
                   denials("t9"))
    assert pr_fix.sandbox_refused(other) == ""


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
