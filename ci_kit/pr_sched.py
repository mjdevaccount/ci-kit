"""The PR scheduler's GitHub side: read who holds the scarce pools, park, re-run, and say so.

The POLICY is pure and lives in ci_kit/pr_queue.py (`head`, `schedule`, `admit`; the WHY and the
measured thresholds are there). This module is its two actors:

  * `pass_(...)`, called by pr_autoupdate on every run: publishes nothing new, it ACTS --
      - cancels each non-head run `schedule` names (a PREEMPTION), labels its PR `parked`;
      - RE-RUNS parked work: the head's at once (a head BEHIND main gets the queue's update
        instead, which is its fresh run), a non-head's only while no scarce pool has a queued job
        and the head needs none -- one per pass, lowest number first, so a freed pool is refilled
        in landing order rather than GitHub's;
      - takes the label off a PR with nothing parked left (a new push replaced the parked run);
      - pages an OUT-OF-QUEUE MERGE: a push to main that merged a PR other than the head while the
        head was in flight against the old main (td#3072, Renovate's own automerge, 03:53Z
        11-14, invalidated head #3073);
      - names the head, its wait per pool, every parking and re-run in the summary, and returns an
        error for a STALLED head so the run goes red (mcp ci-schedule-audit reads that streak).
  * the review gate's admission (`python -m ci_kit.pr_admit`, its own module): parks a non-head
    PR's model review while the head needs the sealed pool (pr_queue.admit).

PARKED IS RED, AND LABELLED. A parked review fails `review deferred`; a preempted run is
cancelled. Both block the merge exactly as a missing check does -- nothing is waived -- and both
read as "CI failed" to an author's agent or an auto-fix bot, so the PR carries the `parked` label
(`queue:parked`; its description says it re-runs itself) from the moment it is parked until the
re-run starts. pr_heal leaves a parked PR alone: re-running it is this module's job, in order.

THE TOKENS. Reads: the job's GITHUB_TOKEN (actions: read) first, then the PAT (pr_autoupdate's
`read`). Cancel / re-run / label: the PAT, which needs Actions: Read and write (a reusable workflow
cannot ask GITHUB_TOKEN for it, see pr_heal). NOTHING IS PARKED UNPROVEN: every pass first PROBES
that grant (`can_act`: cancel a COMPLETED run -- 409 with the grant, 403 without, a no-op either
way). Without it the pass parks nothing and does not create the label -- and the review gate's
admission adds the label without creating it, so it parks nothing either: a park nobody can re-run
would strand the PR. The refusal is an ERROR naming the grant (a red run), never a silent skip.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import subprocess
from collections.abc import Callable

from ci_kit import pr_queue
from ci_kit.ntfy import page
from ci_kit.pr_heal import PARKED  # ONE name; pr_heal reads it too

PARKED_COLOR = "bfd4f2"
PARKED_DESCRIPTION = "PR queue: CI/review parked so the queue head gets the runner; re-runs itself. NOT a failure"
# the label's description while a pass cannot re-run: the review gate's admission reads it and parks
# nothing (a park nobody can re-run would strand the PR). ensure_label restores the description.
PARKED_OFF = "PR queue OFF: the push token cannot re-run runs (Actions: write). Admission parks nothing"
DEFERRED_JOB = "review deferred"   # the review gate's job that fails when it parks a review
RECORD_JOB = "record verdict"      # the review gate's job that writes the verdict a carry reads
PRE_JOB = "review"                 # the review gate's first job (the name mcp review_gate_auth reads)
MAX_ATTEMPT = 4                    # a run re-run this often is never parked again (no starving)

RENOVATE = ("renovate", "renovate[bot]", "app/renovate")  # the bot's logins: it merges ITSELF (platformAutomerge off)

Gh = Callable[..., subprocess.CompletedProcess]


def _ts(s: str | None) -> float | None:
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _tail(name: str) -> str:
    """'reviewer / review deferred' -> 'review deferred' (a reusable job's check is prefixed)."""
    return str(name or "").rsplit(" / ", 1)[-1]


def _json(read: Gh, *args: str) -> object:
    return json.loads(read(*args).stdout or "null")


def capacity_of(text: str) -> dict[str, int]:
    """'{"home-sealed": 1, "home-heavy": 1}' -> dict; '' = the scheduler is off. Raises on junk."""
    if not text.strip():
        return {}
    got = json.loads(text)
    if not isinstance(got, dict) or not all(isinstance(k, str) and isinstance(v, int) and v > 0
                                            for k, v in got.items()):
        raise ValueError(f"scarce-pools must be a JSON object of pool label -> capacity >= 1, got {text!r}")
    return got


# ── reading the pools ─────────────────────────────────────────────────────────────────────────
def active_runs(repo: str, read: Gh, branch: str = "") -> list[dict]:
    """Queued + in-progress workflow runs (REST shape), optionally one branch's."""
    out: list[dict] = []
    for status in ("queued", "in_progress"):
        q = f"repos/{repo}/actions/runs?status={status}&per_page=100" + (f"&branch={branch}" if branch else "")
        got = _json(read, "api", q)
        out += (got or {}).get("workflow_runs") or [] if isinstance(got, dict) else []
    return out


def run_jobs(repo: str, run_id: int, read: Gh) -> list[dict]:
    got = _json(read, "api", f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100")
    return (got or {}).get("jobs") or [] if isinstance(got, dict) else []


def pr_of(run: dict, by_branch: dict[str, int], repo: str = "") -> int:
    """The same-repo PR a run belongs to; 0 for a push/schedule run or a fork."""
    if run.get("event") not in ("pull_request", "pull_request_target"):
        return 0
    fork = str((run.get("head_repository") or {}).get("full_name") or "")
    if repo and fork and fork != repo:  # a fork's `patch-1` is not our `patch-1`
        return 0
    mine = by_branch.get(str(run.get("head_branch") or ""), 0)
    if mine:  # the open PR whose head IS this branch -- never a stacked PR that merely lists it
        return mine
    return next((int(p["number"]) for p in run.get("pull_requests") or [] if p.get("number")), 0)


def pool_jobs(repo: str, read: Gh, capacity: dict[str, int], by_branch: dict[str, int]) -> list[pr_queue.Job]:
    """Every queued/running job on a scarce pool, repo-wide."""
    out: list[pr_queue.Job] = []
    for run in active_runs(repo, read):
        for j in run_jobs(repo, int(run["id"]), read):
            status = str(j.get("status") or "")
            if status not in ("queued", "in_progress", "waiting", "pending"):
                continue
            pool = next((lb for lb in j.get("labels") or [] if lb in capacity), "")
            if not pool:
                continue
            out.append(pr_queue.Job(pr_of(run, by_branch, repo), int(run["id"]), int(run.get("run_attempt") or 1),
                                    _tail(j.get("name")), pool, "in_progress" if status == "in_progress" else "queued",
                                    _ts(j.get("created_at")) or _ts(run.get("created_at")) or 0.0,
                                    _ts(j.get("started_at")) if status == "in_progress" else None))
    return out


def parked_runs(repo: str, p: dict, read: Gh) -> tuple[list[dict], bool]:
    """(the PR's parked runs at its CURRENT head, anything still active there). A parked run is the
    newest run of its workflow at that head and is cancelled, or holds a failed `review deferred`."""
    sha = str(p.get("headRefOid") or "")
    got = _json(read, "api", f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100")
    runs = (got or {}).get("workflow_runs") or [] if isinstance(got, dict) else []
    newest: dict[int, dict] = {}
    for r in runs:
        w = int(r.get("workflow_id") or 0)
        if w not in newest or str(r.get("created_at")) > str(newest[w].get("created_at")):
            newest[w] = r
    active = any(r.get("status") != "completed" for r in newest.values())
    out = []
    for r in newest.values():
        if r.get("status") != "completed":
            continue
        if r.get("conclusion") == "cancelled":
            out.append(r)
        elif r.get("conclusion") == "failure" and any(
                _tail(j.get("name")) == DEFERRED_JOB and j.get("conclusion") == "failure"
                for j in run_jobs(repo, int(r["id"]), read)):
            out.append({**r, "deferred": True})
    return out, active


def review_settled(repo: str, branch: str, read: Gh) -> bool:
    """No review run on this branch still owes a verdict: each active run of the review gate has
    finished `record verdict` (or parked). An update now cancels only the gate's tail, and the next
    run can carry -- the 03:58Z case: head #3073 BEHIND, verdict recorded, update held for the tail."""
    for run in active_runs(repo, read, branch):
        names = {_tail(j.get("name")): j for j in run_jobs(repo, int(run["id"]), read)}
        if PRE_JOB not in names:
            continue  # not the review gate
        done = [n for n in (RECORD_JOB, DEFERRED_JOB) if (names.get(n) or {}).get("status") == "completed"]
        if not done:
            return False
    return True


# ── acting ────────────────────────────────────────────────────────────────────────────────────
def _label(repo: str, n: int, write: Gh, add: bool) -> bool:
    from ci_kit.pr_autoupdate import add_label  # lazy: pr_autoupdate imports this module
    if add:
        return add_label(repo, n, PARKED, PARKED_COLOR, PARKED_DESCRIPTION)[0]
    return write("pr", "edit", str(n), "-R", repo, "--remove-label", PARKED).returncode == 0


def ensure_label(repo: str, write: Gh) -> bool:
    """The review gate's admission adds `queue:parked` with the job token, which cannot create it."""
    return write("label", "create", PARKED, "-R", repo, "--color", PARKED_COLOR,
                 "--description", PARKED_DESCRIPTION, "--force").returncode == 0


def can_act(repo: str, read: Gh, write: Gh) -> str:
    """'' when the push token may cancel and re-run runs, else the refusal. The probe cancels a
    COMPLETED run created over a day ago: 409 with Actions: write, 403 without -- it changes nothing
    either way (a day old: no re-run of it is plausibly starting this second). A probe that cannot
    look, or a reply that is neither, does not block (the act itself still says)."""
    day = (dt.datetime.now(dt.UTC) - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        runs = json.loads(read("api", f"repos/{repo}/actions/runs?status=completed&created=<{day}&per_page=1").stdout
                          or "{}")
    except (subprocess.CalledProcessError, ValueError):
        return ""
    ids = [r.get("id") for r in runs.get("workflow_runs") or [] if r.get("id")]
    if not ids:
        return ""
    got = write("api", "-X", "POST", f"repos/{repo}/actions/runs/{ids[0]}/cancel")
    msg = (got.stderr or got.stdout or "").strip()
    if got.returncode and ("resource not accessible" in msg.lower() or "403" in msg):
        return _grant(msg[-300:])
    return ""


def _grant(msg: str) -> str:
    if "resource not accessible" in msg.lower() or "403" in msg:
        return msg + " -- the push token needs Actions: Read and write to park or re-run a run"
    return msg


def rerun(repo: str, r: dict, write: Gh) -> str:
    """'' on success. A parked review re-runs WHOLE (its `pre` job must decide again); a preempted
    run re-runs its cancelled jobs (finished ones are kept)."""
    verb = "rerun" if r.get("deferred") else "rerun-failed-jobs"
    got = write("api", "-X", "POST", f"repos/{repo}/actions/runs/{r['id']}/{verb}")
    return "" if got.returncode == 0 else _grant((got.stderr or got.stdout or "").strip()[-300:])


def out_of_queue(repo: str, prs: list[dict], before: str, sha: str, read: Gh) -> str:
    """'' or the page body: the push `before..sha` merged a PR OUTSIDE the queue while the head was
    in flight (current with `before`, no failing check) -- only an in-flight head was hurt. Outside
    = numbered after the head (the head was passed over), or Renovate's own merge whatever its
    number: with `platformAutomerge: false` (ci-kit's preset) it is never armed, never a member,
    and lands the moment its checks pass (td#3072 past head #3073, 03:53Z 11-14). A lower-numbered
    ARMED merge was the head itself (closed now, so `head` is the next one)."""
    if not before or not sha or set(before) == {"0"}:
        return ""
    merged = _json(read, "api", f"repos/{repo}/commits/{sha}/pulls")
    # a train landing IS the queue (ci_kit/merge_train); anything else merged past an in-flight head is not
    h = pr_queue.head(prs)
    if h is None:
        return ""
    nums = [int(m["number"]) for m in merged or [] if isinstance(m, dict) and m.get("merged_at")
            and not str((m.get("head") or {}).get("ref") or "").startswith("train/")
            and (int(m["number"]) > int(h["number"]) or str((m.get("head") or {}).get("ref") or "").startswith("renovate/")
                 or str((m.get("user") or {}).get("login") or "") in RENOVATE)]
    if not nums:
        return ""
    if pr_queue.check_state(h.get("statusCheckRollup")) == "failing":
        return ""
    cmp = _json(read, "api", f"repos/{repo}/compare/{before}...{h['headRefOid']}")
    if not isinstance(cmp, dict) or int(cmp.get("behind_by", 1)) != 0:
        return ""
    return (f"#{', #'.join(map(str, nums))} merged to main while queue head #{h['number']} was in flight "
            f"against the previous main ({before[:12]}): #{h['number']} must re-test. A merge outside the "
            "queue (Renovate's own automerge, a hand merge) costs the head its run.")


def pass_(repo: str, prs: list[dict], capacity: dict[str, int], read: Gh, write: Gh, summary: list[str],
          *, preempt_after_s: float, stall_s: float, behind: Callable[[dict], bool],
          before: str = "", sha: str = "", now: float | None = None, updated: frozenset[int] = frozenset()) -> int:
    """One scheduler pass (see the module doc). `updated` = PRs this run merged main into: their
    push starts fresh runs, so nothing of theirs is re-run here. Returns the errors."""
    now = dt.datetime.now(dt.UTC).timestamp() if now is None else now
    errors = 0
    same = [p for p in prs if not p.get("isCrossRepository")]
    by_branch = {str(p.get("headRefName")): int(p["number"]) for p in same}
    h = pr_queue.head(same)
    hn = int(h["number"]) if h else None
    off = can_act(repo, read, write) if capacity else ""
    if off:
        errors += 1  # parks NOTHING: a park this token cannot re-run would strand the PR
        print(f"::error::{repo}: the PR scheduler parks nothing -- {off}")
        summary.append(f"SCHEDULER OFF (parks nothing): {off}")
        # a label made while the grant held stays: mark it, so admission parks nothing either
        write("label", "edit", PARKED, "-R", repo, "--description", PARKED_OFF)
    elif capacity and not ensure_label(repo, write):
        errors += 1  # the review gate's admission can only park a labelled PR: until this lands it runs all
        print(f"::error::{repo}: could not create the `{PARKED}` label; admission parks nothing until it exists")
    oq = out_of_queue(repo, same, before, sha, read)
    if oq:
        summary.append(f"OUT-OF-QUEUE MERGE: {oq}; page " + page(f"{repo.rsplit('/', 1)[-1]}: out-of-queue merge", oq,
                                                                 str(h.get("url") or "") if h else ""))
        print(f"::warning::{oq}")
    trains = {int(p["number"]) for p in same if str(p.get("headRefName") or "").startswith("train/")}
    # a merge train IS the queue (ci_kit/merge_train): its jobs count as load, never as preemptible
    jobs = [dataclasses.replace(j, pr=0) if j.pr in trains else j for j in pool_jobs(repo, read, capacity, by_branch)]
    s = pr_queue.schedule(hn, jobs, capacity, now, preempt_after_s, stall_s, MAX_ATTEMPT)
    load = ", ".join(f"{pool} {sum(j.status == 'in_progress' for j in jobs if j.pool == pool)}/{cap} running "
                     f"{sum(j.status == 'queued' for j in jobs if j.pool == pool)} queued" for pool, cap in sorted(capacity.items()))
    summary.append(f"scheduler: head #{hn}" if hn else "scheduler: no queue head")
    summary.append(f"pools: {load}")
    summary += [f"head #{hn} waiting on {pool}: {w / 60:.1f} min" for pool, w in sorted(s.demand.items())]
    for line in s.stalled:
        errors += 1
        print(f"::warning::queue-head-starved {line}")
        summary.append(f"HEAD STARVED: {line}")
    preempt = [] if off else s.preempt
    errors += _park(repo, preempt, write, summary)
    room = not any(j.status == "queued" for j in jobs) and not s.demand
    errors += _refill(repo, same, hn, Refill({j.pr for j, _ in preempt}, set(updated), room, now, 2 * stall_s),
                      read, write, summary, behind)
    return errors


@dataclasses.dataclass
class Refill:
    """What `_refill` needs from the pass: who was just parked or updated, whether the pools have
    room, and how long a non-head may sit parked before the run says so."""
    parked: set[int]
    updated: set[int]
    room: bool
    now: float
    starve_s: float


def _park(repo: str, preempt: list[tuple[pr_queue.Job, str]], write: Gh, summary: list[str]) -> int:
    """Label each PR `schedule` named, THEN cancel its run. Returns the errors.

    Label first: `_refill` finds parked work by the label, so a run cancelled on an unlabelled PR
    would be stranded. No label, no cancel -- the job keeps the pool and runs to its end."""
    errors = 0
    for j, why in preempt:
        if not _label(repo, j.pr, write, True):
            errors += 1
            print(f"::error::#{j.pr}: could not add `{PARKED}`, so run {j.run} was NOT parked")
            summary.append(f"#{j.pr}: park of run {j.run} skipped: `{PARKED}` could not be added")
            continue
        got = write("api", "-X", "POST", f"repos/{repo}/actions/runs/{j.run}/cancel")
        if got.returncode:
            errors += 1
            err = _grant((got.stderr or got.stdout or "").strip()[-300:])
            print(f"::error::#{j.pr}: could not park run {j.run}: {err}")
            summary.append(f"#{j.pr}: park of run {j.run} REFUSED: {err}")
            continue  # the label stays; _refill drops it once nothing at the head is parked
        summary.append(f"#{j.pr}: PARKED run {j.run} ({j.name}): {why}")
    return errors


def _refill(repo: str, prs: list[dict], hn: int | None, r: Refill, read: Gh, write: Gh,
            summary: list[str], behind: Callable[[dict], bool]) -> int:
    """Re-run parked work: the head's now, one non-head's when the pools have room. Never strands:
    a PR with the label and nothing parked left loses the label -- but not while a run is still
    active at its head (that run may be the admission about to end parked, found next pass). A
    non-head parked longer than `starve_s` is an ERROR: the run goes red, which mcp's
    ci-schedule-audit reads. Returns the errors."""
    errors, refilled, first = 0, False, True
    for p in sorted((p for p in prs if any(lb.get("name") == PARKED for lb in p.get("labels") or [])),
                    key=pr_queue.landing_order):
        n = int(p["number"])
        if n in r.parked:
            continue  # parked this pass
        if n in r.updated:  # the update's push is its fresh run: nothing at the new head is parked
            _label(repo, n, write, False)
            summary.append(f"#{n}: updated this run; `{PARKED}` removed")
            continue
        runs, active = parked_runs(repo, p, read)
        if not runs:
            if not active:
                _label(repo, n, write, False)
                summary.append(f"#{n}: nothing parked at its head any more; `{PARKED}` removed")
            continue
        is_head = n == hn
        if is_head and p.get("autoMergeRequest") and behind(p):
            summary.append(f"#{n}: head, parked and behind main -> the queue's update is its fresh run")
            continue
        if not is_head and (refilled or not r.room):
            since = min((_ts(x.get("updated_at")) or r.now) for x in runs)
            line = f"#{n}: parked ({len(runs)} run(s)) {(r.now - since) / 60:.0f} min, waits for a free pool"
            # only the NEXT in line can starve: the ones behind it wait for it, which is queue order
            if first and r.now - since > r.starve_s:
                errors += 1
                print(f"::warning::parked-pr-starved {line}")
                line = "PARKED STARVED: " + line
            first = False
            summary.append(line)
            continue
        # no budget needed here: admission and preemption stop parking a run at its MAX_ATTEMPT-th
        # attempt, so a re-run is the last one that can be parked
        errs = [e for e in (rerun(repo, r, write) for r in runs) if e]
        if errs:
            errors += 1
            print(f"::error::#{n}: re-run refused: {'; '.join(errs)}")
            summary.append(f"#{n}: parked re-run REFUSED: {'; '.join(errs)}")
            continue
        refilled |= not is_head
        _label(repo, n, write, False)
        summary.append(f"#{n}: parked run(s) {', '.join(str(r['id']) for r in runs)} RE-RUN "
                       + ("(queue head)" if is_head else "(pool free, next in landing order)"))
    return errors
