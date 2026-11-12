"""The merge train's decisions, pure: candidates, the open train's phase, the bisect split.

A CANDIDATE is an open PR that would land on its own the moment it were up to date:
  * same repository (a fork's head never reaches the train's token), based on the default branch;
  * auto-merge ARMED (the operator's "land this" -- the train never lands what was not asked);
  * not a draft, no `autoland-off` label or title tag, not ejected from a train, not a train;
  * `mergeable` is not CONFLICTING;
  * every required check present on its head and green. The up-to-date requirement is the one
    thing NOT asked: being behind main is exactly what the train fixes.
The caller's review gate is a required check, so "green" includes its verdict at that head; the
compose step asks the gate's own runs again (github.review_passed) before a PR rides.

THE PHASE of an open train, from its head's required checks and main:
  merged  -> finalize (close each constituent as landed);
  stale   -> main moved past the train's root: strict protection would refuse it, recompose;
  red     -> a required check failed: bisect (below), never a guess at which PR broke it;
  green   -> every required check passed: merge it (rebase, so main gets one commit per PR);
  stuck   -> still pending after `stale_minutes`: close and recompose, loudly;
  pending -> wait.

THE BISECT SPLIT. A red train of n >= 4 keeps its OLDER half for the next train and ejects the
newer floor(n/2); a red train of 2 or 3 ejects all. An ejected PR is labelled `train-ejected` and
lands through its own auto-merge (pr_autoupdate tests it alone against main), so the culprit
meets its own red check and every innocent one lands without a train. At most ~log2(n) trains
for one bad PR, and no train is ever retried with the same members.

NOT EVERY RED IS EVIDENCE (ci-kit#70 finding 6). A red that only says the MACHINERY failed -- a
check that never started (STARTUP_FAILURE, a status ERROR), or the train's review gate reporting
BLIND, or blocking a train the composer's own pre-check passed -- says nothing about any PR. Its
riders are RELEASED (`why=retry`), never ejected, and recomposed; a rider retried RETRY_LIMIT times
at the same head is HELD out of trains (not labelled: a new head re-admits it) and the run is red.

NO INFINITE RECOMPOSE (finding 3). A rider released from a STUCK train (checks never finished)
RECOMPOSE_LIMIT times at the same head is HELD out of trains until its head changes, and the run
is red: something keeps a train it rides from finishing. A STALE release is NOT counted (round 2):
stale only says main moved -- a PR landing on its own (an ejected one, a Renovate bump) -- which
is no fault of any rider; counting it ejected whole trains on a busy main. A rider recomposed
forever by stale trains is the OUTSIDE watcher's: mcp train_watch times a rider across trains
(`rode` markers). Nothing here is sticky: hold re-admits on a new head. The counts live in the
rider's own comments (`released` markers), so they survive across runs and trains.
"""
from __future__ import annotations

import re

from ci_kit.merge_train import EJECTED, OPT_OUT, RIDING, TRAIN_PREFIX
from ci_kit.redclass import FAIL, PASS, rollup_infra

# PASS / FAIL / INFRA and the machinery-vs-verdict test live in ci_kit/redclass.py, shared with
# pr_queue and pr_heal. CANCELLED / STALE are NOT failures here: a cancelled check is no evidence
# against any PR, so it never ejects one. It stays pending, and a train pending past
# stale_minutes is recomposed.
RECOMPOSE_LIMIT = 2
RETRY_LIMIT = 2
RODE = "<!-- merge-train:rode train={train} head={head} -->"
RELEASED = "<!-- merge-train:released train={train} head={head} why={why} -->"
_RELEASED = re.compile(r"<!-- merge-train:released train=(\d+) head=([0-9a-f]{40}) why=([a-z-]+) -->")


def labels(pr: dict) -> set[str]:
    return {str(lb.get("name", "")) for lb in pr.get("labels") or []}


def is_train(pr: dict) -> bool:
    return str(pr.get("headRefName") or "").startswith(TRAIN_PREFIX) and not pr.get("isCrossRepository")


def checks(rollup: list[dict] | None, required: tuple[str, ...]) -> tuple[str, list[str]]:
    """('green' | 'red' | 'pending', names) for a head's checks. STRICT, unlike pr_queue: a
    required check that is absent is PENDING, never green -- a check that never reports must not
    let a train merge (GitHub would refuse it anyway) or a PR ride. `required` empty = every
    check present, and a head with no checks at all is pending. names = the failing (red) or the
    missing/running (pending) ones."""
    seen: dict[str, str] = {}
    for c in rollup or []:
        name = c.get("name") or c.get("context") or ""
        if required and name not in required:
            continue
        if c.get("__typename") == "StatusContext" or ("state" in c and "status" not in c):
            state = (c.get("state") or "").upper()
            verdict = "pass" if state == "SUCCESS" else "fail" if state in ("FAILURE", "ERROR") else "wait"
        elif (c.get("status") or "").upper() != "COMPLETED":
            verdict = "wait"
        else:
            concl = (c.get("conclusion") or "").upper()
            verdict = "pass" if concl in PASS else "fail" if concl in FAIL else "wait"
        # a name reported twice (a re-run): any failure is red, any pending keeps it pending
        prev = seen.get(name)
        seen[name] = verdict if prev is None else min(prev, verdict, key=("fail", "wait", "pass").index)
    red = sorted(n for n, v in seen.items() if v == "fail")
    if red:
        return "red", red
    wanted = required or tuple(seen)
    waiting = sorted(n for n in wanted if seen.get(n) != "pass")
    if waiting or not wanted:
        return "pending", waiting or ["(no checks reported)"]
    return "green", []


def red_is_infra(rollup: list[dict] | None, required: tuple[str, ...]) -> bool:
    """Every failing required check failed as MACHINERY (never started), none as a verdict.
    The rollup-depth test (redclass.rollup_infra); phases.judge asks the jobs depth too."""
    return rollup_infra(rollup, required)


def released(bodies: list[str], head: str, whys: set[str]) -> int:
    """How many times this rider was released from a train AT `head` for one of `whys`."""
    return sum(1 for b in bodies for m in _RELEASED.finditer(b) if m.group(2) == head and m.group(3) in whys)


def rider_gate(bodies: list[str], head: str) -> tuple[str, str]:
    """('ride' | 'hold', why) for a candidate at `head`, from its own comments."""
    n = released(bodies, head, {"stuck"})
    if n >= RECOMPOSE_LIMIT:
        return "hold", (f"released from {n} STUCK trains at this head (checks never finished): held out of "
                        "trains until its head changes; its own lane's checks say what never reports")
    r = released(bodies, head, {"retry"})
    if r >= RETRY_LIMIT:
        return "hold", (f"{r} trains at this head failed on machinery (a BLIND or blocked review gate, a check "
                        "that never started): held out of trains until its head changes")
    return "ride", ""


def why_not(pr: dict, required: tuple[str, ...], base: str = "main") -> str:
    """'' when the PR may ride a train, else why not (the summary prints it)."""
    if pr.get("isCrossRepository"):
        return "a fork"
    if is_train(pr):
        return "a train"
    if (pr.get("baseRefName") or base) != base:
        return f"based on {pr.get('baseRefName')}, not {base}"
    if not pr.get("autoMergeRequest"):
        return "auto-merge not armed"
    if pr.get("isDraft"):
        return "a draft"
    have = labels(pr)
    if OPT_OUT in have or OPT_OUT in str(pr.get("title") or "").lower():
        return OPT_OUT
    if EJECTED in have:
        return "ejected from a train (lands on its own auto-merge)"
    if (pr.get("mergeable") or "").upper() == "CONFLICTING":
        return "conflicts with main"
    state, names = checks(pr.get("statusCheckRollup"), required)
    if state != "green":
        return f"checks {state} ({', '.join(names[:4])})"
    return ""


def armed_at(pr: dict) -> tuple[str, int]:
    return ((pr.get("autoMergeRequest") or {}).get("enabledAt") or "", int(pr.get("number", 0)))


def candidates(prs: list[dict], required: tuple[str, ...], base: str = "main",
               exclude: frozenset[int] = frozenset()) -> tuple[list[dict], list[tuple[dict, str]]]:
    """(candidates oldest-armed first, [(pr, why not)]). `exclude` = numbers ejected this run."""
    ok, no = [], []
    for p in sorted(prs, key=armed_at):
        why = "ejected from a train this run" if p.get("number") in exclude else why_not(p, required, base)
        (no.append((p, why)) if why else ok.append(p))
    return ok, no


def phase(train: dict, root: str, main_sha: str, age_min: float, stale_minutes: float,
          required: tuple[str, ...]) -> tuple[str, list[str]]:
    """(phase, names) of an OPEN train PR -- see the module docstring. `root` is the main commit
    the train was composed on."""
    if root != main_sha:
        return "stale", [f"main moved {root[:12]} -> {main_sha[:12]}"]
    state, names = checks(train.get("statusCheckRollup"), required)
    if state == "red":
        return "red", names
    if state == "green":
        return "green", []
    if age_min > stale_minutes:
        return "stuck", names
    return "pending", names


def split(riders: list[int]) -> tuple[list[int], list[int]]:
    """(keep for the next train, eject) for a red train's constituents, oldest first."""
    n = len(riders)
    if n < 4:
        return [], list(riders)
    return riders[: n - n // 2], riders[n - n // 2:]


def stray_riders(prs: list[dict], riding: set[int]) -> list[int]:
    """PRs labelled in-train that ride no open train: a crashed run's leftovers. The label makes
    pr_autoupdate skip the PR, so a stray one would park it forever."""
    return sorted(p["number"] for p in prs if RIDING in labels(p) and p["number"] not in riding)
