"""Base-driven retry: an armed PR held red and BEHIND is re-queued when main has FIXED the check.

WHY (2026-11-15). pr_queue holds a BEHIND PR whose required check fails at its head: "the author's
fix re-queues it". That assumes the fix is a push to the PR. When the cause is on the BASE -- a
time-dependent test on main, fixed on main by a separate PR -- nothing ever pushes to the held PR: a
Renovate PR sat red and armed for good, and a hand update-branch is not how this estate lands work.
A held-forever PR that nothing re-examines is the defect.

WHAT ELSE WAS CHECKED. GitHub's merge queue (needs an organisation; ci-kit runs its own queue for
that reason, ci_kit/merge_train). Renovate `rebaseWhen` is `conflicted` in every serial-queue repo
(renovate/default.json5: a second updater raced the queue), so Renovate never refreshes a BEHIND
branch there. The heal ladder (ci_kit/pr_ladder.py) fixes the PR's OWN code (relock, regen, Claude)
-- the wrong place when the PR is fine and main was broken. pr_heal reruns MACHINERY reds only. None
re-merges the base into a judgment-red PR, which is the one action that can turn it green here.

THE RULE (`decide`, pure). A candidate (`candidate`: armed, same repo, not a draft/train/rider/
parked, no conflict label or `autoland-off`, BEHIND and not CONFLICTING, red on a required check
that is not machinery -- machinery is already eligible) is RETRIED when
  * EVIDENCE: every failing required check has a conclusion on a base commit the PR does not yet
    contain (main since the PR's merge base, newest concluded first) and that conclusion is GREEN.
    Absent there (a check main never runs, e.g. the PR review) or red there = held: no evidence
    the base can help. Only commits since the merge base count, so the same green cannot buy a
    second retry: after the update they ARE the merge base, and main must move again;
  * ONCE PER HEAD: no retry marker already names this head (a raced or refused PUT spends it);
  * CAP: fewer than `cap` CONSECUTIVE base-driven retries. Consecutive = the head is a 2-parent
    merge whose first parent is the last retry's head (the updater's own merge commit, or an
    equivalent hand merge of main); an author's commit, a Renovate rebase, or a plain queue
    update in between starts the count again.
At the cap the PR is SURFACED once per head (a marker comment, `::error::`, a red pass) and held.
A retry takes a queue slot only AFTER every green eligible PR (pr_queue.plan's `base_retry`), it is
never the queue head (pr_queue.blocked still counts it red), and its marker is written BEFORE the
PUT (pr_autoupdate._drive): a failed record means no update -- the record is the budget.

THE RECORD (append-only PR comments, read back as the budget, like pr_heal's):
`<!-- smart-merge:base-retry head=<sha> base=<sha> n=<k> -->` and
`<!-- smart-merge:base-retry-spent head=<sha> n=<k> -->`.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from ci_kit import redclass
from ci_kit.merge_train import OPT_OUT, RIDING, TRAIN_PREFIX

PARKED = "queue:parked"  # ci_kit/pr_sched.PARKED, literal: pr_sched imports pr_autoupdate
CAP = 2
LOOKBACK = 10  # base commits read (newest first) for a failing check's conclusion
MARK = "<!-- smart-merge:base-retry head={head} base={base} n={n} -->"
SPENT = "<!-- smart-merge:base-retry-spent head={head} n={n} -->"
_MARK = re.compile(r"<!-- smart-merge:base-retry head=([0-9a-f]{40}) base=([0-9a-f]{40}) n=(\d+) -->")
_SPENT = re.compile(r"<!-- smart-merge:base-retry-spent head=([0-9a-f]{40}) n=(\d+) -->")
_GREEN = {"SUCCESS", "NEUTRAL"}
_ABSENT = {"SKIPPED", "STALE", ""}

Gh = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class Evidence:
    """What GitHub says about one candidate: the base commits it lacks, newest first, and the head's parents."""
    base: list[str]
    parents: list[str]
    results: dict[str, str]  # failing check -> green | red | absent (newest concluded result on `base`)


# ── pure ──────────────────────────────────────────────────────────────────────────────────────
def _labels(p: dict) -> set[str]:
    return {str(lb.get("name")) for lb in p.get("labels") or []}


def candidate(p: dict, required: tuple[str, ...], conflict_label: str, machinery: frozenset[int]) -> str:
    """'' = a base-retry candidate, else why it is not. Pure."""
    labels = _labels(p)
    if not p.get("autoMergeRequest"):
        return "not armed"
    if p.get("isDraft") or p.get("isCrossRepository") or str(p.get("headRefName") or "").startswith(TRAIN_PREFIX):
        return "a draft, a fork or a train"
    if OPT_OUT in labels or OPT_OUT in str(p.get("title") or "").lower():
        return f"`{OPT_OUT}`"
    if labels & {RIDING, PARKED, conflict_label}:
        return "riding, parked or conflicted"
    if (p.get("mergeStateStatus") or "").upper() != "BEHIND" or (p.get("mergeable") or "").upper() == "CONFLICTING":
        return "not cleanly behind its base"
    if int(p["number"]) in machinery:
        return "machinery red (already eligible)"
    if not redclass.red_checks(p.get("statusCheckRollup"), required):
        return "not red"
    return ""


def chain(bodies: list[str], head: str, parents: list[str]) -> int:
    """Consecutive base-driven retries leading to `head`: the last marker's n when `head` is a merge
    whose first parent is that marker's head, else 0. Pure."""
    marks = [m.groups() for b in bodies for m in _MARK.finditer(b or "")]
    if not marks or len(parents) != 2 or parents[0] != marks[-1][0]:
        return 0
    return int(marks[-1][2])


def verdict(results: dict[str, str]) -> str:
    """'' = every failing check is green on the new base commits, else the hold reason. Pure."""
    bad = {k: v for k, v in sorted(results.items()) if v != "green"}
    if not results or bad:
        return "no evidence the base fixed it (" + ", ".join(f"`{k}` {v} on main" for k, v in bad.items()) + ")"
    return ""


def settled(head: str, bodies: list[str]) -> tuple[str, str]:
    """(action, why) when the record alone decides this head (no GitHub read owed), else ('', ''). Pure."""
    if any(m.group(1) == head for b in bodies for m in _SPENT.finditer(b or "")):
        return "quiet", "base-driven retries spent at this head, already named"
    if any(m.group(1) == head for b in bodies for m in _MARK.finditer(b or "")):
        return "hold", "already retried once from this head (base-driven)"
    return "", ""


def decide(head: str, bodies: list[str], ev: Evidence, cap: int) -> tuple[str, str, int]:
    """(action, why, n) for one candidate: retry | hold | spent | quiet. Pure: the policy the tests pin."""
    action, why = settled(head, bodies)
    if action:
        return action, why, 0
    if not ev.base:
        return "hold", "no base commits it lacks", 0
    why = verdict(ev.results)
    if why:
        return "hold", why, 0
    n = chain(bodies, head, ev.parents) + 1
    if n > cap:
        return "spent", f"{cap} base-driven retries and still red: needs the author or a session", 0
    return "retry", f"failing check(s) green on main since its merge base: base-driven retry {n} of {cap}", n


def mark_comment(head: str, base: str, n: int, cap: int, checks: list[str]) -> str:
    return "\n".join([
        MARK.format(head=head, base=base, n=n),
        f"**smart auto-merge**: red at `{head[:12]}` on {', '.join(f'`{c}`' for c in checks)}, and green on main since "
        f"this PR's merge base (`{base[:12]}`). Updating the branch from main: base-driven retry {n} of {cap}."])


def spent_comment(head: str, cap: int, checks: list[str]) -> str:
    return "\n".join([
        SPENT.format(head=head, n=cap),
        f"**smart auto-merge**: still red at `{head[:12]}` on {', '.join(f'`{c}`' for c in checks)} after {cap} "
        "consecutive base-driven retries, though main is green on it. Not retried again: the PR itself needs the author "
        "or a session. A push re-runs CI."])


# ── GitHub ────────────────────────────────────────────────────────────────────────────────────
def _lines(read: Gh, *args: str) -> list[dict]:
    return [json.loads(x) for x in (read(*args).stdout or "").splitlines() if x.strip()]


def evidence(repo: str, p: dict, base_ref: str, failing: list[str], read: Gh) -> Evidence:
    """Read the base commits the PR lacks, its head's parents, and each failing check's newest
    concluded result on those commits. Raises when GitHub will not answer."""
    head = str(p["headRefOid"])
    # paginated: one page lists the OLDEST commits, and the newest are the ones that can carry the fix
    got = read("api", "--paginate", f"repos/{repo}/compare/{head}...{base_ref}?per_page=100", "--jq", ".commits[].sha")
    base = list(reversed([x.strip() for x in (got.stdout or "").splitlines() if x.strip()]))  # newest first
    parents = json.loads(read("api", f"repos/{repo}/commits/{head}", "--jq", "[.parents[].sha]").stdout or "[]")
    results: dict[str, str] = {}
    for sha in base[:LOOKBACK]:
        todo = [c for c in failing if c not in results]
        if not todo:
            break
        seen: dict[str, str] = {}
        newest: dict[str, int] = {}  # a re-run is a newer check run at the same sha: the highest id answers
        for c in _lines(read, "api", "--paginate", f"repos/{repo}/commits/{sha}/check-runs?per_page=100",
                        "--jq", ".check_runs[] | {name, status, conclusion, id}"):
            if c.get("name") in todo and (c.get("status") or "").lower() == "completed" \
                    and int(c.get("id") or 0) >= newest.get(c["name"], 0):
                newest[c["name"]] = int(c.get("id") or 0)
                seen[c["name"]] = str(c.get("conclusion") or "").upper()
        for s in _lines(read, "api", f"repos/{repo}/commits/{sha}/status", "--jq", ".statuses[] | {context, state}"):
            if s.get("context") in todo and (s.get("state") or "").lower() != "pending":
                seen.setdefault(s["context"], "SUCCESS" if s["state"].lower() == "success" else "FAILURE")
        for name, concl in seen.items():
            if concl not in _ABSENT:
                results[name] = "green" if concl in _GREEN else "red"
    return Evidence(base, list(parents), {c: results.get(c, "absent") for c in failing})


def assess(repo: str, armed: list[dict], required: tuple[str, ...], conflict_label: str, machinery: frozenset[int],
           base_of: Callable[[dict], str], cap: int, read: Gh, comments: Callable[[str, int], list[str]], write: Gh,
           summary: list[str]) -> tuple[dict[int, str], int]:
    """({PR: the marker comment to write before its update}, errors). A spent PR is surfaced here."""
    retry: dict[int, str] = {}
    errors = 0
    if cap <= 0:
        return retry, 0
    for p in sorted(armed, key=lambda x: int(x["number"])):
        if candidate(p, required, conflict_label, machinery):
            continue
        n, head = int(p["number"]), str(p["headRefOid"])
        failing = redclass.red_checks(p.get("statusCheckRollup"), required)
        try:
            bodies = comments(repo, n)
            action, why = settled(head, bodies)
            if action:  # the record decides: no compare or check reads on every pass
                summary.append(f"#{n}: red and behind -> base-driven {action}: {why}")
                continue
            ev = evidence(repo, p, base_of(p), failing, read)
        except (subprocess.CalledProcessError, ValueError, KeyError) as e:
            errors += 1
            print(f"::error::#{n}: base-driven retry could not read its evidence: {e} {(getattr(e, 'stderr', '') or '')[-300:]}")
            summary.append(f"#{n}: red and behind, base-driven retry NOT assessed (GitHub would not say): {e}")
            continue
        action, why, k = decide(head, bodies, ev, cap)
        line = f"#{n}: red and behind -> base-driven {action}: {why}"
        if action == "retry":
            retry[n] = mark_comment(head, ev.base[0], k, cap, failing)
        elif action == "spent":
            errors += 1  # loud once per head: the run goes red, the comment is the outside record
            print(f"::error::#{n}: {why}")
            r = write("pr", "comment", str(n), "-R", repo, "--body-file", "-", input_text=spent_comment(head, cap, failing))
            line += "" if r.returncode == 0 else "; its comment FAILED"
        summary.append(line)
    return retry, errors


def record(repo: str, n: int, body: str, write: Gh) -> bool:
    """Write the retry marker BEFORE the update: no record, no update (the record is the budget)."""
    r = write("pr", "comment", str(n), "-R", repo, "--body-file", "-", input_text=body)
    if r.returncode:
        print(f"::error::#{n}: base-driven retry marker FAILED, not updated: {(r.stderr or r.stdout or '').strip()[:300]}")
    return r.returncode == 0

