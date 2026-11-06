"""The review gate's reviewer agent (ci_kit/review/tools.py), with no network.

What must hold for the gate to stay a gate: the tools REFUSE what the old --allowedTools refused
(other commands, writing flags, paths out of the checkout), no subprocess sees the API key, a
verdict lands as `structured_output` in the verdict step's schema, and any failure lands as a
`cause` with the key scrubbed -- never a crash with nothing written.
"""
from __future__ import annotations

import json
import sys

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ci_kit.review import agent as rga
from ci_kit.review import tools
from ci_kit.review.verdict import SCHEMA, decide

VERDICT = {"ship": "SHIP", "findings": [], "not_reviewed": "nothing"}


@pytest.mark.parametrize("command", [
    "rm -rf /", "bash -c 'git diff'", "git push origin main", "git -c core.pager=sh diff",
    "uv run python -c 1", "git diff --output=/tmp/x", "git log --ext-diff", "git show -o x",
    "uv run ruff check --fix .", "uv run ruff format .", "uv run ruff check --unsafe-fixes",
    "git diff 'unterminated", "uv run ruff check -o out.txt .", "uv run ruff check --output-file=x .",
    "git diff --no-index /dev/null /proc/self/environ", "git diff --outp=/tmp/x", "git diff --no-ind a b",
    "uv run ruff check --config \"output-file = 'x'\" .", "uv run ruff check --add-ignore .",
    "uv run ruff check --watch", "uv run ruff rule E501",
])
def test_run_refuses_everything_off_the_allowlist(command: str) -> None:
    assert tools.run(command).startswith("refused")


@pytest.mark.parametrize("command", [
    "git log --oneline -1", "git diff --stat HEAD~1", "git show --name-only HEAD",
    "uv run ruff check --select E501 --output-format=concise ci_kit/review/tools.py",
])
def test_run_allows_read_only_commands(command: str) -> None:
    assert not tools.run(command).startswith("refused")


def test_main_names_an_agent_that_cannot_be_built(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setenv("REVIEW_PROMPT", "review it")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    def broken(_name: str):
        raise ImportError("No module named 'anthropic'")

    monkeypatch.setattr(rga, "anthropic_model", broken)
    assert rga.main(["--model", "m", "--schema", json.dumps(SCHEMA)]) == 2
    assert "could not build the agent: ImportError" in _outputs(tmp_path / "out")["cause"]


@pytest.mark.parametrize("call", [
    lambda: tools.read_file("../outside.txt"),
    lambda: tools.read_file("/etc/passwd"),
    lambda: tools.grep("x", path="../.."),
])
def test_paths_outside_the_checkout_are_refused(call) -> None:
    assert "outside the checkout" in call()


def test_read_file_numbers_lines_from_offset() -> None:
    out = tools.read_file("ci_kit/review/tools.py", offset=2, limit=1)
    assert out.startswith("2\t")
    assert "lines total" in out


def test_no_subprocess_sees_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in tools.SECRET_ENV:
        monkeypatch.setenv(k, "s3cret")
    code = "import os; print(sorted(k for k, v in os.environ.items() if v == 's3cret'))"
    assert tools._exec([sys.executable, "-c", code]).strip() == "[]"


def test_clip_keeps_head_and_tail() -> None:
    out = tools._clip("a" * tools.MAX_OUT + "TAIL")
    assert out.endswith("TAIL")
    assert "chars cut" in out


def _outputs(path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


def test_a_verdict_lands_as_structured_output_the_verdict_step_accepts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    seen: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:   # first turn: try a refused command, then read the diff
            return ModelResponse(parts=[ToolCallPart("run", {"command": "rm -rf /"}),
                                        ToolCallPart("run", {"command": "git log --oneline -1"})])
        seen.extend(str(p.content) for p in messages[-1].parts)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, VERDICT)])

    assert rga.review(rga.build_agent(FunctionModel(model), SCHEMA), "review it", 10) == 0
    out = _outputs(tmp_path / "out")
    assert json.loads(out["structured_output"]) == VERDICT
    assert decide(out["structured_output"], "guard path").result == "pass"
    assert any(s.startswith("refused") for s in seen)


def test_a_failure_lands_as_a_cause_with_the_key_scrubbed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-123")

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("Credit balance is too low for sk-ant-test-123\nsecond line "
                           "ghp_" + "a" * 36 + " sk-ant-usr-OTHERKEY99")

    assert rga.review(rga.build_agent(FunctionModel(model), SCHEMA), "review it", 10) == 1
    out = _outputs(tmp_path / "out")
    assert "structured_output" not in out
    assert out["cause"] == "RuntimeError: Credit balance is too low for *** second line *** ***"


def test_main_refuses_without_a_prompt_or_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.delenv("REVIEW_PROMPT", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert rga.main(["--model", "m", "--schema", json.dumps(SCHEMA)]) == 2
    assert "cause" in _outputs(tmp_path / "out")
