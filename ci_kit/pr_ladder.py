"""THE HEAL LADDER: an armed PR that goes red or conflicts heals itself, cheapest tier first.

WHY (operator, 2026-11-14: "we can likely handle a lot of cases with no claude at all. only when we
cant do you upgrade to that"). No session or person should have to watch a PR. Before this, a
judgment red got ONE comment and a conflict got ONE page, and both then waited for a person.

THE TIERS, each tried only when the one before cannot act:
  0  a clean merge of main                    pr_autoupdate (GitHub's update-branch)
  -  a machinery red                          pr_heal: rerun the failed jobs (budgeted)
  1  a lockfile-only conflict                 relocked (ci_kit/pr_regen.LOCKFILES: uv, npm, pnpm, NuGet)
  2  a conflict or red in GENERATED files     regenerated from the caller's map (ci_kit/pr_regen.py);
                                              a conflict git's own merge resolves (`merge=union`)
                                              is pushed as that merge
  3  a judgment red or a real conflict        Claude Code in Actions (ci_kit/pr_fix.py), on the
                                              sealed pool, at most once per head
  4  the page                                 ONE ntfy page per head: the PR, the failing check, what
                                              tier 3 tried (or why it could not run)
Tiers 1-4 run in the caller's `pr-heal.yml` call, one job per PR, from this pass's `heal` output.
Every push is a merge or fix commit to the PR branch, never forced, made by ci_kit/pr_apply.py on a
fresh runner. The repo's CI and its review gate judge the result like any other push.

RENOVATE (2026-11-14). Renovate merges its own PRs (`platformAutomerge: false` estate-wide), so they
are never armed; one whose body says `**Automerge**: Enabled` counts as armed here (`armed`). A
CONFLICTED Renovate PR is not ours to merge into: its branch is Renovate's, and Renovate rebuilds
it whole (versions, lockfiles, a terraform lock's hashes) when its rebase checkbox is ticked. So
the ladder's step for it is to TICK that box (mode `rebase`, the same once-per-head marker), and
page when the head has not moved after `stale-minutes` (a consumer's #246 sat conflicted 11-07..11-14).
A RED Renovate PR takes the ladder like any other; tier 2's built-in pin rehash
(ci_kit/pin_rehash.py) moves a release binary's sha256 table to the version Renovate set.

WHO IS ELIGIBLE (`blocked`, pure): an ARMED (or Renovate-automerge), same-repo, non-draft PR, never `autoland-off` (label
or title tag) and never the database Renovate group (`dba`: never auto-merged). Tier 3 is further
refused a Renovate MAJOR (`claude_blocked`). Such a PR keeps the old behaviour: the judgment
comment, the conflict page.

THE BUDGET, in the PR's own comments (pr_heal's markers, the same idea): a dispatch writes
`<!-- smart-merge:heal head=<sha> mode=<m> claude=<0|1> at=<utc> -->` BEFORE it is handed to the
heal job, so a head is dispatched at most ONCE, whatever happens next. Tier 3 also has a per-PR
lifetime cap (`claude-per-pr`) and a per-pass cap (`claude-per-pass`: a PR over it waits for the
next pass, lowest number first), and every PR a total cap (`per-pr`) so a push -> red -> push loop
ends in a page. The heal job writes `<!-- smart-merge:healed head=<sha> tier=<t> result=<r> -->`,
and a page `<!-- smart-merge:paged head=<sha> why=<w> -->` (one per head).

A TIER THAT CANNOT START IS NOT A TIER THAT FAILED (v1.45.0). Tier 3 on a runner that cannot run
the CLI's sandbox (ci_kit/pr_fix.py `sandbox_check`) is the same answer for every PR, so it has its
own result, `cannot-start` -- in the `healed` marker, in the `paged` marker's `why`, and as the
page's TITLE ("tier 3 CANNOT START", naming the runner, never "could not fix it"). 17 tier-3
attempts on 15 PRs (2026-11-15..26) were each recorded `failed` and paged under the title every
honest decline carries; nothing said they were one fault. One search lists the PRs that hit it:
`repo:<owner>/<repo> in:comments "result=cannot-start"`.

WATCHED: a dispatch with no `healed` record at its head after `stale-minutes` (the heal job never
ran, was cancelled, or died before its report) is paged by the next pass that sees the PR still red
or conflicting -- the pass is the scheduled watcher (a consumer runs it every 15 min), and a dark
pass is an outside schedule audit's. An outside PR reconciler reads the markers into its report.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

from ci_kit.merge_train import OPT_OUT

HEAL = "<!-- smart-merge:heal head={head} mode={mode} claude={claude} at={at} -->"
HEALED = "<!-- smart-merge:healed head={head} tier={tier} result={result} -->"
PAGED = "<!-- smart-merge:paged head={head} why={why} -->"
_HEAL = re.compile(r"<!-- smart-merge:heal head=([0-9a-f]{40}) mode=([a-z]+) claude=([01]) at=([0-9TZ:+-]+) -->")
ATTEMPT = "<!-- smart-merge:attempt head={head} tier=claude run={run} -->"
_ATTEMPT = re.compile(r"<!-- smart-merge:attempt head=([0-9a-f]{40}) tier=claude run=([0-9]+) -->")
_HEALED = re.compile(r"<!-- smart-merge:healed head=([0-9a-f]{40}) tier=([a-z0-9-]+) result=([a-z-]+) -->")
_PAGED = re.compile(r"<!-- smart-merge:paged head=([0-9a-f]{40}) why=([a-z-]+) -->")
RENOVATE = ("renovate", "renovate[bot]", "app/renovate")
MAJOR_TITLE = re.compile(r"\bto v?\d+(\.x)?\s*(\(|$)", re.I)
DB_LABEL = "dba"
CANNOT_START = "cannot-start"   # ci_kit/pr_fix.py's KIND, and this record's result for it (not imported: see in_flight)

Comments = Callable[[str, int], list[str]]
Write = Callable[..., subprocess.CompletedProcess]


# THE STALE DEADLINE IS DERIVED, NOT GUESSED (v1.35.0). A dispatch is "stale" (paged) only after
# the longest a LIVE heal can take: every pr-heal.yml job's timeout-minutes, in order (they run
# one after another; tests/test_heal_ladder_must_fire.py pins this table to the workflow and the
# fix job to pr_fix.FIX_JOB_MIN), plus the wait for a sealed runner before the fix job starts --
# measured up to 38 min while full reviews held the pool. 90 was below the 95 of the timeouts
# alone: a heal still inside its limits was paged as lost.
HEAL_JOB_MIN = {"plan": 5, "regen": 20, "claim": 5, "fix": 50, "push": 10, "report": 5}
SEALED_QUEUE_MIN = 40
STALE_MIN = sum(HEAL_JOB_MIN.values()) + SEALED_QUEUE_MIN


@dataclass(frozen=True)
class Config:
    on: bool = False
    claude_per_pass: int = 1     # tier-3 dispatches per pass (0 = tier 3 off)
    claude_per_pr: int = 2       # tier-3 dispatches per PR, all heads
    per_pr: int = 6              # heal dispatches per PR, all heads
    stale_min: float = STALE_MIN  # a dispatch with no record after this long is paged (derived above)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        e = os.environ if env is None else env

        def num(k: str, d: float) -> float:
            v = (e.get(k) or "").strip()
            return float(v) if v else d
        return cls(on=(e.get("CI_KIT_HEAL") or "").strip().lower() in ("1", "true"),
                   claude_per_pass=int(num("CI_KIT_HEAL_CLAUDE_PER_PASS", 1)),
                   claude_per_pr=int(num("CI_KIT_HEAL_CLAUDE_PER_PR", 2)),
                   per_pr=int(num("CI_KIT_HEAL_PER_PR", 6)), stale_min=num("CI_KIT_HEAL_STALE_MIN", STALE_MIN))


# ── pure ──────────────────────────────────────────────────────────────────────────────────────
def _labels(pr: dict) -> set[str]:
    return {str(lb.get("name") if isinstance(lb, dict) else lb) for lb in pr.get("labels") or []}


def _author(pr: dict) -> str:
    a = pr.get("author") or pr.get("user") or {}
    return str(a.get("login") if isinstance(a, dict) else a or "").lower()


def is_renovate(pr: dict) -> bool:
    return _author(pr) in RENOVATE or str(pr.get("headRefName") or "").startswith("renovate/")


def armed(pr: dict) -> bool:
    """Armed for auto-merge, or a Renovate PR Renovate will merge itself (pr_list's flag). Pure."""
    return bool(pr.get("autoMergeRequest") or pr.get("auto_merge")
                or (is_renovate(pr) and pr.get("renovateAutomerge")))


def blocked(pr: dict) -> str:
    """Why the ladder may not touch this PR at all ('' = it may). Pure."""
    title = str(pr.get("title") or "")
    if OPT_OUT in _labels(pr) or OPT_OUT in title.lower():
        return f"`{OPT_OUT}`: the author opted out of automatic landing"
    if not armed(pr):
        return "not armed"
    if pr.get("isDraft") or pr.get("draft"):
        return "a draft"
    if pr.get("isCrossRepository"):
        return "a fork"
    if is_renovate(pr) and (DB_LABEL in _labels(pr) or "database" in title.lower()):
        return "the database Renovate group (never auto-merged)"
    return ""


def claude_blocked(pr: dict) -> str:
    """Why tier 3 may not run on this PR ('' = it may). Pure."""
    why = blocked(pr)
    if why:
        return why
    if is_renovate(pr) and ("major" in _labels(pr) or MAJOR_TITLE.search(str(pr.get("title") or ""))):
        return "a Renovate MAJOR (waits for review, never a model's fix)"
    return ""


def marks(bodies: list[str], pattern: re.Pattern[str]) -> list[tuple[str, ...]]:
    return [m.groups() for b in bodies for m in pattern.finditer(b or "")]


def _when(at: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return None


def decide(pr: dict, bodies: list[str], cfg: Config, claude_left: int, now: dt.datetime,
           mode: str = "red") -> tuple[str, bool, str]:
    """(action, claude, why) for one red or conflicting armed PR. Pure: the policy the tests pin.
    action: skip (not the ladder's; the old comment/page) | dispatch | rebase (a conflicted Renovate
    PR: tick its rebase box) | quiet (dispatched at this
    head) | stale (dispatched, no record past stale-minutes, not yet paged) | defer (wait for a tier-3
    slot next pass) | capped (the PR's total budget is spent; paged once per head)."""
    why = blocked(pr)
    if why:
        return "skip", False, why
    head = str(pr.get("headRefOid") or pr.get("head") or "")
    heals = marks(bodies, _HEAL)
    paged = any(h == head for h, _ in marks(bodies, _PAGED))
    mine = [x for x in heals if x[0] == head]
    if mine:
        done = any(h == head for h, _, _ in marks(bodies, _HEALED))
        at = _when(mine[-1][3])
        if not done and not paged and at and (now - at).total_seconds() > cfg.stale_min * 60:
            return "stale", False, f"dispatched {mine[-1][3]} and no heal record since"
        return "quiet", False, "already dispatched at this head" + ("" if done else in_flight(bodies, head, at, now))
    if len({h for h, *_ in heals}) >= cfg.per_pr:
        return ("quiet" if paged else "capped"), False, f"{cfg.per_pr} heal(s) already spent on this PR"
    if mode == "conflict" and is_renovate(pr):
        return "rebase", False, "a Renovate branch: Renovate rebuilds it (rebase checkbox), never a merge pushed onto it"
    used = sum(1 for _, _, c, _ in heals if c == "1")
    tier3 = cfg.claude_per_pass > 0 and used < cfg.claude_per_pr and not claude_blocked(pr)
    if tier3 and claude_left <= 0:
        return "defer", False, f"tier 3 allowed, but this pass's {cfg.claude_per_pass} tier-3 slot(s) are taken"
    note = "tiers 1-3" if tier3 else ("tiers 1-2: " + (claude_blocked(pr) or ("tier 3 off" if cfg.claude_per_pass <= 0
                                                                               else f"tier 3 spent {used}/{cfg.claude_per_pr}")))
    return "dispatch", tier3, note


def in_flight(bodies: list[str], head: str, at: dt.datetime | None, now: dt.datetime) -> str:
    """The summary suffix for a heal still running at this head. Tier 3 holds the ONE sealed runner
    (the review gate's full reviews queue behind it) for up to pr_fix.FIX_JOB_MIN minutes, and
    nothing else says so: the claim's attempt marker names the run. Pure."""
    from ci_kit.pr_fix import FIX_JOB_MIN  # lazy: pr_fix pulls in the review harness
    age = f", {int((now - at).total_seconds() // 60)} min since dispatch" if at else ""
    runs = [r for h, r in marks(bodies, _ATTEMPT) if h == head]
    if runs:
        return (f"; TIER 3 IN FLIGHT on the sealed runner (run {runs[-1]}{age}; holds it up to "
                f"{FIX_JOB_MIN} min, full reviews queue behind it)")
    return f"; in flight, no heal record yet{age}"


def page_text(repo: str, pr: dict, checks: str, tried: str, result: str = "") -> tuple[str, str]:
    """(title, body) of the tier-4 page. Pure. A `cannot-start` is titled as the RUNNER's fault: the
    PR number is where it was seen, not what is wrong."""
    n = pr.get("number")
    name = repo.rsplit('/', 1)[-1]
    if result == CANNOT_START:
        return (f"{name}: heal tier 3 CANNOT START (the runner, every PR)",
                "\n".join((f"Seen on #{n}. {tried}",
                           "Every tier-3 heal in this repo fails this way until the sealed runner is fixed; "
                           "each one still spends its PR's attempt at that head.")))
    title = f"{name} #{n}: heal ladder could not fix it"
    body = "\n".join(x for x in (str(pr.get("title") or ""), f"Failing: {checks}" if checks else "",
                                 f"Tried: {tried}", "Needs a session; auto-merge stays armed.") if x)
    return title, body


# ── the pass (pr_autoupdate.run calls this after its update loop) ──────────────────────────────
@dataclass
class Candidate:
    pr: dict
    mode: str            # conflict | red
    checks: str = ""
    runs: str = ""


def dispatch(repo: str, cands: list[Candidate], cfg: Config, comments: Comments, write: Write,
             summary: list[str], page: Callable[[str, str, str], str], now: dt.datetime | None = None
             ) -> tuple[list[dict], int, set[int]]:
    """Decide each candidate, record each dispatch BEFORE it is handed on, page the stale and the
    capped. Returns (the `heal` output rows, errors, the PR numbers the ladder took -- for those the
    old judgment comment / conflict page is not written)."""
    now = now or dt.datetime.now(dt.UTC)
    out: list[dict] = []
    errors, taken, left = 0, set(), cfg.claude_per_pass
    seen: set[int] = set()
    for c in sorted(cands, key=lambda c: int(c.pr["number"])):
        n, head = int(c.pr["number"]), str(c.pr.get("headRefOid") or "")
        if n in seen:
            continue
        seen.add(n)
        try:
            bodies = comments(repo, n)
        except (subprocess.CalledProcessError, ValueError) as e:
            errors += 1
            summary.append(f"#{n}: heal ladder could not read its comments: {e}")
            continue
        action, claude, why = decide(c.pr, bodies, cfg, left, now, c.mode)
        if action == "skip":
            summary.append(f"#{n}: {c.mode} at {head[:12]} -> not the heal ladder's ({why})")
            continue
        taken.add(n)
        line = f"#{n}: {c.mode} at {head[:12]} -> heal {action}: {why}"
        if action == "dispatch":
            at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            body = "\n".join([HEAL.format(head=head, mode=c.mode, claude=int(claude), at=at),
                              f"**heal ladder**: {'conflicts with main' if c.mode == 'conflict' else 'judgment red'} at "
                              f"`{head[:12]}`" + (f" ({c.checks})" if c.checks else "") + f" -- handed to the heal job, {why}."
                              " Every push it makes is judged by CI and the review gate like any other."])
            r = write("pr", "comment", str(n), "-R", repo, "--body-file", "-", input_text=body)
            if r.returncode:  # unrecorded = not dispatched: the marker IS the once-per-head budget
                errors += 1
                line += f"; NOT dispatched, its record failed: {(r.stderr or '').strip()[:200]}"
            else:
                left -= int(claude)
                out.append({"pr": n, "head": head, "mode": c.mode, "claude": claude, "checks": c.checks[:300],
                            "runs": c.runs[:300]})
        elif action == "rebase":
            line += _rebase(repo, c.pr, write, now)
            errors += "NOT" in line
        elif action in ("stale", "capped"):
            title, text = page_text(repo, c.pr, c.checks, why)
            said = page(title, text, str(c.pr.get("url") or ""))
            r = write("pr", "comment", str(n), "-R", repo, "--body-file", "-",
                      input_text=f"{PAGED.format(head=head, why=action)}\n**heal ladder**: paged -- {why} ({said}).")
            errors += 1 if r.returncode else 0
            line += f"; page {said}"
            if action == "stale" and not any(h == head and m == "rebase" for h, m, *_ in marks(bodies, _HEAL)):
                errors += 1  # loud: a heal that never reported is a broken ladder, not a PR's red
                # (a Renovate rebase that did not happen is Renovate's: paged, not the pass's red)
        summary.append(line)
    return out, errors, taken


def _rebase(repo: str, pr: dict, write: Write, now: dt.datetime) -> str:
    """Tick a conflicted Renovate PR's rebase box, recorded first. The summary suffix ('NOT' = an error)."""
    n, head = int(pr["number"]), str(pr.get("headRefOid") or "")
    r = write("pr", "view", str(n), "-R", repo, "--json", "body", "-q", ".body")
    if r.returncode:
        return f"; NOT asked, its body could not be read: {(r.stderr or '').strip()[:200]}"
    from ci_kit.pr_renovate import ticked  # here: pr_renovate -> pr_sched -> pr_heal -> this module
    body = ticked(r.stdout or "")
    # ticked already (a person, or a pass whose PATCH failed) or no box: nothing to ask, but the head
    # is still recorded, so the stale-minutes page fires if Renovate never moves it (a consumer's #246)
    did = "ticked Renovate's rebase box" if body is not None else "its rebase box is already ticked or absent"
    mark = HEAL.format(head=head, mode="rebase", claude=0, at=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    r = write("pr", "comment", str(n), "-R", repo, "--body-file", "-",
              input_text=f"{mark}\n**heal ladder**: conflicts with main at `{head[:12]}` -- {did}: Renovate rebuilds "
                         "its own branch. Paged if the head has not moved after stale-minutes.")
    if r.returncode:
        return f"; NOT asked, its record failed: {(r.stderr or '').strip()[:200]}"
    if body is None:
        return "; its rebase box is already ticked or absent -- recorded, waiting on Renovate"
    r = write("api", "-X", "PATCH", f"repos/{repo}/pulls/{n}", "--input", "-", input_text=json.dumps({"body": body}))
    return "; rebase box ticked" if not r.returncode else f"; NOT ticked: {(r.stderr or '').strip()[:200]}"


# ── the heal job's first and last steps ────────────────────────────────────────────────────────
def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True, encoding="utf-8",
                          errors="replace").stdout


def plan(repo: str, n: int, head: str, claude: bool, runs: list[str], checks: str, ctx_out: str) -> dict[str, str]:
    """Re-check everything at run time (the pass's word is a request, not a licence), fix the base
    sha every later job merges, and gather what tier 3 reads: the failed jobs' log tails."""
    from ci_kit.pr_renovate import automerge_enabled  # here, like `ticked`
    p = json.loads(_gh("api", f"repos/{repo}/pulls/{n}"))
    pr = {"number": n, "title": p.get("title"), "labels": p.get("labels"), "user": p.get("user"),
          "auto_merge": p.get("auto_merge"), "draft": p.get("draft"), "headRefName": p["head"]["ref"],
          "isCrossRepository": (p["head"].get("repo") or {}).get("full_name") != repo,
          "renovateAutomerge": automerge_enabled(p.get("body") or "")}
    why = "closed" if p.get("state") != "open" else blocked(pr)
    if not why and p["head"]["sha"] != head:
        why = f"head moved to {p['head']['sha'][:12]} (a new head is a new budget)"
    if not why:
        from ci_kit.pr_heal import comments_of  # here: pr_heal imports this module
        if any(h == head for h, *_ in marks(comments_of(repo, n), _HEALED)):
            why = "already healed at this head (a re-run of a finished heal is not a new budget)"
    base_ref = p["base"]["ref"]
    base = json.loads(_gh("api", f"repos/{repo}/git/ref/heads/{base_ref}"))["object"]["sha"]
    parts = [f"PR #{n}: {p.get('title')}", f"Red required checks: {checks or '(a conflict with main)'}", ""]
    for run in [r for r in runs if r.isdigit()][:4]:
        try:
            tail = "\n".join(_gh("run", "view", run, "-R", repo, "--log-failed").splitlines()[-150:])
        except subprocess.CalledProcessError as e:
            tail = f"(log unavailable: {(e.stderr or '').strip()[-200:]})"
        parts += [f"## run {run}, failed steps (last 150 lines)", tail[-12_000:], ""]
    if "review" in checks.lower():
        try:
            gate = [b for b in json.loads(_gh("api", "--paginate", f"repos/{repo}/issues/{n}/comments?per_page=100",
                                              "--jq", "[.[].body]") or "[]") if "<!-- review-gate" in (b or "")]
            if gate:
                parts += ["## the review gate's latest verdict", gate[-1][:10_000]]
        except (subprocess.CalledProcessError, ValueError):
            pass
    with open(ctx_out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))
    tier3 = claude and not claude_blocked(pr)
    return {"go": str(not why).lower(), "why": why or "eligible", "ref": p["head"]["ref"], "base": base,
            "base-ref": base_ref, "claude": str(tier3).lower(), "title": str(p.get("title") or "")[:200],
            "url": str(p.get("html_url") or "")}


def claim(repo: str, n: int, head: str, run: str, write: Write, comments: Comments) -> dict[str, str]:
    """Tier 3's ONE attempt at this head, recorded BEFORE Claude runs (the fix job holds no token, so
    this token-only job writes it). A second workflow run at the head -- or a full re-run of this one --
    finds the record and refuses; a 'Re-run failed jobs' is held by the attempt number (claimed_fix)."""
    prior = [r for h, r in marks(comments(repo, n), _ATTEMPT) if h == head]
    if prior:
        return {"claimed": "false", "why": f"tier 3 already ran at this head (run {prior[0]}): once per head"}
    r = write("pr", "comment", str(n), "-R", repo, "--body-file", "-",
              input_text=f"{ATTEMPT.format(head=head, run=run)}\n**heal ladder**: tier 3 (Claude) starting at "
                         f"`{head[:12]}` -- one attempt per head; the result follows in this run's record.")
    if r.returncode:
        return {"claimed": "false", "why": f"the attempt record failed, so tier 3 did not run: {(r.stderr or '').strip()[:200]}"}
    return {"claimed": "true", "why": "claimed"}


def claimed_fix(fix: str, fix_why: str, claimed: str, claim_why: str, attempt: str, run_attempt: str) -> tuple[str, str]:
    """The fix job's state for the record when the claim held it back. Pure. `claimed` '' = the claim
    job never ran (tier 3 was not wanted): the fix job's own word stands."""
    if fix or not claimed:
        return fix, fix_why
    if claimed != "true":
        return "refused", claim_why or "the claim did not record"
    if attempt != run_attempt:
        return "refused", (f"tier 3 was claimed in attempt {attempt} of this run; a re-run never runs Claude twice at "
                           "one head")
    return fix, fix_why


def outcome(go: bool, plan_why: str, regen: str, regen_why: str, fix: str, fix_why: str, push: str, push_why: str,
            claude: bool) -> tuple[str, str, str, bool]:
    """(tier, result, what happened, page?) for the heal job's record. Pure."""
    if not go:  # a plan that could not look is a failure of the ladder, never a quiet skip
        if plan_why and not plan_why.startswith("plan could not"):
            return "none", "skipped", plan_why, False
        return "none", "failed", plan_why or "the plan job did not run", True
    if fix == "fixed" and not push:
        return "claude", "failed", f"tier 3 fixed it ({fix_why[:200]}) and the push job did not run", True
    if fix == "fixed":
        tier = "claude"
    elif regen in ("regen", "merge"):
        tier = regen
    else:
        tier = "claude" if fix else "none"
    if push == "pushed":
        return tier, "pushed", push_why, False
    if push in ("moved", "nothing"):
        return tier, push, push_why, False
    if push in ("refused", "failed"):
        return tier, push, f"tier {tier}'s change was not pushed: {push_why}", True
    if fix == "failed" and fix_why.startswith(f"KIND={CANNOT_START}"):
        return "claude", CANNOT_START, (f"TIER 3 CANNOT START on this runner -- not this PR's failure: {fix_why} "
                                        f"(tier 2: {regen or 'not run'} ({regen_why or '-'}))"), True
    if fix in ("declined", "refused", "failed"):
        return "claude", fix, f"tier 2: {regen or 'not run'} ({regen_why or '-'}); tier 3 {fix}: {fix_why}", True
    if regen in ("regen", "merge"):
        return tier, "failed", f"tier 2 produced a fix ({regen}) and the push job did not run", True
    if not claude:
        return "none", "exhausted", f"tier 2: {regen or 'not run'} ({regen_why or '-'}); tier 3 not allowed here", True
    return "claude", "failed", f"tier 2: {regen or 'not run'}; tier 3 did not run or report", True


def report(repo: str, n: int, head: str, o: tuple[str, str, str, bool], checks: str, url: str, run_url: str,
           write: Write, comments: Comments, page: Callable[[str, str, str], str]) -> int:
    tier, result, what, should_page = o
    lines = [HEALED.format(head=head, tier=tier, result=result),
             f"**heal ladder** at `{head[:12]}`: **{result}** (tier {tier}) -- {what}", f"[heal run]({run_url})"]
    errors = 0
    if should_page:
        if any(h == head for h, _ in marks(comments(repo, n), _PAGED)):
            lines.append("Already paged at this head.")
        else:
            title, body = page_text(repo, {"number": n, "title": ""}, checks, what, result)
            said = page(title, body, url)
            lines.insert(1, PAGED.format(head=head, why=result))
            lines.append(f"Paged: {said}.")
    r = write("pr", "comment", str(n), "-R", repo, "--body-file", "-", input_text="\n".join(lines))
    if r.returncode:
        errors += 1
        print(f"::error::#{n}: the heal record comment failed: {(r.stderr or '').strip()[:300]}")
    print("\n".join(lines))
    return errors


def main(argv: list[str] | None = None) -> int:
    from ci_kit.ntfy import page
    from ci_kit.pr_heal import comments_of, write
    from ci_kit.pr_regen import output
    ap = argparse.ArgumentParser(prog="python -m ci_kit.pr_ladder", description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("plan")
    b = sub.add_parser("report")
    c = sub.add_parser("claim")
    c.add_argument("--run", required=True)
    for x in (a, b, c):
        x.add_argument("--repo", required=True)
        x.add_argument("--pr", type=int, required=True)
        x.add_argument("--head", required=True)
        x.add_argument("--checks", default="")
    a.add_argument("--claude", default="false")
    a.add_argument("--runs", default="")
    a.add_argument("--context-out", required=True)
    for k in ("go", "plan-why", "regen", "regen-why", "fix", "fix-why", "push", "push-why", "claude", "url", "run-url",
              "claimed", "claim-why", "claim-attempt", "run-attempt"):
        b.add_argument(f"--{k}", default="")
    args = ap.parse_args(argv)
    if args.cmd == "plan":
        try:
            output(**plan(args.repo, args.pr, args.head, args.claude == "true", re.split(r"[,\s]+", args.runs),
                          args.checks, args.context_out))
            return 0
        except (subprocess.CalledProcessError, ValueError, KeyError) as e:
            print(f"::error::heal plan could not read #{args.pr}: {e} {(getattr(e, 'stderr', '') or '')[-300:]}")
            output(go="false", why=f"plan could not read the PR: {e}"[:300])
            return 2
    if args.cmd == "claim":
        try:
            output(**claim(args.repo, args.pr, args.head, args.run, write, comments_of))
            return 0
        except (subprocess.CalledProcessError, ValueError) as e:
            output(claimed="false", why=f"the claim could not read the PR's records: {e}"[:300])
            return 2
    fix, fix_why = claimed_fix(args.fix, args.fix_why, args.claimed, args.claim_why, args.claim_attempt, args.run_attempt)
    o = outcome(args.go == "true", args.plan_why, args.regen, args.regen_why, fix, fix_why, args.push,
                args.push_why, args.claude == "true")
    output(result=o[1], tier=o[0])
    return 1 if report(args.repo, args.pr, args.head, o, args.checks, args.url, args.run_url, write, comments_of, page) \
        or o[1] in ("failed", CANNOT_START) else 0


if __name__ == "__main__":
    sys.exit(main())
