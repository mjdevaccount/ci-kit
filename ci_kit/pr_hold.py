"""THE QUEUE HOLD: a commit status `queue/position` that branch protection REQUIRES, success only on
the queue HEAD's commit -- so an armed PR behind the head cannot merge out of turn, whatever its
checks say. ORDER only: it removes or weakens no check; every required check still runs and passes
at the merging head.

THE CASE (2026-11-14 06:19:20Z): td#3083 was armed and green while #3082 was the head; GitHub's
auto-merge landed #3083 first and #3082 had to re-test. pr_sched.out_of_queue PAGED that -- after
it happened. The hold makes it impossible.

Per pass of pr-autoupdate (every push to main, every last required check, the caller's cron):
  * the HEAD -> success. The head is pr_queue.head with `skip` = pr_queue.blocked -- the SAME
    head the updater and the scheduler use: the lowest-numbered armed same-repo non-draft PR into
    the default branch that CAN land -- not DIRTY, no required check red (failing or cancelled),
    except a parked or machinery red BEHIND main, whose fresh run is the queue's next update. A
    red or conflicting PR as the head would freeze every merge into main until a human fixed it;
    skipped, it is the head again the moment its fix (or re-run) leaves it running or green, and
    whatever PR held success then goes back to pending;
  * a merge train's PR (`train/...`) -> success: a train IS the queue landing;
  * every other same-repo PR -> pending, saying why (behind the head, skipped, or not in the queue
    at all: a draft or an unarmed PR -- Renovate's, whose preset never arms them, until
    pr_reconcile arms it -- joins by arming). A fork gets nothing: it is never a member.
A status is posted only when its STATE differs from the commit's current one, so a waiting PR is
not re-posted every pass; every pending goes out BEFORE the success, so a head change never has
two PRs holding success at once. A new head commit (an update, a push) has no status until the next pass,
and that pass is due: its own checks finishing trigger one.

FAIL-SAFE, AND WATCHED. The head's success is posted by ANY pass, whatever else that pass does
(the scheduler OFF, a crashed heal), and a stale pending on a PR that became head clears on the
next pass. The default branch unread (so a stacked PR cannot be told apart): the head of EVERY
base gets success, nothing is posted pending -- this pass holds nothing new -- and the run is red.
A post the token is refused is an ERROR (a red run). The head still reading not-success on a
SECOND pass is a page: the first failing pass leaves a marker comment on the PR
(`<!-- queue:head-held sha=... -->`, the pr_heal pattern), the second pages ntfy once and marks it
paged. The run's red streak is what mcp ci-schedule-audit reads. A head that HAS success, every
required check green (or never reported), current with main, and still open `stale_s` after its
last check finished is the same alarm (a required check that never reports, td#2258's shape; a
merge GitHub refuses for a reason no check shows): with the hold on, no other PR's merge moves
main to shake it loose, so the hold must say so itself.

KEEP `queue/position` OUT of the merge train's `required-checks` (and pr-autoupdate's): a member
waiting its turn reads pending, so a train would never collect it. A train's own PR gets success the
moment merge-train opens it (merge_train.phases.queue_position); every pass re-posts it. The PR list is GraphQL's first
100 open PRs, oldest first: the head is always among them; a PR past 100 has no status, so it cannot
merge until the backlog shrinks -- held, never let through.

TOKEN: the push token (PR_UPDATE_TOKEN) needs Commit statuses: Read and write. Reads use the job's
GITHUB_TOKEN (statuses: read) first. The caller turns it on with `queue-hold: true` BEFORE adding
`queue/position` to branch protection -- protection first would block every merge until a pass
proved it can post.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from collections.abc import Callable

from ci_kit import pr_queue
from ci_kit.ntfy import page, recent

CONTEXT = "queue/position"
HEAD = "head of the PR queue: may merge"
TRAIN = "a merge train lands for the queue"
SKIPPED = "skipped by the queue: a conflict or a failing required check; head again once fixed"
UNREAD = "unread"
_READ: str | None = "<read it>"  # hold(base=...) default: read the default branch here
HELD = "<!-- queue:head-held sha={sha}{paged} -->"
_HELD = re.compile(r"<!-- queue:head-held sha=([0-9a-f]{40})( paged)? -->")

Gh = Callable[..., subprocess.CompletedProcess]


def _train(p: dict) -> bool:
    return str(p.get("headRefName") or "").startswith("train/")


def desired(prs: list[dict], base: str | None, skip: frozenset[int] = frozenset()) -> dict[int, tuple[str, str]]:
    """PR number -> (state, description) for every same-repo open PR into `base` (the protected
    default branch). A stacked PR (into another PR's branch) is neither the head nor held:
    protection on its base does not ask for the status, and as the head it would hold every PR
    into main behind one that cannot land there. `base` None = the default branch is unknown: the
    head of EVERY base (and every train) gets success and nothing is held -- fail-safe. Pure."""
    same = [p for p in prs if not p.get("isCrossRepository")]
    if base is None:
        heads = [pr_queue.head([p for p in same if p.get("baseRefName") == b], skip)
                 for b in sorted({str(p.get("baseRefName") or "") for p in same})]
        out = {int(h["number"]): ("success", HEAD) for h in heads if h}
        return out | {int(p["number"]): ("success", TRAIN) for p in same if _train(p)}
    same = [p for p in same if p.get("baseRefName") == base]
    h = pr_queue.head(same, skip)
    hn = int(h["number"]) if h else None
    out = {}
    for p in same:
        n = int(p["number"])
        if _train(p):
            out[n] = ("success", TRAIN)
        elif n == hn:
            out[n] = ("success", HEAD)
        elif pr_queue.member(p) and n in skip:
            out[n] = ("pending", SKIPPED)
        elif pr_queue.member(p):
            out[n] = ("pending", f"waits its turn: the queue head is #{hn}")
        else:
            out[n] = ("pending", "not in the PR queue (a draft, or not armed): arm auto-merge to join it")
    return out


def current(repo: str, sha: str, read: Gh) -> tuple[str | None, str]:
    """(the commit's `queue/position` state, when it was set) from the COMBINED status (newest per
    context, however many other statuses the commit carries). None = it has none; UNREAD = the read
    failed, and then the state is posted anyway: a redundant post costs nothing, a missed one holds a PR."""
    try:
        rows = json.loads(read("api", f"repos/{repo}/commits/{sha}/status").stdout or "{}").get("statuses") or []
    except (subprocess.CalledProcessError, ValueError, AttributeError, TypeError):
        return UNREAD, ""
    s = next((s for s in rows if isinstance(s, dict) and s.get("context") == CONTEXT), None)
    return (str(s.get("state")), str(s.get("updated_at") or "")) if s else (None, "")


def _epoch(s: str) -> float:
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def stuck(p: dict, since: str, required: tuple[str, ...], now: float, stale_s: float) -> str:
    """'' or why the head, holding success, has not merged and nothing will move it: every required
    check green or absent (redclass.red_checks empty, nothing running), current with main, and
    `stale_s` past both its success and its last check finishing. Pure."""
    rollup = p.get("statusCheckRollup") or []
    if pr_queue.check_state(rollup, required) != "green" or \
            (p.get("mergeStateStatus") or "UNKNOWN").upper() in ("BEHIND", "DIRTY", "UNKNOWN"):
        return ""
    last = max([_epoch(since)] + [_epoch(str(c.get("completedAt") or c.get("createdAt") or "")) for c in rollup])
    if not last or now - last < stale_s:
        return ""
    return (f"has held `{CONTEXT}` success with its required checks green for {(now - last) / 60:.0f} min and has not "
            f"merged (merge state {p.get('mergeStateStatus')}): a required check that never reported, or a merge "
            "GitHub refuses -- nothing else can merge meanwhile")


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


def default_branch(repo: str, read: Gh) -> str | None:
    """The protected default branch; None when it could not be read."""
    try:
        return json.loads(read("api", f"repos/{repo}").stdout or "{}").get("default_branch") or None
    except (subprocess.CalledProcessError, ValueError, AttributeError):
        return None


def hold(repo: str, prs: list[dict], read: Gh, write: Gh, summary: list[str],
         comments: Callable[[str, int], list[str]], skip: frozenset[int] = frozenset(), *,
         base: str | None = _READ, stale_s: float = 1800, required: tuple[str, ...] = (),
         now: float | None = None) -> int:
    """One hold pass; `skip` = pr_queue.blocked(...). Returns the errors. NEVER raises: the caller
    runs it even when the pass around it crashes, and a crash in here (a missing gh binary, an
    answer of the wrong shape) is one error -- a red run -- never a silent skip."""
    try:
        return _hold(repo, prs, read, write, summary, comments, skip, base, stale_s, required, now)
    except Exception as e:  # noqa: BLE001 -- the guard IS the point: the run goes red, it never crashes the pass
        print(f"::error::{CONTEXT}: the hold pass crashed: {type(e).__name__}: {e}")
        summary.append(f"hold ({CONTEXT}): CRASHED ({type(e).__name__}: {e}) -- statuses as last posted")
        return 1


def _hold(repo: str, prs: list[dict], read: Gh, write: Gh, summary: list[str],
          comments: Callable[[str, int], list[str]], skip: frozenset[int], base: str | None, stale_s: float,
          required: tuple[str, ...], now: float | None) -> int:
    errors = 0
    now = dt.datetime.now(dt.UTC).timestamp() if now is None else now
    base = default_branch(repo, read) if base == _READ else base
    if base is None:
        errors += 1
        print(f"::error::{CONTEXT}: could not read {repo}'s default branch -- every base's head marked, nothing held")
        summary.append(f"hold ({CONTEXT}): default branch UNREAD -- every base's head marked, nothing held this pass")
    want = desired(prs, base, skip)
    by_n = {int(p["number"]): p for p in prs}
    posted: list[str] = []
    unread = 0
    # every pending before any success: a head change never leaves two PRs holding success
    for n, (state, desc) in sorted(want.items(), key=lambda kv: (kv[1][0] == "success", kv[0])):
        sha = str(by_n[n].get("headRefOid") or "")
        if len(sha) != 40:
            continue
        was, since = current(repo, sha, read)
        unread += was == UNREAD
        if was == state:
            why = stuck(by_n[n], since, required, now, stale_s) if desc == HEAD else ""
            if why:
                errors += 1
                print(f"::error::queue head #{n} {why}")
                summary.append(_escalate(repo, by_n[n], sha, why, write, comments))
            continue
        err = _post(repo, sha, state, desc, write)
        if not err:
            posted.append(f"#{n} {was or 'none'}->{state}")
            continue
        errors += 1
        print(f"::error::{CONTEXT} on #{n} ({state}) was refused: {err}")
        if desc == HEAD:
            summary.append(_escalate(repo, by_n[n], sha, f"`{CONTEXT}` success was refused: {err}", write, comments))
    heads = sorted(n for n, (_, d) in want.items() if d == HEAD)
    summary.append(f"hold ({CONTEXT}): head #{', #'.join(map(str, heads))}" if heads else f"hold ({CONTEXT}): no queue head")
    skipped = sorted(n for n, (_, d) in want.items() if d == SKIPPED)
    if skipped:
        summary.append("hold skips (cannot land as they stand): #" + ", #".join(map(str, skipped)))
    if posted:
        summary.append("hold posted: " + ", ".join(posted))
    if unread:
        summary.append(f"hold: {unread} status read(s) failed -- posted without knowing the current state")
    return errors


def _escalate(repo: str, p: dict, sha: str, why: str, write: Gh, comments: Callable[[str, int], list[str]]) -> str:
    """The head cannot merge (`why`). First pass: a marker comment. Second: ONE page. A comment
    that cannot be read counts as no marker: at worst one extra comment, never a lost page."""
    n = int(p["number"])
    try:
        marks = [m for body in comments(repo, n) for m in _HELD.findall(body) if m[0] == sha]
    except (subprocess.CalledProcessError, ValueError, OSError):
        marks = []
    if any(paged for _, paged in marks):
        return f"HEAD HELD: #{n} still cannot merge ({CONTEXT}); paged already"
    if not marks:
        body = (HELD.format(sha=sha, paged="") + f"\nThe PR queue's head cannot merge: {why}. "
                "The next pass looks again and pages if it still cannot.")
        if write("pr", "comment", str(n), "-R", repo, "--body", body).returncode == 0:
            return f"HEAD HELD: #{n} cannot merge ({why}); paged next pass if it still cannot"
        # the marker was refused (a dead push token refuses statuses AND comments): without a
        # marker no later pass could ever page, so page now -- ntfy needs no GitHub token -- and
        # dedup on the topic itself (every pass would page otherwise: one per CI completion)
        why += " (and the marker comment was refused: the push token)"
    title = f"{repo.rsplit('/', 1)[-1]}: queue head #{n} cannot merge"
    if not marks and recent(title):
        return f"HEAD HELD: #{n} cannot merge ({why}); paged already (ntfy, last 6h)"
    try:
        note = page(title, f"two passes: {why}"[:900], str(p.get("url") or ""))
    except Exception as e:  # noqa: BLE001 -- page() catches OSError only (a malformed URL is ValueError)
        note = f"push FAILED: {e}"
    write("pr", "comment", str(n), "-R", repo, "--body", HELD.format(sha=sha, paged=" paged") + f"\nPaged: {note}")
    return f"HEAD HELD: #{n} cannot merge for 2 passes ({why}); page {note}"
