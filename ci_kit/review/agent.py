"""The review gate's `api` transport: a pydantic-ai agent on the PLAIN Anthropic Messages API.

WHY NOT claude-code-action (trade-data, 2026-10-18). The action drives the claude CLI, which
sends the `anthropic-beta: claude-code-20250219` header, and Anthropic bills that traffic as
CLAUDE CODE. An org credit grant covering "Agent SDK, API, Batch API, Playground, Managed Agents"
does not cover it, so every run died in 350ms with "Credit balance is too low" (td#2373, BLIND
x4). A plain API call from the `anthropic` SDK carries no such header.

Contract (shared with `claude`): the verdict SCHEMA comes in with `--schema` (the workflow prints
it with `python -m ci_kit.review.verdict --print-schema`); the tools are `tools`; the output is
`structured_output=<json>`, or on any failure `cause=<why>` with the key scrubbed -- a BLIND that
names itself (pydantic_ai is imported inside functions, so a broken install still reaches main()
and is written as a cause).

Exit 0 with `structured_output`, 1 with `cause` (the step is continue-on-error), 2 bad args.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from ci_kit.review import tools


def build_agent(model, schema: dict):
    from pydantic_ai import Agent, StructuredDict
    return Agent(model, output_type=StructuredDict(schema, name="review_verdict"),
                 instructions=tools.INSTRUCTIONS,
                 tools=[tools.read_file, tools.grep, tools.glob_files, tools.run])


def anthropic_model(name: str):
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider
    return AnthropicModel(name, provider=AnthropicProvider(api_key=os.environ["ANTHROPIC_API_KEY"]))


def _settings():
    from pydantic_ai.models.anthropic import AnthropicModelSettings
    return AnthropicModelSettings(max_tokens=16_000, anthropic_cache_instructions=True,
                                  anthropic_cache_tool_definitions=True, anthropic_cache_messages=True)


def review(agent, prompt: str, max_requests: int) -> int:
    try:
        from pydantic_ai import UsageLimits
        result = agent.run_sync(prompt, model_settings=_settings(),
                                usage_limits=UsageLimits(request_limit=max_requests))
    except Exception as e:  # any failure is a BLIND with its cause named, never a crash
        cause = tools.scrub(f"{type(e).__name__}: {e}", os.environ.get("ANTHROPIC_API_KEY", ""))
        tools.emit("cause", cause[:400])
        tools.summary(f"reviewer failed: {cause[:400]}")
        return 1
    u = result.usage
    tools.summary(f"reviewer usage: requests={u.requests} input={u.input_tokens} output={u.output_tokens} "
                  f"cache_read={u.cache_read_tokens} cache_write={u.cache_write_tokens}")
    tools.emit("structured_output", json.dumps(result.output))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", required=True)
    ap.add_argument("--schema", required=True, help="the verdict JSON schema (verdict --print-schema)")
    ap.add_argument("--max-requests", type=int, default=80)
    a = ap.parse_args(argv)
    prompt = os.environ.get("REVIEW_PROMPT", "")
    if not prompt.strip() or not os.environ.get("ANTHROPIC_API_KEY"):
        tools.emit("cause", "KIND=no-credential review agent: REVIEW_PROMPT or ANTHROPIC_API_KEY is empty")
        return 2
    try:
        schema = json.loads(a.schema)
    except ValueError as e:
        tools.emit("cause", f"review agent: --schema is not JSON ({e})")
        return 2
    try:
        agent = build_agent(anthropic_model(a.model), schema)
    except Exception as e:  # ImportError after a bad install, a schema pydantic-ai rejects, ...
        tools.emit("cause", f"review agent: could not build the agent: {type(e).__name__}: {e}"[:400])
        return 2
    return review(agent, prompt, a.max_requests)


if __name__ == "__main__":
    sys.exit(main())
