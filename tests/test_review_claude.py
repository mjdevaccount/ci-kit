"""The review gate's subscription transport (ci_kit.review.claude + mcp_server), no network.

What must hold for the gate to stay a gate on the subscription: the CLI gets NO built-in tool and
only the agent module's four, the API key never reaches it (in `-p` the key outranks the OAuth
token and would bill the API account), a verdict lands as `structured_output` the verdict step
passes, and every failure lands as a `cause` whose KIND says what to do -- wait out a rate limit,
re-mint the token, or top up -- with the token scrubbed.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from ci_kit.review import claude as rgc
from ci_kit.review import mcp_server as rgm
from ci_kit.review import tools
from ci_kit.review.verdict import SCHEMA, decide

VERDICT = {"ship": "SHIP", "findings": [], "not_reviewed": "nothing"}
INIT = {"type": "system", "subtype": "init", "mcp_servers": [{"name": "review", "status": "connected"}],
        "tools": ["StructuredOutput", *(f"mcp__review__{t}" for t in tools.TOOLS)]}


def _stream(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _outputs(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


def _argv() -> list[str]:
    return rgc.cli_argv("claude-sonnet-5-5", json.dumps(SCHEMA), Path.cwd())


def test_cli_gets_no_builtin_tool_and_only_the_four_review_tools() -> None:
    argv = _argv()
    assert argv[argv.index("--tools") + 1] == ""
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert sorted(allowed) == sorted(f"mcp__review__{t}" for t in tools.TOOLS)
    # the MCP server is launched from the kit's own interpreter, never a path in the caller's checkout
    config = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]["review"]
    assert config["args"] == ["-m", "ci_kit.review.mcp_server"]
    assert {"--restricted", "--strict-mcp-config", "--json-schema"} <= set(argv)
    # --bare would not read CLAUDE_CODE_OAUTH_TOKEN: the subscription run would have no credential
    assert "--bare" not in argv


def test_the_api_key_never_reaches_the_cli() -> None:
    env = rgc.cli_env({"ANTHROPIC_API_KEY": "k", "GH_TOKEN": "g", "GITHUB_TOKEN": "g",
                       "CLAUDE_CODE_OAUTH_TOKEN": "o", "PATH": "/bin"})
    assert "ANTHROPIC_API_KEY" not in env and "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "o"


@pytest.mark.parametrize(("categories", "text", "kind"), [
    (["rate_limit"], "", "rate-limit"),
    (["overloaded", "authentication_failed"], "", "auth-rejected"),   # the newest category wins
    (["billing_error"], "", "credit"),
    ([], "Claude AI usage limit reached|1762444800", "rate-limit"),
    ([], "Credit balance is too low", "credit"),
    ([], "Invalid API key · Please run /login", "auth-rejected"),
    ([], "OAuth token has expired", "auth-rejected"),
    ([], "something else broke", "error"),
])
def test_failure_kind_names_what_to_do_next(categories, text, kind) -> None:
    assert rgc.failure_kind(categories, text) == kind


def test_a_verdict_lands_as_structured_output_the_verdict_step_accepts() -> None:
    out = _stream(INIT, {"type": "result", "subtype": "success", "is_error": False, "structured_output": VERDICT})
    key, value = rgc.judge(out, "", 0)
    assert key == "structured_output"
    assert decide(value, "guard path").result == "pass"


def test_a_rate_limited_run_is_a_cause_naming_the_kind_and_credential(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-SECRETSECRET")
    out = _stream({"type": "system", "subtype": "api_retry", "error": "rate_limit", "attempt": 1},
                  {"type": "result", "subtype": "error_during_execution", "is_error": True,
                   "result": "limit hit for sk-ant-oat01-SECRETSECRET"})
    key, value = rgc.judge(out, "", 1)
    assert key == "cause"
    assert value.startswith("KIND=rate-limit credential=CLAUDE_CODE_OAUTH_TOKEN exit=1")
    assert "SECRET" not in value


@pytest.mark.parametrize("init", [
    {**INIT, "mcp_servers": [{"name": "review", "status": "failed"}], "tools": ["StructuredOutput"]},
    {**INIT, "mcp_servers": []},
    {**INIT, "tools": ["StructuredOutput", "mcp__review__read_file"]},
    None,
])
def test_a_verdict_written_without_the_review_tools_is_a_cause_not_a_pass(init) -> None:
    # a failed MCP server does not stop `claude -p` (proved on CLI 2.1.302): it returns a success
    # with a verdict a reviewer that could read nothing wrote
    events = [e for e in (init,) if e] + [{"type": "result", "is_error": False, "structured_output": VERDICT}]
    key, value = rgc.judge(_stream(*events), "", 0)
    assert key == "cause"
    assert ("had no tools" in value) if init else value.endswith("no init event")


def test_an_error_result_with_a_stray_structured_output_is_still_a_cause() -> None:
    out = _stream(INIT, {"type": "result", "is_error": True, "structured_output": VERDICT, "result": "x"})
    assert rgc.judge(out, "", 1)[0] == "cause"


def test_no_result_event_is_a_cause_not_a_crash() -> None:
    key, value = rgc.judge("not json\n[1]\n", "boom", 1)
    assert key == "cause" and "boom" in value


def test_main_refuses_without_the_token(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setenv("REVIEW_PROMPT", "review it")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert rgc.main(["--model", "m", "--schema", json.dumps(SCHEMA)]) == 2
    assert _outputs(tmp_path / "out")["cause"].startswith("KIND=no-credential")


def test_main_names_a_missing_cli(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setenv("REVIEW_PROMPT", "review it")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "o")

    def missing(*_a, **_k):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(subprocess, "run", missing)
    assert rgc.main(["--model", "m", "--schema", json.dumps(SCHEMA)]) == 1
    assert "exit=not-run" in _outputs(tmp_path / "out")["cause"]


def test_main_writes_the_verdict(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setenv("REVIEW_PROMPT", "review it")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "o")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        out = _stream(INIT, {"type": "result", "is_error": False, "structured_output": VERDICT})
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert rgc.main(["--model", "m", "--schema", json.dumps(SCHEMA)]) == 0
    assert json.loads(_outputs(tmp_path / "out")["structured_output"]) == VERDICT
    assert seen["input"] == "review it" and "ANTHROPIC_API_KEY" not in seen["env"]
    # the CLI's project context is an empty dir outside the checkout: no PR-authored
    # CLAUDE.md / .claude/ can reach the reviewer, whatever `--restricted` covers on a CLI bump
    cwd = Path(seen["cwd"]).resolve()
    assert not cwd.is_relative_to(tools.ROOT.resolve())
    assert list(cwd.iterdir()) == []


def test_a_cli_cwd_inside_the_checkout_is_refused(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    with pytest.raises(ValueError, match="inside the checkout"):
        rgc.cli_cwd(tmp_path)
    assert not rgc.cli_cwd(tmp_path / "elsewhere").is_relative_to(tmp_path / "elsewhere")


def test_the_mcp_server_serves_the_agent_tools_and_they_still_refuse() -> None:
    server = rgm.build_server()
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == set(tools.TOOLS)
    result = asyncio.run(server.call_tool("run", {"command": "rm -rf /"}))
    assert "refused" in json.dumps(result.model_dump(mode="json"))
