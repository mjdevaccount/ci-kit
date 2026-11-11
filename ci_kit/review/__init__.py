"""The pre-merge model review gate: a reviewer whose verdict is a required CI check.

WHY (trade-data, 2026-09-26). PRs open READY with auto-merge armed at open, and CI is the only
merge gate. A review that lived in session memory lost every race: trade-data#2059 auto-merged
30s after opening, and the reviewer's DO NOT SHIP arrived after. Anthropic's managed Code Review
never blocks a merge, so the gate is `.github/workflows/review-gate.yml` (reusable) over these
modules. Moved here from trade-data `scripts/review_gate_*.py` (2026-11-06) so every repo calls
ONE gate and no PR can edit the policy that grades it.

  tier        which reasons put the diff in the tier, the docs/lockfile-only skip, the credential
  carry       carry a pass across a base-only update; list sibling PRs sharing files
  tools       the reviewer's four read-only tools (no shell, fenced to the checkout)
  agent       `api` transport: pydantic-ai on the plain Messages API (ANTHROPIC_API_KEY)
  claude      `subscription` transport: the Claude Code CLI headless (CLAUDE_CODE_OAUTH_TOKEN)
  mcp_server  `tools` as a stdio MCP server for the CLI
  verdict     the structured output -> pass / block / BLIND, and the PR comment body
  train       a merge-train PR (head `train/*`): every commit is one constituent's exact squash at a
              head whose own review-gate run passed -> pass with no model call, else block / BLIND

Only `agent`, `claude` and `mcp_server` need the `review` extra; the rest are stdlib.
"""
