"""Renovate's PRs JOIN THE PR QUEUE: each pass of pr-autoupdate arms GitHub auto-merge on every
Renovate PR whose preset verdict is automerge, so no session has to.

WHY (a consumer, 2026-11-14). The serial queue (ci_kit/pr_queue.py) orders ARMED PRs only, and with
the queue hold on (ci_kit/pr_hold.py) an unarmed PR reads `queue/position` pending forever. In a
serial-queue repo Renovate never merges and never arms (renovate/default.json5: `automergeType:
pr-comment`, `platformAutomerge: false`), so every automerge PR sat outside the queue until a
session ran `gh pr merge --squash --auto` on it by hand: #3081, #3092, #3093, #3103 (opened 17:17Z).
An outside reconciler's hourly arm (off the runners) could not help in a hold repo: it arms only a PR
whose WHOLE rollup is SUCCESS, and the hold's own pending `queue/position` keeps that rollup PENDING.

THE RULE, per open Renovate PR (author the Renovate GitHub App -- a Bot, login `renovate`; a User
who registered that name is not it), unarmed, same-repo, not a draft, not a train:
  * HELD, never armed -- each named in the summary, every pass:
      - `autoland-off` as a label or in the title (the estate's opt-out; Renovate's stop label too);
      - Renovate's own verdict is not "Enabled.": the `🚦 **Automerge**:` line of its config block
        (after the LAST `### Configuration` heading), which Renovate writes from the preset's
        automerge for THAT update (database images, langfuse, windmill, a major, `fails-green` all
        say Disabled). The changelogs come BEFORE that block, so upstream text cannot flip it
        (`automerge_verdict`);
      - no such line at all (Renovate's onboarding PR, a PR this cannot read);
      - auto-merge was turned off BY HAND since (the PR's last AutoMergeDisabledEvent has
        reasonCode `manually_disabled`): a session's `gh pr merge --disable-auto` is a hold, as the
        preset documents, and is never undone here. Re-arm by hand to rejoin;
      - DIRTY or CONFLICTING: the pass ticks the PR's rebase box (`ticked`), Renovate recreates the
        branch on current main, and a later pass arms it. Needed since the preset's kit rule
        (`rebaseWhen: never` in queue repos, v1.32.0): Renovate never moves that branch by itself.
        An ARMED one that goes DIRTY later is ticked too (a conflict never disarms a PR);
        A refused tick is a red run -- untouched, the PR would sit forever;
      - CLEAN / UNSTABLE / HAS_HOOKS: GitHub would merge it the moment it is armed (gh's `--auto`
        merges an UNSTABLE PR at once -- past a red non-required check). Never in a hold repo (an
        unarmed PR's `queue/position` is pending, so it is BLOCKED); elsewhere it is armed once main
        moves. UNSTABLE stays held (a check is red or still pending: `renovate/stability-days`).
        CLEAN is LANDED by this pass when the queue is IDLE (`land_idle`, below).
  * LANDED, when the queue is idle (v1.60.0): nothing in flight, nothing to attempt, no train open.
    "Armed once main moves" waited on a push that a quiet repo never gets: trade-infra#977/#978
    (2026-11-28) sat CLEAN, every check green, Renovate's verdict Enabled, under `queue: 0/1 in
    flight, 0 to attempt` for every pass until a session merged them by hand -- Renovate never
    merges here (`pr-comment`) and GitHub refuses to ARM a CLEAN PR ("clean status", `_RACE`).
    So the lowest-numbered such PR is MERGED, squash, pinned to the head this pass read
    (`mergePullRequest` expectedHeadOid), only when GitHub calls it CLEAN (every check green,
    required or not: a pending or red non-required one reads UNSTABLE), its own rollup reads green
    here too -- STRICTLY: a non-empty rollup, every check SUCCESS/SKIPPED/NEUTRAL, every required
    name present -- and every workflow run on that head has COMPLETED (a relock still queued would
    push after the merge). A head BEHIND its base is UPDATED instead (the queue's own PUT
    update-branch): a NON-strict base never reports BEHIND, so CLEAN there is not "tested against
    today's main", and after one land the next would merge on a base its checks never saw. ONE per
    pass. Idle means nothing in flight, nothing to attempt but DIRTY PRs (they move nothing), no
    armed HELD PR that is not failing (it waits its turn; a land would jump it), no open train.
    A refused merge is a red run; a head or base that moved since the read, or a PR another actor
    merged first, is a race (next pass).
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
is an ERROR (`::error::`, a red pr-autoupdate run, whose red streak an outside schedule audit reads) --
except GitHub refusing because the PR turned mergeable-now or its head moved since the list was read
(`raced`): the next pass reads the new state, so that is a summary line, not a red.
Each held PR is named in the run summary with why. From outside: a PR reconciler's `stalled`
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

# Renovate's own config block: the LAST `### Configuration` heading (prBodyTemplate puts it AFTER the
# changelogs, whose upstream text may carry any heading or marker), then its `🚦 **Automerge**:` line
_CONFIG = re.compile(r"^### Configuration[ \t]*\r?$", re.M)
_AUTOMERGE_LINE = re.compile(r"^\U0001f6a6 \*\*Automerge\*\*: (.*?)\s*$", re.M)
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
LAND = """mutation($id: ID!, $oid: GitObjectID!) {
  mergePullRequest(input: {pullRequestId: $id, mergeMethod: SQUASH, expectedHeadOid: $oid}) {
    pullRequest { state merged }
  }
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


def conflicted(p: dict) -> bool:
    """GitHub reports a merge conflict with the base. Pure."""
    return (str(p.get("mergeStateStatus") or "").upper() == "DIRTY"
            or str(p.get("mergeable") or "").upper() == "CONFLICTING")


def _opted_out(p: dict) -> bool:
    names = {str(lb.get("name") or "").lower() for lb in p.get("labels") or []}
    return OPT_OUT in names or OPT_OUT in str(p.get("title") or "").lower()


def candidate(p: dict) -> bool:
    """A Renovate PR this pass decides about: unarmed, not a draft, not a train. Pure."""
    return (is_renovate(p) and not p.get("autoMergeRequest") and not p.get("isDraft")
            and not str(p.get("headRefName") or "").startswith(TRAIN_PREFIX))


def automerge_verdict(body: str) -> str | None:
    """Renovate's own verdict, e.g. 'Enabled.' or 'Disabled by config. ...'; None = unreadable. Pure.

    Read ONLY after the LAST `### Configuration` heading: Renovate's default prBodyTemplate is
    header, table, notes, CHANGELOGS, then the config block, so upstream release notes (text the
    upstream maintainer writes) come FIRST and may carry `- **Automerge**: Enabled.` -- a first-match
    read armed a held database update on it (reviewer, 2026-11-14, on a live consumer PR's layout).
    Nothing after the config block is upstream text (controls, footer)."""
    heads = list(_CONFIG.finditer(body or ""))
    if not heads:
        return None
    m = _AUTOMERGE_LINE.search(body, heads[-1].end())
    return m.group(1) if m else None


def automerge_enabled(body: str) -> bool:
    """Renovate will merge this PR itself (its config block says Enabled). Pure; the heal ladder's
    `armed` for a Renovate PR in a repo with no queue, where nothing arms it (ci_kit/pr_ladder.py)."""
    return automerge_verdict(body) == ENABLED


# Renovate's own control (its PR body's "rebase/retry" checkbox): ticked, its next run recreates the
# branch on current main -- the one way to move a `rebaseWhen: "never"` branch (the preset's kit rule)
_REBASE_BOX = re.compile(r"^([ \t]*- )\[ \]([ \t]*<!-- rebase-check -->)", re.M)
CONFLICT = "conflicts with its base: Renovate recreates it on current main, and the next pass arms it"


def ticked(body: str) -> str | None:
    """`body` with Renovate's rebase box ticked; None when it has no UNticked box (already asked,
    or not Renovate's layout). Only after the last `### Configuration`, like the verdict. Pure."""
    heads = list(_CONFIG.finditer(body or ""))
    if not heads:
        return None
    at = heads[-1].end()
    tail, k = _REBASE_BOX.subn(r"\1[x]\2", body[at:], count=1)
    return body[:at] + tail if k else None


STABILITY = "renovate/stability-days"  # Renovate's minimumReleaseAge status: PENDING until the age passes
AGE_HELD = (f"HELD BY POLICY, not stuck: `{STABILITY}` is pending (the preset's minimumReleaseAge, the "
            "supply-chain age gate); it joins the queue by itself once that status passes")


def age_held(p: dict) -> bool:
    """`renovate/stability-days` is on the head and not yet success. Pure. Such a PR is HELD BY
    POLICY (operator 2026-11-28: shown as held, never as stuck), never armed and never landed."""
    for c in p.get("statusCheckRollup") or []:
        if (c.get("context") or c.get("name")) == STABILITY:
            return str(c.get("state") or c.get("conclusion") or "").upper() != "SUCCESS"
    return False


def verdict(p: dict, body: str, disabled: dict | None) -> tuple[bool, str]:
    """(arm?, why) for one candidate. Pure: the policy the tests pin. `disabled` = the PR's last
    AutoMergeDisabledEvent ({reasonCode, createdAt, actor}) or None."""
    if _opted_out(p):
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
    if conflicted(p):
        return False, CONFLICT
    if age_held(p):
        # BEFORE the arm: armed, GitHub merges on the REQUIRED checks only, and stability-days is
        # never required -- the arm would land it past the supply-chain age gate the preset sets
        return False, AGE_HELD
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


# GitHub's refusals for a state this pass read a moment too early: the next pass decides afresh
_RACE = ("clean status", "unstable status", "has_hooks status", "head sha", "head ref", "expected head")


def raced(err: str) -> bool:
    """A refused arm that is a race, not a failure: the PR became mergeable-now (an UNKNOWN that was
    CLEAN) or Renovate pushed after the list was read (expectedHeadOid). Pure."""
    return any(r in err.lower() for r in _RACE)


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


def _ask_rebase(repo: str, n: int, body: str, write: Gh) -> tuple[str, int]:
    """Tick Renovate's rebase box on a conflicting automerge PR: (why, errors). Kit branches in
    queue repos are `rebaseWhen: "never"` (the preset), so Renovate itself never moves them, and
    neither can the queue (its update of a conflicting PR is refused) -- unasked, armed or not, this
    PR would sit forever. The body goes on stdin: a Renovate body runs to ~60k characters, and as one
    `-f` argument a multi-byte one passes Linux's 128 KiB single-argument cap."""
    new = ticked(body)
    if new is None:
        return CONFLICT + " (its rebase box is already ticked, or absent)", 0
    r = write("api", "-X", "PATCH", f"repos/{repo}/pulls/{n}", "--input", "-",
              input_text=json.dumps({"body": new}))
    if r.returncode:
        msg = " ".join((r.stderr or r.stdout or "").split())[-200:]
        print(f"::error::#{n}: Renovate PR conflicts and its rebase box could not be ticked, so nothing "
              f"will move it: {msg}")
        return CONFLICT + f" -- rebase box NOT ticked ({msg})", 1
    return CONFLICT + " (rebase box ticked this pass)", 0


def join(repo: str, prs: list[dict], read: Gh, write: Gh, summary: list[str]) -> int:
    """Arm every Renovate automerge PR (module doc). Marks each armed row in `prs` so the rest of
    the pass treats it as armed. Returns the errors. Never raises: a look that fails is one error."""
    errors = 0
    # unarmed: decide whether it joins. ARMED and conflicting: GitHub never disarms on a conflict
    # (a consumer's AutoMergeDisabledEvents: manually_disabled, closed, head_mismatch only), so an
    # armed kit PR that went DIRTY is no candidate yet still needs its rebase box (reviewer, 11-14)
    look = [p for p in prs if candidate(p) or (is_renovate(p) and p.get("autoMergeRequest")
                                               and not p.get("isDraft") and conflicted(p))]
    for p in sorted(look, key=lambda p: int(p["number"])):
        n = int(p["number"])
        try:
            body, disabled = _detail(repo, n, read)
        except (subprocess.CalledProcessError, ValueError, KeyError, TypeError, AttributeError) as e:
            errors += 1
            why = " ".join(str(getattr(e, "stderr", "") or e).split())[-200:]
            print(f"::error::#{n}: could not read Renovate PR #{n} to decide whether it joins the queue: {why}")
            summary.append(f"#{n}: Renovate PR NOT decided -- its body/timeline could not be read ({why})")
            continue
        if p.get("autoMergeRequest"):
            if automerge_verdict(body) == ENABLED and not _opted_out(p):
                why, failed = _ask_rebase(repo, n, body, write)
                errors += failed
                summary.append(f"#{n}: armed Renovate PR {why}")
            continue
        arm, why = verdict(p, body, disabled)
        if not arm:
            if why.startswith("mergeable as it stands") and idle_landable(p):
                p[LANDABLE] = True  # land_idle's candidate: Enabled, not held, CLEAN (decided above)
            if why == CONFLICT:
                why, failed = _ask_rebase(repo, n, body, write)
                errors += failed
            summary.append(f"#{n}: Renovate PR left out of the queue -- {why}")
            continue
        err = _arm(p, write)
        if err and raced(err):
            summary.append(f"#{n}: Renovate PR not armed this pass -- GitHub: {err[:160]} (decided afresh next pass)")
            continue
        if err:
            errors += 1
            print(f"::error::#{n}: Renovate automerge PR could not be armed, so it is NOT in the PR queue: {err}")
            summary.append(f"#{n}: Renovate automerge PR NOT armed ({err})")
            continue
        p["autoMergeRequest"] = {"enabledAt": "this pass"}
        summary.append(f"#{n}: Renovate PR armed ({why}) -- joins the PR queue")
    return errors


# ---- the idle queue lands a CLEAN Renovate PR itself (v1.60.0, module doc "LANDED") -------------
LANDABLE = "renovateLandable"  # row key join() sets on a CLEAN Enabled candidate it left unarmed
_LAND_STATES = {"CLEAN", "HAS_HOOKS"}  # every check green; UNSTABLE (a red/pending one) never


_GREEN_RUN = {"SUCCESS", "SKIPPED", "NEUTRAL"}  # GitHub's passing conclusions; CANCELLED/STALE are not
# GitHub's answers to mergePullRequest that are a race, not a failure: the head or base moved since this
# pass read them, or another actor merged it first (verbatim: "Head branch was modified. Review and try
# the merge again.")
_LAND_RACE = ("head branch was modified", "base branch was modified", "already merged")


def _strict_green(rollup: list[dict] | None, required: tuple[str, ...]) -> bool:
    """Every check on the head PASSED (none pending, none cancelled), there is at least one, and every
    REQUIRED name is among them. Stricter than pr_queue.check_state, which reads an empty rollup and a
    cancelled run as green: a merge is the one act here nobody re-decides. Pure."""
    seen: set[str] = set()
    for c in rollup or []:
        seen.add(str(c.get("name") or c.get("context") or ""))
        if c.get("__typename") == "StatusContext" or ("state" in c and "status" not in c):
            if str(c.get("state") or "").upper() != "SUCCESS":
                return False
        elif str(c.get("status") or "").upper() != "COMPLETED" or \
                str(c.get("conclusion") or "").upper() not in _GREEN_RUN:
            return False
    return bool(seen) and all(r in seen for r in required)


def idle_landable(p: dict, required: tuple[str, ...] = ()) -> bool:
    """GitHub calls it CLEAN (or HAS_HOOKS) and every check on its head passed here too. Pure."""
    merge = str(p.get("mergeStateStatus") or "").upper()
    return merge in _LAND_STATES and _strict_green(p.get("statusCheckRollup"), required)


def idle(prs: list[dict], plan, required: tuple[str, ...] = ()) -> str:
    """'' when the queue is idle (a land jumps nothing), else why not. Pure.

    Busy = a PR in flight, one to attempt that is not DIRTY (a conflicting one moves nothing until its
    author resolves it: it must not block a land for ever), an armed HELD one that is not failing (it is
    waiting its turn -- checks running, a review, a slot; landing past it jumps it), or an open train."""
    from ci_kit.pr_queue import check_state
    if plan.in_flight:
        return f"#{plan.in_flight[0]['number']} is in flight"
    nxt = next((p for p in plan.update if str(p.get("mergeStateStatus") or "").upper() != "DIRTY"), None)
    if nxt:
        return f"#{nxt['number']} is next in the queue"
    held = next((p for p, _ in plan.held if check_state(p.get("statusCheckRollup"), required) != "failing"), None)
    if held:
        return f"armed #{held['number']} is held, waiting its turn"
    train = next((p for p in prs if str(p.get("headRefName") or "").startswith(TRAIN_PREFIX)), None)
    return f"merge train #{train['number']} is open" if train else ""


def _runs_done(repo: str, oid: str, read: Gh) -> str:
    """'' when every workflow run on the head has completed, else why not (a relock or a slow
    workflow that has not posted its check yet would act after the merge)."""
    r = read("api", f"repos/{repo}/actions/runs?head_sha={oid}&per_page=100", "--jq",
             "[.workflow_runs[] | select(.status != \"completed\") | .name]")
    names = json.loads(r.stdout or "[]")
    return f"workflow run(s) still running on its head: {', '.join(sorted(set(names)))}" if names else ""


def _behind(repo: str, p: dict, read: Gh) -> int:
    """How many base commits the head lacks. A NON-strict base never reports BEHIND, so CLEAN there
    does not mean "tested against today's main": after one land the next CLEAN one would merge on a
    base its checks never saw (reviewer, 2026-11-28; trade-infra and local-mcp are strict:false)."""
    r = read("api", f"repos/{repo}/compare/{p.get('baseRefName') or 'main'}...{p['headRefOid']}?per_page=1",
             "--jq", ".behind_by")  # the shape doomed_runs.Canceller._behind reads; per_page=1: no commit list
    return int((r.stdout or "").strip())


def land_idle(repo: str, prs: list[dict], plan, required: tuple[str, ...], read: Gh, write: Gh,
              update: Callable[[dict], tuple[str, str]], summary: list[str]) -> int:
    """MERGE the lowest-numbered landable Renovate PR when the queue is idle (module doc). A head
    behind its base is UPDATED instead (`update` = the queue's own PUT update-branch -> (kind,
    detail)): its fresh checks decide, and the next pass lands or arms it. Returns the errors.
    Never raises."""
    todo = sorted((p for p in prs if p.get(LANDABLE) and idle_landable(p, required)), key=lambda p: int(p["number"]))
    if not todo:
        return 0
    why = idle(prs, plan, required)
    if why:
        summary.append(f"Renovate PR(s) {', '.join('#' + str(p['number']) for p in todo)} CLEAN, not landed: {why} "
                       "(the queue's order wins; decided afresh once it is idle)")
        return 0
    p, n = todo[0], int(todo[0]["number"])
    try:
        busy = _runs_done(repo, str(p["headRefOid"]), read)
        behind = 0 if busy else _behind(repo, p, read)
    except (subprocess.CalledProcessError, ValueError, TypeError) as e:
        print(f"::error::#{n}: could not read its head's workflow runs or its distance from base, so it was not "
              f"landed: {' '.join(str(getattr(e, 'stderr', '') or e).split())[-200:]}")
        summary.append(f"#{n}: CLEAN Renovate PR NOT landed -- its head's runs or base distance could not be read")
        return 1
    if busy:
        summary.append(f"#{n}: CLEAN Renovate PR not landed yet -- {busy} (the run's completion starts the next pass)")
        return 0
    if behind > 0:
        kind, detail = update(p)
        if kind == "error":
            print(f"::error::#{n}: CLEAN Renovate PR is {behind} commit(s) behind its base and could not be "
                  f"updated, so it sits green and unmerged: {detail}")
            summary.append(f"#{n}: CLEAN Renovate PR NOT landed -- {behind} behind base, update failed ({detail[:160]})")
            return 1
        summary.append(f"#{n}: CLEAN Renovate PR not landed -- {behind} commit(s) behind its base (a non-strict "
                       f"base): update-branch {kind}; its fresh checks decide next pass")
        return 0
    r = write("api", "graphql", "-f", f"query={LAND}", "-f", f"id={p['id']}", "-f", f"oid={p['headRefOid']}")
    msg = " ".join((r.stderr or r.stdout or "").split())[-300:]
    try:
        got = json.loads(r.stdout or "{}")["data"]["mergePullRequest"]["pullRequest"]
    except (ValueError, KeyError, TypeError):
        got = {}
    if not r.returncode and got.get("merged"):
        p[LANDABLE] = False
        summary.append(f"#{n}: Renovate PR LANDED (CLEAN, every check green, queue idle) -- squash, head "
                       f"{str(p['headRefOid'])[:12]}")
        return 0
    if raced(msg) or any(x in msg.lower() for x in _LAND_RACE):
        summary.append(f"#{n}: CLEAN Renovate PR not landed this pass -- GitHub: {msg[:160]} (decided afresh next pass)")
        return 0
    print(f"::error::#{n}: CLEAN Renovate automerge PR could not be landed, so it sits green and unmerged: "
          f"{msg or 'GitHub answered without merged=true'}")
    summary.append(f"#{n}: CLEAN Renovate PR NOT landed ({msg[:200] or 'no merged=true in the answer'})")
    return 1
