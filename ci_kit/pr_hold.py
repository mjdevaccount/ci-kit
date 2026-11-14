"""THE QUEUE HOLD: a commit status `queue/position` that branch protection REQUIRES, success only on
the queue HEAD's commit -- so an armed PR behind the head cannot merge out of turn, whatever its
checks say. ORDER only: it removes or weakens no check; every required check still runs and passes
at the merging head.

THE CASE (2026-11-14 06:19:20Z): td#3083 was armed and green while #3082 was the head; GitHub's
auto-merge landed #3083 first and #3082 had to re-test. pr_sched.out_of_queue PAGED that -- after
it happened. The hold makes it impossible.

Per pass of pr-autoupdate (every push to main, every last required check, the caller's cron):
  * the HEAD (pr_queue.head: the lowest-numbered armed same-repo non-draft PR) -> success;
  * a merge train's PR (`train/...`) -> success: a train IS the queue landing;
  * every other same-repo PR -> pending, saying why (behind the head, or not in the queue at all:
    a draft or an unarmed PR joins by arming). A fork gets nothing: it is never a member.
A status is posted only when its STATE differs from the commit's newest one, so a waiting PR is
not re-posted every pass. A new head commit (an update, a push) has no status until the next pass,
and that pass is due: its own checks finishing trigger one.

FAIL-SAFE, AND WATCHED. The head's success is posted by ANY pass, whatever else that pass does
(the scheduler OFF, a crashed heal), and a stale pending on a PR that became head clears on the
next pass. A post the token is refused is an ERROR (a red run). The head still reading not-success
on a SECOND pass is a page: the first failing pass leaves a marker comment on the PR
(`<!-- queue:head-held sha=... -->`, the pr_heal pattern), the second pages ntfy once and marks it
paged. The run's red streak is what mcp ci-schedule-audit reads.

TOKEN: the push token (PR_UPDATE_TOKEN) needs Commit statuses: Read and write. Reads use the job's
GITHUB_TOKEN (statuses: read) first. The caller turns it on with `queue-hold: true` BEFORE adding
`queue/position` to branch protection -- protection first would block every merge until a pass
proved it can post.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable

from ci_kit import pr_queue
from ci_kit.ntfy import page

CONTEXT = "queue/position"
HEAD = "head of the PR queue: may merge"
TRAIN = "a merge train lands for the queue"
HELD = "<!-- queue:head-held sha={sha}{paged} -->"
_HELD = re.compile(r"<!-- queue:head-held sha=([0-9a-f]{40})( paged)? -->")

Gh = Callable[..., subprocess.CompletedProcess]


def desired(prs: list[dict], base: str = "") -> dict[int, tuple[str, str]]:
    """PR number -> (state, description) for every same-repo open PR into `base` (the protected
    default branch; '' = every base). A stacked PR (into another PR's branch) is neither the head
    nor held: protection on its base does not ask for the status, and as the head it would hold
    every PR into main behind one that cannot land there. Pure."""
    same = [p for p in prs if not p.get("isCrossRepository") and (not base or p.get("baseRefName") == base)]
    h = pr_queue.head(same)
    hn = int(h["number"]) if h else None
    out: dict[int, tuple[str, str]] = {}
    for p in same:
        n = int(p["number"])
        if str(p.get("headRefName") or "").startswith("train/"):
            out[n] = ("success", TRAIN)
        elif n == hn:
            out[n] = ("success", HEAD)
        elif pr_queue.member(p):
            out[n] = ("pending", f"waits its turn: the queue head is #{hn}")
        else:
            out[n] = ("pending", "not in the PR queue (a draft, or not armed): arm auto-merge to join it")
    return out


def current(repo: str, sha: str, read: Gh) -> str | None:
    """The commit's newest `queue/position` state, None when it has none or it could not be read."""
    try:
        got = json.loads(read("api", f"repos/{repo}/commits/{sha}/statuses?per_page=100").stdout or "[]")
    except (subprocess.CalledProcessError, ValueError):
        return None
    return next((str(s.get("state")) for s in got if isinstance(s, dict) and s.get("context") == CONTEXT), None)


def _post(repo: str, sha: str, state: str, desc: str, write: Gh) -> str:
    url = ""
    if os.environ.get("GITHUB_RUN_ID"):
        url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY', repo)}"
               f"/actions/runs/{os.environ['GITHUB_RUN_ID']}")
    got = write("api", "-X", "POST", f"repos/{repo}/statuses/{sha}", "-f", f"state={state}",
                "-f", f"context={CONTEXT}", "-f", f"description={desc[:140]}", *(["-f", f"target_url={url}"] if url else []))
    if got.returncode == 0:
        return ""
    msg = (got.stderr or got.stdout or "").strip()[-300:]
    if "resource not accessible" in msg.lower() or "403" in msg:
        msg += " -- the push token needs Commit statuses: Read and write"
    return msg


def hold(repo: str, prs: list[dict], read: Gh, write: Gh, summary: list[str],
         comments: Callable[[str, int], list[str]]) -> int:
    """One hold pass. Returns the errors. Never raises: a crash here must not stop the updates."""
    errors = 0
    try:
        base = json.loads(read("api", f"repos/{repo}").stdout or "{}").get("default_branch") or ""
    except (subprocess.CalledProcessError, ValueError, AttributeError):
        base = ""  # unknown: hold every base, as if no PR were stacked
    want = desired(prs, base)
    by_n = {int(p["number"]): p for p in prs}
    posted: list[str] = []
    for n, (state, desc) in sorted(want.items()):
        sha = str(by_n[n].get("headRefOid") or "")
        if len(sha) != 40:
            continue
        was = current(repo, sha, read)
        if was == state:
            continue
        err = _post(repo, sha, state, desc, write)
        if not err:
            posted.append(f"#{n} {was or 'none'}->{state}")
            continue
        errors += 1
        print(f"::error::{CONTEXT} on #{n} ({state}) was refused: {err}")
        if desc == HEAD:
            summary.append(_escalate(repo, by_n[n], sha, err, write, comments))
    head = next((n for n, (_, d) in want.items() if d == HEAD), None)
    summary.append(f"hold ({CONTEXT}): head #{head}" if head else f"hold ({CONTEXT}): no queue head")
    if posted:
        summary.append("hold posted: " + ", ".join(posted))
    return errors


def _escalate(repo: str, p: dict, sha: str, err: str, write: Gh, comments: Callable[[str, int], list[str]]) -> str:
    """The head could not be given success. First pass: a marker comment. Second: ONE page."""
    n = int(p["number"])
    try:
        marks = [m for body in comments(repo, n) for m in _HELD.findall(body) if m[0] == sha]
    except (subprocess.CalledProcessError, ValueError, OSError):
        marks = []
    if any(paged for _, paged in marks):
        return f"HEAD HELD: #{n} still not mergeable ({CONTEXT}); paged already"
    if not marks:
        body = (HELD.format(sha=sha, paged="") + f"\nThe PR queue could not mark this PR, its head, mergeable "
                f"(`{CONTEXT}` success was refused: {err}). The next pass retries and pages if it fails again.")
        write("pr", "comment", str(n), "-R", repo, "--body", body)
        return f"HEAD HELD: #{n} could not be marked mergeable ({CONTEXT}); retried next pass, paged if it fails again"
    note = page(f"{repo.rsplit('/', 1)[-1]}: queue head #{n} cannot merge",
                f"{CONTEXT} success refused on two passes: {err}", str(p.get("url") or ""))
    write("pr", "comment", str(n), "-R", repo, "--body", HELD.format(sha=sha, paged=" paged") + f"\nPaged: {note}")
    return f"HEAD HELD: #{n} not mergeable for 2 passes ({CONTEXT}); page {note}"
