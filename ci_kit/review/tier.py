"""The review gate's first step: is this diff reviewed, why, by which model, on which credential.

EVERY CODE PR IS REVIEWED (trade-data operator law 2026-10-17: structure -- a duplicate of an
existing helper, a library re-implemented, a layer skipped -- is what the ratchets cannot see).
The caller's own tiers stay as REASONS the reviewer reads, not as whether it runs:
  * a guard path (the caller's paths-filter list: gates, hooks, migrations, the money path);
  * a build loop of 3+ NON-MERGE commits -- the armed-PR updater's merges of main never count;
  * the caller's label (default `review:required`), which must be on the PR AT OPEN: the
    workflow has no `labeled` trigger, because a job-level `if:` cannot filter label events and a
    SKIPPED required check reads as PASSED.

DOCS / LOCKFILE ONLY is the one diff that skips the model call (trade-data CI diet, 2026-09-27):
every file is prose (`*.md`) or a lockfile, so there is no guard to prove red and no structure.
It drops ONLY the commit-count reason; a guard path or the label still reviews. The diff is the
PR's own (merge-base..head) with `--no-renames`: with rename detection `--name-only` prints only
the NEW name, so `git mv app/x.py docs/x.md` would read as docs-only. An empty or unreadable list
is NOT docs-only. Paths under a `--not-prose` prefix (default `.claude/` agent charters and
`prompts/` runtime LLM prompts) are never prose, whatever their suffix.

THE CREDENTIAL TOGGLE (trade-data operator, 2026-11-06): the caller repo's VARIABLE
REVIEW_GATE_AUTH = `subscription` (secret CLAUDE_CODE_OAUTH_TOKEN, the `claude` transport) | `api`
(secret ANTHROPIC_API_KEY, the `agent` transport) | `auto`/unset (subscription first). A mode
whose secret is missing is UNARMED: red (unconfigured must not look healthy) but no per-PR page.
A fork head is BLIND: repository secrets never reach it. The log line `Reviewer: mode=...
credential=<NAME> model=...` is read by mcp/scripts/review_gate_auth.py; a value never prints.

Exit 0 once the outputs are written; 2 = git could not answer (the step fails, so the job is red).
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass

from ci_kit.review import tools

LOCKFILES = frozenset({"uv.lock", "poetry.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
                       "Cargo.lock"})
NOT_PROSE = (".claude/", "prompts/")
MODES = ("subscription", "api", "auto")
CREDS = {"oauth": ("CLAUDE_CODE_OAUTH_TOKEN", "Claude Code CLI on the subscription"),
         "api_key": ("ANTHROPIC_API_KEY", "plain Messages API, billed per token")}


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def docs_only(changed: list[str], not_prose: tuple[str, ...] = NOT_PROSE) -> bool:
    """Every path is prose or a lockfile. An empty list is NOT docs-only (nothing read != clean)."""
    if not changed:
        return False
    for f in changed:
        if any(f.startswith(p) for p in not_prose):
            return False
        if not (f.endswith(".md") or f.rsplit("/", 1)[-1] in LOCKFILES):
            return False
    return True


def guard_names(guard_files: str, limit: int = 5) -> str:
    """paths-filter's `list-files: shell` text -> the first few names, one line (a path may carry
    CR/LF; one reaching GITHUB_OUTPUT would forge an output line)."""
    try:
        names = shlex.split(guard_files)
    except ValueError:
        names = guard_files.split()
    return ",".join(" ".join(n.split()) for n in names[:limit])


def mode_of(raw: str) -> str:
    """Only [a-z] survives, max 16: a variable cannot forge an output line; `API` reads as `api`."""
    return "".join(c for c in (raw or "auto").lower() if "a" <= c <= "z")[:16]


def credential(mode: str, has_oauth: bool, has_key: bool) -> tuple[str, str]:
    """(credential, wanted-secret-text). credential '' = none; wanted '' = the mode is not one."""
    if mode == "subscription":
        return ("oauth" if has_oauth else ""), "CLAUDE_CODE_OAUTH_TOKEN"
    if mode == "api":
        return ("api_key" if has_key else ""), "ANTHROPIC_API_KEY"
    if mode == "auto":
        cred = "oauth" if has_oauth else "api_key" if has_key else ""
        return cred, "CLAUDE_CODE_OAUTH_TOKEN (subscription) or ANTHROPIC_API_KEY (api)"
    return "", ""


@dataclass(frozen=True)
class Tier:
    required: bool
    reasons: str
    notes: list[str]
    model: str
    credential: str
    credential_name: str
    transport: str
    mode: str
    blind: str
    unarmed: bool


def decide(*, guard: bool, guard_files: str, commits: int, changed: list[str], labeled: bool,
           label: str, head_repo: str, repo: str, auth_mode: str, has_oauth: bool, has_key: bool,
           model: str, guard_model: str, not_prose: tuple[str, ...] = NOT_PROSE) -> Tier:
    reasons, notes = [], []
    if guard:
        reasons.append(f"guard path ({guard_names(guard_files)})")
    only_docs = docs_only(changed, not_prose)
    if commits >= 3:
        if only_docs:
            notes.append(f"{commits} commits, but docs/lockfile-only: the commit count does not put it in the tier.")
        else:
            reasons.append(f"{commits} commits")
    if labeled:
        reasons.append(f"label {label}")
    mode = mode_of(auth_mode)
    cred, want = credential(mode, has_oauth, has_key)
    cred_name, transport = CREDS.get(cred, ("none", "none"))
    blind, unarmed = "", False
    if head_repo != repo:
        blind = f"the head is a fork ({head_repo}); repository secrets never reach it"
    elif not want:
        blind, unarmed = f"gate UNARMED: repository variable REVIEW_GATE_AUTH='{mode}' is not subscription, api or auto", True
    elif not cred:
        blind, unarmed = f"gate UNARMED (REVIEW_GATE_AUTH={mode}), no reviewer credential: set repository secret {want}", True
    use_model = guard_model if guard else model
    if not reasons:
        if only_docs:
            notes.append("Not in the review tier (docs/lockfile-only diff, no guard path, no label): pass.")
            return Tier(False, "", notes, use_model, cred, cred_name, transport, mode, blind, unarmed)
        reasons.append("every code PR: structure review")
    return Tier(True, "; ".join(reasons), notes, use_model, cred, cred_name, transport, mode, blind, unarmed)


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == "true"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", required=True)
    ap.add_argument("--guard-model", required=True)
    ap.add_argument("--label", default="review:required")
    ap.add_argument("--not-prose", action="append", help=f"path prefix never read as prose (default {NOT_PROSE})")
    a = ap.parse_args(argv)
    e = os.environ
    try:
        base, head = e["BASE_SHA"], e["HEAD_SHA"]
        mb = _git("merge-base", base, head).strip()
        commits = int(_git("rev-list", "--no-merges", "--count", f"{mb}..{head}").strip() or 0)
        changed = _git("diff", "--no-renames", "--name-only", mb, head).splitlines()
    except (KeyError, subprocess.CalledProcessError, OSError, ValueError) as err:
        print(f"::error::ci_kit.review.tier could not read the PR's own diff: {err}", file=sys.stderr)
        return 2
    t = decide(guard=_flag("GUARD"), guard_files=e.get("GUARD_FILES", ""), commits=commits,
               changed=changed, labeled=_flag("LABELED"), label=a.label,
               head_repo=e.get("HEAD_REPO", ""), repo=e.get("GITHUB_REPOSITORY", ""),
               auth_mode=e.get("AUTH_MODE", ""), has_oauth=_flag("HAS_OAUTH"), has_key=_flag("HAS_KEY"),
               model=a.model, guard_model=a.guard_model,
               not_prose=tuple(a.not_prose) if a.not_prose else NOT_PROSE)
    for n in t.notes:
        tools.summary(n)
    tools.emit("required", "true" if t.required else "false")
    if not t.required:
        return 0
    tools.emit("reasons", t.reasons)
    tools.summary(f"In the review tier: {t.reasons}")
    tools.emit("model", t.model)
    tools.emit("credential", t.credential)
    tools.emit("credential_name", t.credential_name)
    tools.emit("blind", t.blind)
    tools.emit("unarmed", "true" if t.unarmed else "false")
    line = f"Reviewer: mode={t.mode} credential={t.credential_name} model={t.model}"
    tools.summary(f"{line} ({t.transport})")
    print(line)   # in the LOG too, which the API serves: mcp/scripts/review_gate_auth.py reads it
    return 0


if __name__ == "__main__":
    sys.exit(main())
