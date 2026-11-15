"""Base-driven retry: an armed PR held red and BEHIND is re-queued when MAIN went red -> green on its check.

WHY (2026-11-15). pr_queue holds a BEHIND PR whose required check fails at its head: "the author's
fix re-queues it". That assumes the fix is a push to the PR. When the cause is on the BASE and the
fix lands on main, nothing ever pushes to the held PR: an armed Renovate PR sat red for good, and a
hand update-branch is not how this estate lands work.

WHAT ELSE WAS CHECKED. GitHub's merge queue (needs an organisation; ci-kit runs its own queue for
that reason, ci_kit/merge_train). Renovate `rebaseWhen` is `conflicted` in every serial-queue repo
(renovate/default.json5), so Renovate never refreshes a BEHIND branch there. The heal ladder
(ci_kit/pr_ladder.py) fixes the PR's OWN code; pr_heal reruns MACHINERY reds only. None re-merges
the base into a judgment-red PR.

THE EVIDENCE RULE: A RED -> GREEN TRANSITION ON MAIN, per failing check. "Green on newer main" alone
is no evidence: under strict protection main is green by construction, so it holds for EVERY
judgment red (review of 6e69e0d, finding 1). So for each failing required check, identified by
(app, workflow file, name) -- a status by its context -- BOTH must hold:
  * RED AT ITS BASE: a finished failing run of that check on the PR's merge-base commit or one of
    the `NEAR` commits just before it (commits the PR already contains). Skipped, neutral, stale,
    cancelled or absent is not red;
  * GREEN SINCE: the newest finished result on the main commits the PR LACKS (newest first, at
    most `LOOKBACK`) is SUCCESS. Neutral and skipped are not green.
Main never red there = HELD, the old behaviour. WHAT IT DOES NOT COVER: a check that is red only on
the PR's run while main's own runs stayed green -- e.g. a time-dependent test that started failing
after main's last run at the PR's base. Under strict protection main records no red then, so this
rule holds that PR exactly as before. It fires only when main ITSELF recorded the red (a scheduled
or re-run on main, a push that bypassed the gate) and then recorded the green.

THE BOUNDS. ONCE PER HEAD (a retry marker at the head holds it; a raced PUT spends it). A CAP on
retries since the last NON-UPDATER commit: walking first parents through 2-parent merges (the
updater's own, retry or plain update alike, or an equivalent hand merge of main), the retry markers
on that chain are counted; an author commit or a rebase ends the chain. At the cap: ONE comment per
head, `::error::`, a red pass, then held. ONE ACTOR PER HEAD: a head the heal ladder took is never
base-retried, and pr_autoupdate gives a PR this pass retried to neither the ladder nor pr_heal.

TRUST. A marker GRANTS a retry budget only when the token's OWN identity wrote it (`comments_by` +
`whoami`); counts come from markers along the head's ancestry, never from a number in a comment. A
forged retry marker is ignored (it neither grants nor resets); the heal ladder's markers are read
from every author, which is fail-safe here: a forged one can only HOLD a PR.

ORDER. A retry takes a queue slot only AFTER every green eligible PR (pr_queue.plan's `base_retry`),
is never the queue head (pr_queue.blocked still counts it red), and its marker is written BEFORE the
PUT (pr_autoupdate._drive): no record, no update. Default OFF (`base-retry-cap` 0): nothing is read.

THE RECORD: `<!-- smart-merge:base-retry head=<sha> base=<sha> -->`, `<!-- smart-merge:base-retry-spent head=<sha> -->`.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

from ci_kit import pr_ladder, redclass
from ci_kit.merge_train import OPT_OUT, RIDING, TRAIN_PREFIX

PARKED = "queue:parked"  # ci_kit/pr_sched.PARKED, literal: pr_sched imports pr_autoupdate
CAP = 0          # default OFF: one repo turns it on first and is watched
NEAR = 3         # commits before the merge base read for the RED observation
LOOKBACK = 10    # main commits the PR lacks read (newest first) for the GREEN one
CHAIN = 30       # first-parent merge steps walked when counting retries
MARK = "<!-- smart-merge:base-retry head={head} base={base} -->"
SPENT = "<!-- smart-merge:base-retry-spent head={head} -->"
_MARK = re.compile(r"<!-- smart-merge:base-retry head=([0-9a-f]{40})\b")
_SPENT = re.compile(r"<!-- smart-merge:base-retry-spent head=([0-9a-f]{40})\b")
_RED = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ERROR"}
_GREEN = {"SUCCESS"}

Gh = Callable[..., subprocess.CompletedProcess]
Key = tuple[str, str, str]  # (app slug | "status", workflow path, check name | status context)


@dataclass(frozen=True)
class Run:
    key: Key
    conclusion: str  # upper case; '' = not finished
    id: int


@dataclass
class Evidence:
    """What GitHub says about one candidate."""
    failing: list[Key]                      # the PR's failing required checks, fully identified
    near: list[list[Run]]                   # merge base, then NEAR commits before it (newest first)
    lacking: list[list[Run]]                # main commits the PR lacks, newest first
    base: str = ""                          # main's newest commit the PR lacks
    unread: list[str] = field(default_factory=list)


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


def trusted(comments: list[tuple[str, str]], me: str, pattern: re.Pattern[str]) -> set[str]:
    """The heads named by `pattern` markers the token's own identity wrote. Pure."""
    return {m.group(1) for who, body in comments if me and who == me for m in pattern.finditer(body or "")}


def red_at_base(key: Key, near: list[list[Run]]) -> bool:
    """A finished FAILING run of `key` on the merge base or the commits just before it. Pure."""
    return any(r.key == key and r.conclusion in _RED for runs in near for r in runs)


def transition(key: Key, ev: Evidence) -> str:
    """'' = main went RED (at the PR's base) -> GREEN (since) on `key`, else the hold reason. Pure."""
    name = key[2] if not key[1] else f"{key[2]} ({key[1]})"
    if not red_at_base(key, ev.near):
        return f"`{name}` never red on main at its base (main was fine: the red is the PR's)"
    for runs in ev.lacking:
        done = sorted((r for r in runs if r.key == key and r.conclusion), key=lambda r: -r.id)
        done = [r for r in done if r.conclusion not in ("SKIPPED", "NEUTRAL", "STALE", "CANCELLED")]
        if done:
            return "" if done[0].conclusion in _GREEN else f"`{name}` still {done[0].conclusion.lower()} on main"
    return f"`{name}` has no finished result on main since its merge base"


def count(head: str, parents: Callable[[str], list[str]], retried: set[str], cap: int, limit: int = CHAIN) -> int:
    """Retries since the last non-updater commit: walk first parents through 2-parent merges from
    `head`, counting the (trusted) retried heads met. An author commit or a rebase ends it. Pure
    given `parents`."""
    n, h = 0, head
    for _ in range(limit):
        ps = parents(h)
        if len(ps) != 2:
            break
        h = ps[0]
        n += h in retried
        if n >= cap:
            break
    return n


def decide(head: str, retried: set[str], spent: set[str], laddered: bool, reason: str, done: int,
           cap: int) -> tuple[str, str]:
    """(action, why) for one candidate: retry | hold | spent | quiet. Pure: the policy the tests pin."""
    if head in spent:
        return "quiet", "base-driven retries spent at this head, already named"
    if head in retried:
        return "hold", "already retried once from this head (base-driven)"
    if laddered:
        return "hold", "the heal ladder took this head (one actor per head)"
    if reason:
        return "hold", f"no evidence main fixed it: {reason}"
    if done >= cap:
        return "spent", f"{done} base-driven retries since its last own commit and still red: needs the author or a session"
    return "retry", f"main went red -> green on its failing check(s): base-driven retry {done + 1} of {cap}"


def mark_comment(head: str, base: str, n: int, cap: int, checks: list[str]) -> str:
    return "\n".join([
        MARK.format(head=head, base=base),
        f"**smart auto-merge**: red at `{head[:12]}` on {', '.join(f'`{c}`' for c in checks)}; main was red on it at this "
        f"PR's base and is green since (`{base[:12]}`). Updating the branch from main: base-driven retry {n} of {cap}."])


def spent_comment(head: str, cap: int, checks: list[str]) -> str:
    return "\n".join([
        SPENT.format(head=head),
        f"**smart auto-merge**: still red at `{head[:12]}` on {', '.join(f'`{c}`' for c in checks)} after {cap} "
        "base-driven retries since its last own commit. Not retried again: the PR itself needs the author or a session."])


# ── GitHub ────────────────────────────────────────────────────────────────────────────────────
def _lines(read: Gh, *args: str) -> list[dict]:
    return [json.loads(x) for x in (read(*args).stdout or "").splitlines() if x.strip()]


def runs_at(repo: str, sha: str, read: Gh) -> list[Run]:
    """Every check run and commit status at `sha`, identified by (app, workflow file, name)."""
    paths = {int(w["suite"]): str(w.get("path") or "") for w in _lines(
        read, "api", "--paginate", f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100",
        "--jq", ".workflow_runs[] | {suite: .check_suite_id, path}") if w.get("suite")}
    out = []
    for c in _lines(read, "api", "--paginate", f"repos/{repo}/commits/{sha}/check-runs?per_page=100",
                    "--jq", ".check_runs[] | {name, status, conclusion, id, app: .app.slug, suite: .check_suite.id}"):
        done = (c.get("status") or "").lower() == "completed"
        out.append(Run((str(c.get("app") or ""), paths.get(int(c.get("suite") or 0), ""), str(c.get("name") or "")),
                       str(c.get("conclusion") or "").upper() if done else "", int(c.get("id") or 0)))
    for s in _lines(read, "api", "--paginate", f"repos/{repo}/commits/{sha}/statuses?per_page=100",
                    "--jq", ".[] | {context, state, id}"):
        state = str(s.get("state") or "").upper()
        out.append(Run(("status", "", str(s.get("context") or "")), "" if state == "PENDING" else state,
                       int(s.get("id") or 0)))
    return out


def failing_keys(runs: list[Run], names: list[str]) -> list[Key]:
    """The PR head's red checks, by full identity: the newest run per key, red, named in `names`. Pure."""
    newest: dict[Key, Run] = {}
    for r in runs:
        if r.key[2] in names and (r.key not in newest or r.id > newest[r.key].id):
            newest[r.key] = r
    return sorted(k for k, r in newest.items() if r.conclusion in _RED | {"CANCELLED"})


def evidence(repo: str, p: dict, base_ref: str, names: list[str], read: Gh) -> Evidence:
    """The candidate's failing keys and main's runs near its merge base and since. Raises when
    GitHub will not answer. Cheapest refusal first: no red near the base = no reads since."""
    head = str(p["headRefOid"])
    ev = Evidence(failing_keys(runs_at(repo, head, read), names), [], [])
    got = [x.strip() for x in (read("api", "--paginate", f"repos/{repo}/compare/{head}...{base_ref}?per_page=100",
                                    "--jq", ".merge_base_commit.sha, .commits[].sha").stdout or "").splitlines() if x.strip()]
    if not got or not ev.failing:
        return ev
    mb = got[0]
    lacking = list(reversed([x for x in got[1:] if x != mb]))  # newest first
    near = [x.strip() for x in (read("api", f"repos/{repo}/commits?sha={mb}&per_page={NEAR + 1}", "--jq",
                                     ".[].sha").stdout or "").splitlines() if x.strip()] or [mb]
    ev.near = [runs_at(repo, sha, read) for sha in near[:NEAR + 1]]
    ev.base = lacking[0] if lacking else ""
    if not all(red_at_base(k, ev.near) for k in ev.failing):
        return ev  # main was never red at its base on some check: no read of the commits since
    for sha in lacking[:LOOKBACK]:
        ev.lacking.append(runs_at(repo, sha, read))
        if all(not transition(k, ev) for k in ev.failing):
            break
    return ev


def parents_of(repo: str, read: Gh) -> Callable[[str], list[str]]:
    def get(sha: str) -> list[str]:
        return list(json.loads(read("api", f"repos/{repo}/commits/{sha}", "--jq", "[.parents[].sha]").stdout or "[]"))
    return get


def whoami(write: Gh) -> str:
    """The push token's login: the one identity whose markers are trusted ('' = unknown)."""
    r = write("api", "user", "--jq", ".login")
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def assess(repo: str, armed: list[dict], required: tuple[str, ...], conflict_label: str, machinery: frozenset[int],
           base_of: Callable[[dict], str], cap: int, read: Gh, comments: Callable[[str, int], list[tuple[str, str]]],
           write: Gh, summary: list[str]) -> tuple[dict[int, str], int]:
    """({PR: the marker comment to write before its update}, errors). A spent PR is surfaced here."""
    retry: dict[int, str] = {}
    errors = 0
    todo = [p for p in sorted(armed, key=lambda x: int(x["number"]))
            if not candidate(p, required, conflict_label, machinery)]
    if cap <= 0 or not todo:
        return retry, 0
    me = whoami(write)
    if not me:
        print("::error::base-driven retry: the push token's identity could not be read (GET /user); feature skipped this pass")
        summary.append("base-driven retry SKIPPED: the push token's identity is unknown, so no marker could be trusted")
        return retry, 1
    for p in todo:
        n, head = int(p["number"]), str(p["headRefOid"])
        names = redclass.red_checks(p.get("statusCheckRollup"), required)
        ev, done = Evidence([], [], []), 0
        try:
            notes = comments(repo, n)
            retried, spent = trusted(notes, me, _MARK), trusted(notes, me, _SPENT)
            laddered = any(h == head for h, *_ in pr_ladder.marks([b for _, b in notes], pr_ladder._HEAL))
            action, why = decide(head, retried, spent, laddered, "", 0, cap)
            if action in ("retry", "spent"):  # the record did not settle it: read main
                ev = evidence(repo, p, base_of(p), names, read)
                lost = sorted(set(names) - {k[2] for k in ev.failing})  # every red check must be identified
                reason = "; ".join([f"`{c}` not identified at its head" for c in lost]
                                   + [r for r in (transition(k, ev) for k in ev.failing) if r]) if ev.failing else \
                    "its failing checks have no check run at its head"
                done = count(head, parents_of(repo, read), retried, cap) if not reason and retried else 0
                action, why = decide(head, retried, spent, laddered, reason, done, cap)
        except (subprocess.CalledProcessError, ValueError, KeyError) as e:
            errors += 1
            print(f"::error::#{n}: base-driven retry could not read its evidence: {e} {(getattr(e, 'stderr', '') or '')[-300:]}")
            summary.append(f"#{n}: red and behind, base-driven retry NOT assessed (GitHub would not say): {e}")
            continue
        line = f"#{n}: red and behind -> base-driven {action}: {why}"
        if action == "retry":
            retry[n] = mark_comment(head, ev.base or head, done + 1, cap, names)
        elif action == "spent":
            errors += 1  # loud once per head: the run goes red, the comment is the outside record
            print(f"::error::#{n}: {why}")
            r = write("pr", "comment", str(n), "-R", repo, "--body-file", "-", input_text=spent_comment(head, cap, names))
            line += "" if r.returncode == 0 else "; its comment FAILED"
        summary.append(line)
    return retry, errors


def record(repo: str, n: int, body: str, write: Gh) -> bool:
    """Write the retry marker BEFORE the update: no record, no update (the record is the budget)."""
    r = write("pr", "comment", str(n), "-R", repo, "--body-file", "-", input_text=body)
    if r.returncode:
        print(f"::error::#{n}: base-driven retry marker FAILED, not updated: {(r.stderr or r.stdout or '').strip()[:300]}")
    return r.returncode == 0
