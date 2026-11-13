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

VERSION BUMP ONLY is the second (2026-11-10, ci_kit/review/bump.py has the rule), for a PR OPENED
BY a bump bot (BUMP_BOTS: the event's `pull_request.user.login`, which GitHub sets, not the diff)
from this repository, whose every non-merge commit that bot authored and every merge is a clean,
unedited merge (`git merge-tree --write-tree`). One allowance (2026-11-12, trade-data relock.yml):
a non-merge commit by ANY author whose own diff touches lockfiles only is accepted -- the gate
passes a lockfile-only diff whoever wrote it, so composing the two adds no trust; any other
foreign commit is reviewed, and the log names the lock that failed. A diff cannot prove a SHA
or a lockfile is what it claims, so the bot's identity is the first lock and the diff the second
(reviewer 11-11 #2: any other author, the same shape is reviewed). Every changed
line of every manifest differs from its pair only in a version token, so the model would read a
version string. It drops the commit-count AND the guard-path reason -- a bumped pin in a guard
file (`pyproject.toml`) changes no guard -- and the label still reviews. The summary lists the
first 20 bumps, and the log says `Review tier: skipped (version bump only, N lines)`. A diff the
bump check cannot read is reviewed, never red.

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
from collections.abc import Callable
from dataclasses import dataclass

from ci_kit.review import tools
from ci_kit.review.bump import LOCKFILES, bump_only

NOT_PROSE = (".claude/", "prompts/")
BUMP_BOTS = {"renovate[bot]": "29139614+renovate[bot]@users.noreply.github.com"}
# GitHub's committer for an API commit (Renovate's own, td#2955/#2951 checked 11-11). An amend,
# rebase or cherry-pick on a workstation keeps the bot as AUTHOR but commits as the person.
WEB_FLOW = "noreply@github.com"
MODES = ("subscription", "api", "auto")
CREDS = {"oauth": ("CLAUDE_CODE_OAUTH_TOKEN", "Claude Code CLI on the subscription"),
         "api_key": ("ANTHROPIC_API_KEY", "plain Messages API, billed per token")}


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def _bumps(mb: str, head: str, changed: list[str]) -> list[str]:
    """The PR's version bumps, or [] -- never an error: a diff this cannot read is reviewed, not red.
    Bytes split on LF by bump_only; surrogateescape so a Latin-1 file cannot kill the gate."""
    try:
        raw = subprocess.run(["git", "diff", "--no-renames", "--no-ext-diff", "--no-textconv",
                              "--no-color", "-U0", mb, head], check=True, capture_output=True).stdout
        return bump_only(raw.decode("utf-8", "surrogateescape"), changed)
    except Exception as err:  # noqa: BLE001 -- any failure means "not a bump": the model reviews
        print(f"::warning::version-bump check could not read the diff ({type(err).__name__}): reviewing")
        return []


def _honest_merge(sha: str, parents: list[str]) -> bool:
    """A two-parent merge whose tree is exactly what git merges its parents to, with no conflict:
    an update-branch merge of main. An edit or a conflict resolution inside the merge is not
    (reviewer 11-11 round 3 #1: `--no-merges` let a hand-made merge carry a SHA swap past)."""
    if len(parents) != 2:
        return False
    try:   # exit 1 = conflicts -> CalledProcessError
        merged = _git("merge-tree", "--write-tree", parents[0], parents[1]).split("\n", 1)[0].strip()
    except subprocess.CalledProcessError:
        return False
    return merged == _git("rev-parse", f"{sha}^{{tree}}").strip()


def verified(repo: str, sha: str) -> bool:
    """GitHub says this commit's signature is valid (`gh`, as the carry step already uses it, with
    the job's GH_TOKEN). Any failure is False: the PR is reviewed."""
    out = subprocess.run(["gh", "api", f"repos/{repo}/commits/{sha}", "--jq", ".commit.verification.verified"],
                         check=True, capture_output=True, text=True, timeout=30).stdout
    return out.strip() == "true"


def lockfile_only(parent: str, sha: str) -> bool:
    """This commit's OWN diff (against its first parent, `--no-renames`, so a rename is a delete
    plus an add and both names must be lockfiles) touches lockfiles and nothing else. An empty
    commit or a diff git cannot read is not lockfile-only: nothing read != clean."""
    out = _git("diff", "--no-renames", "--name-only", "-z", parent, sha)
    paths = [p for p in out.split("\0") if p]
    return bool(paths) and all(p.rsplit("/", 1)[-1] in LOCKFILES for p in paths)


def bump_identity(author: str, head_repo: str, repo: str, mb: str, head: str,
                  signed: Callable[[str, str], bool] | None = None) -> tuple[str, list[str]]:
    """('', foreign) when the commit locks hold, else (the lock that failed, []). A same-repo PR a
    bump bot opened; every non-merge commit is EITHER the bot's own, as GitHub made it, OR touches
    lockfiles only (any author: `foreign` lists them, `sha8 author`); every merge an honest one; at
    least one bot commit. A bot commit must have (reviewer 11-11 rounds 4-5):
      * author `%ae` = the bot (a PR's .mailmap cannot remap `%ae`);
      * committer `%ce` = GitHub or the bot -- an amend, cherry-pick or local rebase keeps the
        author but commits as the person;
      * author date = committer date -- GitHub's rebase-style update-branch re-commits an amended
        commit as web-flow, but keeps its older author date;
      * a VERIFIED signature (`signed`, the commits API) -- `git filter-branch` keeps both
        identities and both dates, but drops the signature.
    THE LOCKFILE ALLOWANCE (2026-11-12): trade-data's relock.yml adds a `github-actions[bot]`
    `uv.lock` commit to a first-party wheel bump. The gate already passes a lockfile-only diff
    whoever wrote it (docs_only), so trusting a lockfile-only commit inside a bot bump adds no
    trust; no author string is trusted for it. A root or shallow-grafted commit (no parent) is
    reviewed. A hand commit touching any other path, an edited merge or a rewritten bot commit is
    reviewed. Never raises: anything it cannot read means review."""
    email = BUMP_BOTS.get(author)
    if not email or not repo or head_repo != repo:
        return "identity (PR opener or fork head)", []
    signed = signed or verified
    try:
        log = _git("log", "--format=%H%x1f%P%x1f%ae%x1f%ce%x1f%at%x1f%ct", f"{mb}..{head}")
        rows = [r.split("\x1f") for r in log.splitlines()]
        foreign: list[str] = []
        own = 0
        for sha, parents, who, committer, at, ct in rows:
            ps = parents.split()
            if not ps:
                return f"root or shallow commit {sha[:8]}", []
            if len(ps) > 1:
                if not _honest_merge(sha, ps):
                    return f"merge {sha[:8]} is not a clean, unedited merge", []
            elif who == email and committer in (WEB_FLOW, email) and at == ct and signed(repo, sha):
                own += 1
            elif lockfile_only(ps[0], sha):
                foreign.append(f"{sha[:8]} {who}")
            else:
                return f"non-lockfile foreign commit {sha[:8]} by {who} (identity/signature)", []
        if not own:
            return "identity (no commit by the bump bot)", []
        return "", foreign
    except Exception as err:  # noqa: BLE001 -- e.g. a non-UTF-8 author: review, never red
        print(f"::warning::bump-bot check could not read the commits ({type(err).__name__}): reviewing")
        return f"unreadable commits ({type(err).__name__})", []


def bot_authored(author: str, head_repo: str, repo: str, mb: str, head: str,
                 signed: Callable[[str, str], bool] | None = None) -> bool:
    """bump_identity's verdict alone: every commit lock holds."""
    return bump_identity(author, head_repo, repo, mb, head, signed)[0] == ""


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
           model: str, guard_model: str, not_prose: tuple[str, ...] = NOT_PROSE,
           bumps: list[str] | None = None) -> Tier:
    reasons, notes = [], []
    bumps = bumps or []
    if guard:
        if bumps:
            notes.append(f"guard path ({guard_names(guard_files)}), but version bumps only: no guard changed.")
        else:
            reasons.append(f"guard path ({guard_names(guard_files)})")
    only_docs = docs_only(changed, not_prose)
    if commits >= 3:
        if only_docs or bumps:
            notes.append(f"{commits} commits, but docs/lockfile/version-bump only: the commit count does not put it in the tier.")
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
        if bumps:
            notes.append(f"Not in the review tier (version bump only, {len(bumps)} lines, no label): pass.")
            notes.extend(f"- `{b}`" for b in bumps[:20])
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
    head_repo, repo = e.get("HEAD_REPO", ""), e.get("GITHUB_REPOSITORY", "")
    author = e.get("PR_AUTHOR", "")
    failed, foreign, bumps = "identity (not a bump bot)", [], []
    if author in BUMP_BOTS:
        failed, foreign = bump_identity(author, head_repo, repo, mb, head)
        bumps = [] if failed else _bumps(mb, head, changed)
        failed = failed or ("" if bumps else "non-bump diff")
    if author in BUMP_BOTS and not bumps:
        # the skip fails SAFE but silent: this line is what a watcher counts (reviewer 11-11 round 6 #3);
        # its prefix is mcp/scripts/review_gate_auth.py's regex, the failed lock goes AFTER it
        print(f"Review tier: bump skip NOT taken for {author} (commit identity/signature or a non-bump diff)"
              f" -- failed lock: {failed}")
    elif foreign:
        print(f"Review tier: lockfile-only commits by another author accepted: {', '.join(foreign[:10])}")
    t = decide(guard=_flag("GUARD"), guard_files=e.get("GUARD_FILES", ""), commits=commits,
               changed=changed, labeled=_flag("LABELED"), label=a.label,
               head_repo=head_repo, repo=repo,
               auth_mode=e.get("AUTH_MODE", ""), has_oauth=_flag("HAS_OAUTH"), has_key=_flag("HAS_KEY"),
               model=a.model, guard_model=a.guard_model,
               not_prose=tuple(a.not_prose) if a.not_prose else NOT_PROSE,
               bumps=bumps)
    for n in t.notes:
        tools.summary(n)
    tools.emit("required", "true" if t.required else "false")
    if not t.required:
        if bumps:
            print(f"Review tier: skipped (version bump only, {len(bumps)} lines)")
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
