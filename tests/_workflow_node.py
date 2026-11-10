"""Node tooling in workflow steps: the one detector the node tests share.

A self-hosted pool's image carries what its builder put there, not what ubuntu-latest has: the
sealed VM has no node, so the review gate's `npm install -g @anthropic-ai/claude-code` failed with
`npm: command not found` (exit 127) and every review went BLIND once the job moved there (1.22.1).
"""
from __future__ import annotations

import re

SETUP_NODE = "actions/setup-node@"

# a node tool as a command word: start of line or after a shell separator / quote / path slash,
# ending at a separator -- so `npm`, `&& npx`, `/usr/bin/node`, `$(node -v)`, never `node-version`
NODE_TOOL = re.compile(r"(^|[\s|;&(`'\"/])(npm|npx|node|pnpm|yarn|corepack)(?=$|[\s|;&)`'\"])")


def runs_node(script: str) -> bool:
    """True when a non-comment line of a `run:` script invokes a node tool."""
    return any(NODE_TOOL.search(line) for line in script.splitlines() if not line.lstrip().startswith("#"))


def is_setup_node(step: dict) -> bool:
    return str(step.get("uses", "")).startswith(SETUP_NODE)


def node_faults(workflow: dict) -> list[str]:
    """`job: step i (name)` for each step running node tooling with no setup-node before it in its job."""
    out = []
    for name, job in (workflow.get("jobs") or {}).items():
        have_node = False
        for i, step in enumerate(job.get("steps") or []):
            if is_setup_node(step):
                have_node = True
            elif not have_node and runs_node(str(step.get("run", ""))):
                out.append(f"{name}: step {i} ({step.get('name', 'unnamed')})")
    return out
