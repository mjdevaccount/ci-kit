"""The review gate's `subscription` transport: the reviewer on the Claude Code CLI (`claude -p`).

WHY (trade-data operator, 2026-11-06): "put my claude sub in github then ... i dont wanna pay api
creds". The subscription credential is CLAUDE_CODE_OAUTH_TOKEN (minted by `claude setup-token`),
and Anthropic's terms scope it to Claude Code itself ("ordinary use of Claude Code and other
native Anthropic applications"; Agent SDK products "should use API key authentication",
code.claude.com/docs/en/legal-and-compliance). So this drives the UNMODIFIED CLI headless -- not
the Python Agent SDK, and not claude-code-action, whose actor checks refused Renovate (td#2311).

Same contract as `agent`:
  * the verdict SCHEMA goes in with `--json-schema`; the result's `structured_output` is the JSON
    the verdict step parses;
  * the tools are `tools`' four, served by `mcp_server`, with every built-in tool removed
    (`--tools ""`) -- no Bash, no Read past the checkout fence;
  * the CLI runs in a fresh EMPTY dir outside the checkout (cli_cwd), so project discovery finds no
    PR-authored CLAUDE.md, .claude/rules or .claude/settings.json whatever a CLI bump does to
    `--restricted`, which stays as the second fence; the tool server reaches the checkout through
    REVIEW_GATE_ROOT, never the CLI's cwd; no `--bare`, which would not read the OAuth token;
  * ANTHROPIC_API_KEY is DROPPED from the CLI's env: in `-p` it outranks the OAuth token, and the
    run would bill the API account this transport exists to avoid;
  * on failure `cause=` names the KIND (rate-limit / auth-rejected / credit / overloaded / error),
    so a BLIND says whether to wait, re-mint the token, or top up.

Exit 0 with `structured_output`, 1 with `cause` (the step is continue-on-error), 2 bad args.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ci_kit.review import tools

CLI_TIMEOUT = 33 * 60      # s; under the workflow step's 35 min, so a hang still writes its cause
MAX_TURNS = 80
DROP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GH_TOKEN", "GITHUB_TOKEN",
            "ACTIONS_RUNTIME_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "NTFY_URL")
CRED = "CLAUDE_CODE_OAUTH_TOKEN"
# CLI error categories (system/api_retry `error`, assistant `error`) -> the BLIND's kind
KIND_BY_CATEGORY = {"rate_limit": "rate-limit", "authentication_failed": "auth-rejected",
                    "oauth_org_not_allowed": "auth-rejected", "account_on_hold": "auth-rejected",
                    "billing_error": "credit", "overloaded": "overloaded"}
# when only text survives (the result line of a run that never reached the model)
KIND_BY_TEXT = (("usage limit", "rate-limit"), ("rate limit", "rate-limit"),
                ("credit balance", "credit"), ("billing", "credit"),
                ("invalid api key", "auth-rejected"), ("/login", "auth-rejected"),
                ("oauth", "auth-rejected"), ("unauthorized", "auth-rejected"),
                ("overloaded", "overloaded"))


def failure_kind(categories: list[str], text: str) -> str:
    """The BLIND's kind: what the operator does next differs for each one."""
    for c in reversed(categories):            # the newest structured category wins
        if c in KIND_BY_CATEGORY:
            return KIND_BY_CATEGORY[c]
    low = text.lower()
    return next((kind for needle, kind in KIND_BY_TEXT if needle in low), "error")


def cli_argv(model: str, schema: str, root: Path) -> list[str]:
    config = {"mcpServers": {"review": {
        "type": "stdio", "command": sys.executable, "args": ["-m", "ci_kit.review.mcp_server"],
        "env": {"REVIEW_GATE_ROOT": str(root)}}}}
    allowed = ",".join(f"mcp__review__{t}" for t in tools.TOOLS)
    return ["claude", "-p", "--model", model, "--output-format", "stream-json", "--verbose",
            "--json-schema", schema, "--restricted", "--tools", "", "--strict-mcp-config",
            "--mcp-config", json.dumps(config), "--allowedTools", allowed,
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--disable-slash-commands", "--no-session-persistence",
            "--max-turns", str(MAX_TURNS), "--append-system-prompt", tools.INSTRUCTIONS]


def cli_cwd(root: Path) -> Path:
    """A fresh empty dir OUTSIDE the checkout: the CLI's project context must hold nothing a PR wrote."""
    base = os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()
    cwd = Path(tempfile.mkdtemp(prefix="review-gate-cli-", dir=base)).resolve()
    if cwd.is_relative_to(Path(root).resolve()):
        raise ValueError(f"CLI cwd {cwd} is inside the checkout {root}")
    return cwd


def cli_env(environ: dict[str, str]) -> dict[str, str]:
    env = {k: v for k, v in environ.items() if k not in DROP_ENV}
    env.setdefault("MCP_TOOL_TIMEOUT", str(1_000 * 1_000))   # ms; a full pytest is ~3 min
    env["DISABLE_AUTOUPDATER"] = "1"
    return env


def parse_stream(stdout: str) -> tuple[dict | None, dict | None, list[str]]:
    """The `system/init` event, the final `result` event, and every error category on the way."""
    init, result, categories = None, None, []
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "result":
            result = ev
        elif ev.get("type") == "system" and ev.get("subtype") == "init":
            init = ev
        elif isinstance(ev.get("error"), str):            # system/api_retry and assistant errors
            categories.append(ev["error"])
    return init, result, categories


def tools_missing(init: dict | None) -> str:
    """Why the reviewer could NOT look, or ''. A failed MCP server does not stop `claude -p`: it
    runs on with StructuredOutput alone and returns a success a reviewer with no tools wrote."""
    if not init:
        return "no init event"
    servers = {s.get("name"): s.get("status") for s in init.get("mcp_servers") or [] if isinstance(s, dict)}
    if servers.get("review") != "connected":
        return f"tool server review={servers.get('review', 'absent')}"
    absent = [t for t in tools.TOOLS if f"mcp__review__{t}" not in (init.get("tools") or [])]
    return f"review tools absent: {', '.join(absent)}" if absent else ""


def _scrub(text: str) -> str:
    return tools.scrub(text, os.environ.get(CRED, ""))


def judge(stdout: str, stderr: str, returncode: int | str) -> tuple[str, str]:
    """('structured_output', json) on a verdict, else ('cause', why) -- never both, never neither."""
    init, result, categories = parse_stream(stdout)
    out = (result or {}).get("structured_output")
    blind = tools_missing(init)
    if result and not result.get("is_error") and isinstance(out, dict) and not blind:
        return "structured_output", json.dumps(out)
    if blind and init:     # the model ran, but without its tools: never a verdict
        return "cause", _scrub(f"KIND=error credential={CRED} exit={returncode}: "
                               f"the reviewer had no tools ({blind})")[:400]
    detail = " ".join(str(x) for x in ((result or {}).get("subtype"), (result or {}).get("result"),
                                       stderr.strip()[-300:]) if x)
    kind = failure_kind(categories, detail)
    why = f"KIND={kind} credential={CRED} exit={returncode}: {detail or blind or 'no result event'}"
    return "cause", _scrub(why)[:400]


def run(argv_: list[str], text: str, cwd: Path, env: dict[str, str], timeout: int = CLI_TIMEOUT
        ) -> tuple[str, str, int | str]:
    """The CLI headless on `text`: (stdout, stderr, exit code | 'timeout' | 'not-run'). Never raises:
    a hang keeps what it streamed, a CLI that is not installed is a cause, not a traceback. Shared
    with the heal ladder's fixer (ci_kit/pr_fix.py)."""
    try:
        p = subprocess.run(argv_, input=text, cwd=cwd, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return out, f"timed out after {timeout}s", "timeout"
    except OSError as e:                              # the CLI is not installed
        return "", f"{type(e).__name__}: {e}", "not-run"


def _usage(stdout: str) -> str:
    _, result, _ = parse_stream(stdout)
    if not result:
        return "reviewer usage: (no result event)"
    u = result.get("usage") or {}
    return (f"reviewer usage (subscription): turns={result.get('num_turns')} "
            f"input={u.get('input_tokens')} output={u.get('output_tokens')} "
            f"cache_read={u.get('cache_read_input_tokens')} cache_write={u.get('cache_creation_input_tokens')} "
            f"est_usd={result.get('total_cost_usd')} (an estimate; the subscription is not billed per token)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", required=True)
    ap.add_argument("--schema", required=True, help="the verdict JSON schema (verdict --print-schema)")
    a = ap.parse_args(argv)
    prompt = os.environ.get("REVIEW_PROMPT", "")
    if not prompt.strip() or not os.environ.get(CRED):
        tools.emit("cause", f"KIND=no-credential review claude: REVIEW_PROMPT or {CRED} is empty")
        return 2
    argv_ = cli_argv(a.model, a.schema, tools.ROOT)
    try:
        cwd = cli_cwd(tools.ROOT)
    except ValueError as e:
        tools.emit("cause", f"KIND=error review claude: {e}")
        return 1
    stdout, stderr, code = run(argv_, prompt, cwd, cli_env(dict(os.environ)))
    tools.summary(_usage(stdout))
    key, value = judge(stdout, stderr, code)
    tools.emit(key, value)
    if key == "cause":
        tools.summary(f"reviewer failed: {value}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
