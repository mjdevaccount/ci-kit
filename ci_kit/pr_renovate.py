"""Renovate's PRs JOIN THE PR QUEUE: each pass of pr-autoupdate arms GitHub auto-merge on every
Renovate PR whose preset verdict is automerge, so no session has to.

WHY (trade-data, 2026-11-14). The serial queue (ci_kit/pr_queue.py) orders ARMED PRs only, and with
the queue hold on (ci_kit/pr_hold.py) an unarmed PR reads `queue/position` pending forever. In a
serial-queue repo Renovate never merges and never arms (renovate/default.json5: `automergeType:
pr-comment`, `platformAutomerge: false`), so every automerge PR sat outside the queue until a
session ran `gh pr merge --squash --auto` on it by hand: td#3081, #3092, #3093, #3103 (opened 17:17Z).
mcp pr_reconcile's hourly arm (workstation-side) could not help in a hold repo: it arms only a PR
whose WHOLE rollup is SUCCESS, and the hold's own pending `queue/position` keeps that rollup PENDING.

THE RULE, per open Renovate PR (author the Renovate GitHub App -- a Bot, login `renovate`; a User
who registered that name is not it), unarmed, same-repo, not a draft, not a train:
  * HELD, never armed -- each named in the summary, every pass:
      - `autoland-off` as a label or in the title (the estate's opt-out; Renovate's stop label too);
      - Renovate's own verdict is not "Enabled.": the FIRST whole-line `**Automerge**:` in the body,
        which Renovate writes from the preset's automerge for THAT update (database images,
        langfuse, windmill, a major, `fails-green` all say Disabled). Upstream release notes quoted
        further down cannot flip it (mirrors mcp scripts/pr_reconcile.py `automerge_enabled`);
      - no such line at all (Renovate's onboarding PR, a PR this cannot read);
      - auto-merge was turned off BY HAND since (the PR's last AutoMergeDisabledEvent has
        reasonCode `manually_disabled`): a session's `gh pr merge --disable-auto` is a hold, as the
        preset documents, and is never undone here. Re-arm by hand to rejoin;
      - DIRTY or CONFLICTING: Renovate rebases on a conflict (`rebaseWhen: conflicted`); armed after;
      - CLEAN / UNSTABLE / HAS_HOOKS: GitHub would merge it the moment it is armed (gh's `--auto`
        merges an UNSTABLE PR at once -- past a red non-required check). Never in a hold repo (an
        unarmed PR's `queue/position` is pending, so it is BLOCKED); elsewhere it is armed once main
        moves, and mcp pr_reconcile lands a CLEAN one when nothing is in flight.
  * otherwise ARMED: GraphQL `enablePullRequestAutoMerge` (SQUASH, `expectedHeadOid` = the head this
    pass read), never `gh pr merge --auto`, which merges a CLEAN or UNSTABLE PR immediately. The
    mutation's answer is the proof: no `autoMergeRequest` back is an error. The PR is marked armed in
    this pass's rows, so the same pass orders, updates and holds it like any armed PR.

THE MERGE STANDARD is the repo's REQUIRED checks (+ `queue/position`), the one every other PR there
merges under -- the preset already says so for serial-queue repos ("The every-check gate ...
becomes the REQUIRED checks here"). Arming at once, not after a green rollup, is what a human PR
gets at open; a red required check keeps it from landing exactly as it keeps theirs.

TELEMETRY. What fails silently: an automerge PR that stays unarmed. Here every arm the token is
refused, every answer without `autoMergeRequest`, and every PR whose body/timeline could not be read
is an ERROR (`::error::`, a red pr-autoupdate run, whose red streak mcp ci-schedule-audit reads).
Each held PR is named in the run summary with why. From outside: mcp pr_reconcile's `stalled`
finding (armed hours and still open) and `red` / `conflict` findings for any Renovate PR.

TOKEN: the push token (pr-autoupdate's `token`) arms -- Pull requests: Read and write, which it
already holds. Reads (body, timeline) use the job's GITHUB_TOKEN first, like every other read.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable

from ci_kit.merge_train import OPT_OUT, TRAIN_PREFIX
from ci_kit.pr_sched import RENOVATE

Gh = Callable[..., subprocess.CompletedProcess]

_AUTOMERGE_LINE = re.compile(r"^\W*\*\*Automerge\*\*: (.*?)\s*$", re.M)
ENABLED = "Enabled."
MANUAL = "manually_disabled"  # AutoMergeDisabledEvent.reasonCode of a hand `--disable-auto`
# GitHub merges these the moment auto-merge is enabled (gh pr merge: isImmediatelyMergeable)
_MERGEABLE_NOW = {"CLEAN", "UNSTABLE", "HAS_HOOKS"}

DETAIL = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    body
    timelineItems(itemTypes: [AUTO_MERGE_DISABLED_EVENT], last: 1) { nodes {
      ... on AutoMergeDisabledEvent { createdAt reasonCode actor { login } } } }
  } }
}"""
ARM = """mutation($id: ID!, $oid: GitObjectID!) {
  enablePullRequestAutoMerge(input: {pullRequestId: $id, mergeMethod: SQUASH, expectedHeadOid: $oid}) {
    pullRequest { autoMergeRequest { enabledAt } }
  }
}"""


def is_renovate(p: dict) -> bool:
    """Opened by the Renovate GitHub App. Pure. The login alone is a name anyone may register as a
    User; the app is a Bot (GraphQL `author.__typename`), on a branch in this repo."""
    a = p.get("author") or {}
    return a.get("__typename") == "Bot" and str(a.get("login") or "") in RENOVATE and not p.get("isCrossRepository")


def candidate(p: dict) -> bool:
    """A Renovate PR this pass decides about: unarmed, not a draft, not a train. Pure."""
    return (is_renovate(p) and not p.get("autoMergeRequest") and not p.get("isDraft")
            and not str(p.get("headRefName") or "").startswith(TRAIN_PREFIX))


def automerge_verdict(body: str) -> str | None:
    """Renovate's own verdict: the FIRST whole-line `**Automerge**:` (its config block precedes the
    release notes), e.g. 'Enabled.' or 'Disabled by config. ...'. None = no such line. Pure."""
    m = _AUTOMERGE_LINE.search(body or "")
    return m.group(1) if m else None


def verdict(p: dict, body: str, disabled: dict | None) -> tuple[bool, str]:
    """(arm?, why) for one candidate. Pure: the policy the tests pin. `disabled` = the PR's last
    AutoMergeDisabledEvent ({reasonCode, createdAt, actor}) or None."""
    names = {str(lb.get("name") or "").lower() for lb in p.get("labels") or []}
    if OPT_OUT in names or OPT_OUT in str(p.get("title") or "").lower():
        return False, f"opted out (`{OPT_OUT}`)"
    v = automerge_verdict(body)
    if v is None:
        return False, "its body has no `**Automerge**:` line (not an update PR this can read)"
    if v != ENABLED:
        return False, f"Renovate's verdict is `Automerge: {v[:80]}` (the preset holds this update for a reader)"
    if disabled and disabled.get("reasonCode") == MANUAL:
        who = str((disabled.get("actor") or {}).get("login") or "someone")
        return False, (f"auto-merge was turned off by hand ({who}, {disabled.get('createdAt')}): a hold, "
                       "never undone here -- re-arm by hand to rejoin")
    merge = str(p.get("mergeStateStatus") or "UNKNOWN").upper()
    if merge == "DIRTY" or str(p.get("mergeable") or "").upper() == "CONFLICTING":
        return False, "conflicts with its base: Renovate rebases it on a conflict, and the next pass arms it"
    if merge in _MERGEABLE_NOW:
        return False, (f"mergeable as it stands ({merge}): GitHub would merge it the moment it is armed, past "
                       "the queue; armed once main moves")
    return True, "Renovate's verdict: Automerge Enabled"


def _detail(repo: str, n: int, read: Gh) -> tuple[str, dict | None]:
    owner, name = repo.split("/", 1)
    r = read("api", "graphql", "-f", f"query={DETAIL}", "-f", f"owner={owner}", "-f", f"name={name}",
             "-F", f"number={n}")
    pr = json.loads(r.stdout or "{}")["data"]["repository"]["pullRequest"]
    nodes = ((pr.get("timelineItems") or {}).get("nodes")) or []
    return str(pr.get("body") or ""), (nodes[-1] if nodes else None)


def _arm(p: dict, write: Gh) -> str:
    """'' when GitHub says auto-merge is on, else why not."""
    if not p.get("id") or len(str(p.get("headRefOid") or "")) != 40:
        return "the PR list carried no node id / head sha to arm"
    r = write("api", "graphql", "-f", f"query={ARM}", "-f", f"id={p['id']}", "-f", f"oid={p['headRefOid']}")
    if r.returncode:
        msg = " ".join((r.stderr or r.stdout or "").split())[-300:]
        if "resource not accessible" in msg.lower() or "403" in msg:
            msg += " -- the push token needs Pull requests: Read and write"
        return msg or f"gh exited {r.returncode}"
    try:
        got = json.loads(r.stdout or "{}")["data"]["enablePullRequestAutoMerge"]["pullRequest"]["autoMergeRequest"]
    except (ValueError, KeyError, TypeError):
        got = None
    return "" if got else f"GitHub answered without an autoMergeRequest: {(r.stdout or '')[:200]}"


def join(repo: str, prs: list[dict], read: Gh, write: Gh, summary: list[str]) -> int:
    """Arm every Renovate automerge PR (module doc). Marks each armed row in `prs` so the rest of
    the pass treats it as armed. Returns the errors. Never raises: a look that fails is one error."""
    errors = 0
    for p in sorted((p for p in prs if candidate(p)), key=lambda p: int(p["number"])):
        n = int(p["number"])
        try:
            body, disabled = _detail(repo, n, read)
        except (subprocess.CalledProcessError, ValueError, KeyError, TypeError, AttributeError) as e:
            errors += 1
            why = " ".join(str(getattr(e, "stderr", "") or e).split())[-200:]
            print(f"::error::#{n}: could not read Renovate PR #{n} to decide whether it joins the queue: {why}")
            summary.append(f"#{n}: Renovate PR NOT decided -- its body/timeline could not be read ({why})")
            continue
        arm, why = verdict(p, body, disabled)
        if not arm:
            summary.append(f"#{n}: Renovate PR left out of the queue -- {why}")
            continue
        err = _arm(p, write)
        if err:
            errors += 1
            print(f"::error::#{n}: Renovate automerge PR could not be armed, so it is NOT in the PR queue: {err}")
            summary.append(f"#{n}: Renovate automerge PR NOT armed ({err})")
            continue
        p["autoMergeRequest"] = {"enabledAt": "this pass"}
        summary.append(f"#{n}: Renovate PR armed ({why}) -- joins the PR queue")
    return errors
