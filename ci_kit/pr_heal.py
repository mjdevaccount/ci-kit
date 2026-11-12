"""Smart auto-merge: an armed PR whose red needs NO judgment fixes itself; one that needs judgment says so once.

WHY (trade-data operator, 2026-11-11: "should have some kind of smart auto merge to handle the ones
with smart fails"). Eight armed trade-data PRs backed up for hours that evening and nothing healed
them: a sealed reviewer VM restarted mid-review (td#2988, run 43115550872: no step failed, the log
API answers BlobNotFound) and the PR sat red until a person pointed at it. The updater
(pr_autoupdate) held it as "a check is failing (the author's fix re-queues it)" -- but there was
no fix for an author to make.

WHAT IT DOES, per armed PR whose required checks are red (failing or cancelled) at its head, from
ci_kit/redclass.py's classes (the one classifier, shared with pr_queue and the merge train):
  machinery, PR BEHIND main  -> nothing here: pr_queue makes it ELIGIBLE (`machinery`), and the
                                update the queue gives it is its fresh run. No rerun permission needed.
  machinery, PR current      -> rerun the failed jobs of each red run (`rerun-failed-jobs`, which
                                also reruns the jobs that need them -- the alls-green aggregator), at
                                most `limit` times per head, each one RECORDED as a PR comment.
  machinery, budget spent    -> ONE comment per head naming it, and the run goes RED (exit 1).
  judgment                   -> ONE comment per head naming the class (test / lint / review /
                                check) and the failing step. Nothing is retried, ever: a real test
                                failure is never rerun. A new push re-runs CI.
A head with ANY judgment red is judgment as a whole: rerunning its machinery reds would spend CI
on a head that cannot merge until someone pushes.

THE RECORD (append-only, in the PR's own comments, read back as the budget -- the merge train's
`released` markers, same idea): `<!-- smart-merge:rerun head=<sha> runs=<id,...> -->`,
`<!-- smart-merge:exhausted head=<sha> -->`, `<!-- smart-merge:judgment head=<sha> kind=<k> -->`.
The budget is the larger of the rerun markers at the head and the run's own attempt count - 1, so a
hand rerun spends it too and a lost comment cannot reset it. A new head starts a new budget.

THE TOKEN. A rerun needs Actions WRITE. The reusable workflow cannot ask GITHUB_TOKEN for it: a
called workflow that requests a permission its caller did not grant fails to START, for every
caller. So the rerun goes through the PAT (`GH_TOKEN`, the updater's push token); a PAT without
Actions write is refused, and that refusal is an ERROR naming the grant -- never a silent skip.

WATCHED FROM OUTSIDE: mcp scripts/pr_reconcile.py reads the `exhausted` and `judgment` markers on
open armed PRs into the PR-LAND brief line (a red here also turns pr-autoupdate's run red, which
mcp ci-schedule-audit names).
"""
from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

from ci_kit import redclass
from ci_kit.merge_train import RIDING, TRAIN_PREFIX

RERUN = "<!-- smart-merge:rerun head={head} runs={runs} -->"
EXHAUSTED = "<!-- smart-merge:exhausted head={head} -->"
JUDGED = "<!-- smart-merge:judgment head={head} kind={kind} -->"
_RERUN = re.compile(r"<!-- smart-merge:rerun head=([0-9a-f]{40}) runs=([0-9,]+) -->")
_EXHAUSTED = re.compile(r"<!-- smart-merge:exhausted head=([0-9a-f]{40}) -->")
_JUDGED = re.compile(r"<!-- smart-merge:judgment head=([0-9a-f]{40}) kind=([a-z]+) -->")
_JOB_URL = re.compile(r"/actions/runs/(\d+)/job/(\d+)")
LIMIT = 2
_CANCELLED = {"CANCELLED"}

# `read` RAISES CalledProcessError when GitHub will not answer (pr_autoupdate._gh_read: the job's
# read token first, then the PAT); `write` never raises and is judged by its return code.
Gh = Callable[..., subprocess.CompletedProcess]


@dataclass
class Red:
    """One red workflow run behind one or more of a PR's red required checks, classified."""
    checks: list[str]
    run: int
    attempt: int
    cls: str
    why: str
    url: str


@dataclass
class Verdict:
    number: int
    head: str
    reds: list[Red] = field(default_factory=list)
    unrunnable: list[str] = field(default_factory=list)  # a red check no Actions run stands behind

    @property
    def kind(self) -> str:
        if self.unrunnable or any(r.cls == redclass.JUDGMENT for r in self.reds):
            return redclass.JUDGMENT
        return redclass.MACHINERY if self.reds else "none"


# ── pure ──────────────────────────────────────────────────────────────────────────────────────
def red_checks(rollup: list[dict] | None, required: tuple[str, ...] = ()) -> list[str]:
    """The required checks red at a head: FAILING, or CANCELLED (a cancelled required check blocks
    a merge forever and no author's push is owed for it). A name reported twice takes its newest
    answer: GraphQL lists the latest check run per name per suite. Pure."""
    out: list[str] = []
    newest: dict[str, dict] = {}
    for c in rollup or []:  # two runs of one workflow at one head are two suites: keep the later
        name = c.get("name") or c.get("context") or ""
        if name not in newest or str(c.get("startedAt") or "") >= str(newest[name].get("startedAt") or ""):
            newest[name] = c
    for name, c in newest.items():
        if required and name not in required:
            continue
        if c.get("__typename") == "StatusContext" or ("state" in c and "status" not in c):
            if (c.get("state") or "").upper() in redclass.FAIL:
                out.append(name)
        elif (c.get("status") or "").upper() == "COMPLETED" and \
                (c.get("conclusion") or "").upper() in redclass.FAIL | _CANCELLED:
            out.append(name)
    return sorted(set(out))


def spent(bodies: list[str], head: str, attempts: list[int]) -> int:
    """Reruns already spent at `head`: the larger of the recorded ones and what the runs' own
    attempt counters say (a hand rerun counts; a lost comment does not reset it). Pure."""
    marked = sum(1 for b in bodies for m in _RERUN.finditer(b or "") if m.group(1) == head)
    return max(marked, max((a - 1 for a in attempts), default=0))


def marked(bodies: list[str], pattern: re.Pattern[str], head: str) -> bool:
    return any(m.group(1) == head for b in bodies for m in pattern.finditer(b or ""))


def decide(v: Verdict, bodies: list[str], behind: bool, limit: int) -> tuple[str, str]:
    """(action, why) for one assessed PR. Pure: the policy the tests pin.
    action: none | update (the queue's) | rerun | exhausted | judgment | quiet (already said)."""
    kind = v.kind
    if kind == "none":
        return "none", ""
    if kind == redclass.JUDGMENT:
        if marked(bodies, _JUDGED, v.head):
            return "quiet", "judgment already named at this head"
        return "judgment", "; ".join([r.why for r in v.reds if r.cls == redclass.JUDGMENT]
                                     + [f"`{c}`: not an Actions check (cannot be rerun)" for c in v.unrunnable])
    if behind:
        return "update", "machinery red and behind main: the queue's update is its fresh run"
    done = spent(bodies, v.head, [r.attempt for r in v.reds])
    if done >= limit:
        if marked(bodies, _EXHAUSTED, v.head):
            return "quiet", f"rerun budget spent ({done}/{limit}), already named"
        return "exhausted", f"{done} rerun(s) at this head and still red on machinery"
    return "rerun", f"rerun {done + 1} of {limit} at this head"


def rerun_comment(v: Verdict, done: int, limit: int) -> str:
    runs = ",".join(str(r.run) for r in v.reds)
    lines = [RERUN.format(head=v.head, runs=runs),
             f"**smart auto-merge**: reran the failed jobs at `{v.head[:12]}` (rerun {done + 1} of {limit} at this head)."
             " Machinery, not code -- no evidence against this PR:"]
    lines += [f"- [{', '.join(r.checks)}]({r.url}) attempt {r.attempt}: {r.why}" for r in v.reds]
    return "\n".join(lines)


def exhausted_comment(v: Verdict, limit: int) -> str:
    lines = [EXHAUSTED.format(head=v.head),
             f"**smart auto-merge**: still red on MACHINERY at `{v.head[:12]}` after {limit} rerun(s). Not retried again at"
             " this head -- the runner pool or the network is failing, not this PR. A new push, or a hand rerun once the"
             " pool is healthy, retries it:"]
    lines += [f"- [{', '.join(r.checks)}]({r.url}): {r.why}" for r in v.reds]
    return "\n".join(lines)


def judgment_comment(v: Verdict) -> str:
    bad = [r for r in v.reds if r.cls == redclass.JUDGMENT]
    kind = redclass.judgment_kind(", ".join(c for r in bad for c in r.checks) or ",".join(v.unrunnable),
                                  " ".join(r.why for r in bad))
    lines = [JUDGED.format(head=v.head, kind=kind),
             f"**smart auto-merge**: left for a session -- a **{kind}** red at `{v.head[:12]}` needs judgment. Nothing is"
             " retried automatically; a push re-runs CI."]
    lines += [f"- [{', '.join(r.checks)}]({r.url}): {r.why}" for r in bad]
    lines += [f"- `{c}`: a commit status, not an Actions check" for c in v.unrunnable]
    return "\n".join(lines)


# ── GitHub ────────────────────────────────────────────────────────────────────────────────────
def _json(read: Gh, *args: str) -> object:
    return json.loads(read(*args).stdout or "null")


def classify_run(repo: str, run_id: int, read: Gh) -> tuple[int, str, str, str]:
    """(attempt, class, why, url) for one workflow run, from its latest attempt's jobs."""
    run = _json(read, "api", f"repos/{repo}/actions/runs/{run_id}")
    run = run if isinstance(run, dict) else {}
    jobs = _json(read, "api", f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100")
    jobs = (jobs or {}).get("jobs") or [] if isinstance(jobs, dict) else []
    out: list[tuple[str, str, str]] = []
    for j in jobs:
        if str(j.get("conclusion") or "").lower() not in redclass.RED_RUN:
            continue
        notes = _json(read, "api", f"repos/{repo}/check-runs/{j['id']}/annotations")  # job id == check run id
        msgs = [str(a.get("message") or "") for a in notes] if isinstance(notes, list) else []
        log_error = tail = ""
        failed = [s for s in j.get("steps") or [] if str(s.get("conclusion") or "") == "failure"]
        if failed and all(redclass.SETUP_STEP.search(str(s.get("name") or "")) for s in failed):
            try:  # only a failed SETUP step needs its log: the network's word, or the log's absence
                tail = "\n".join((read("run", "view", str(run_id), "-R", repo, "--log-failed", "--job",
                                        str(j["id"])).stdout or "").splitlines()[-40:])
                log_error = "" if tail else "no log"
            except subprocess.CalledProcessError as e:
                log_error = (e.stderr or "no log").strip()[-200:]
        out.append((str(j.get("name")), *redclass.job_class(j, msgs, log_error, tail)))
    if not out and str(run.get("conclusion") or "").lower() in redclass.RED_RUN:
        # a run red with no job red: it never ran one (a startup failure, a run cancelled whole)
        out.append(("(run)", redclass.MACHINERY, f"the run ended {run.get('conclusion')} with no job run"))
    cls, why = redclass.run_class(out)
    return int(run.get("run_attempt") or 1), cls, why, str(run.get("html_url") or f"run {run_id}")


def assess(repo: str, pr: dict, required: tuple[str, ...], read: Gh) -> Verdict:
    """Classify every red required check at the PR's head. Raises when GitHub will not answer."""
    head = str(pr.get("headRefOid") or "")
    v = Verdict(int(pr["number"]), head)
    names = red_checks(pr.get("statusCheckRollup"), required)
    if not names:
        return v
    got = read("api", "--paginate", f"repos/{repo}/commits/{head}/check-runs?per_page=100",
             "--jq", ".check_runs[] | {id, name, details_url}")
    newest: dict[str, dict] = {}
    for line in (got.stdout or "").splitlines():
        c = json.loads(line)
        if c.get("name") in names and int(c["id"]) > int((newest.get(c["name"]) or {}).get("id") or 0):
            newest[c["name"]] = c
    by_run: dict[int, list[str]] = {}
    for name in names:
        m = _JOB_URL.search(str((newest.get(name) or {}).get("details_url") or ""))
        if m:
            by_run.setdefault(int(m.group(1)), []).append(name)
        else:
            v.unrunnable.append(name)
    for run_id, checks in sorted(by_run.items()):
        attempt, cls, why, url = classify_run(repo, run_id, read)
        if cls != "pass":
            v.reds.append(Red(checks, run_id, attempt, cls, why, url))
    return v


def rerun(repo: str, v: Verdict, write: Gh) -> str:
    """Rerun each red run's failed jobs with the PAT. '' = all accepted, else what refused."""
    errs = []
    for r in v.reds:
        got = write("api", "-X", "POST", f"repos/{repo}/actions/runs/{r.run}/rerun-failed-jobs")
        if got.returncode:
            msg = (got.stderr or got.stdout or "").strip()[-300:]
            if re.search(r"not accessible|403|must have admin|permission", msg, re.I):
                msg += (" -- the push token needs Actions: Read and write (fine-grained PAT) to rerun a machinery red;"
                        " until it has it, a machinery red on a PR current with main waits for a push")
            errs.append(f"run {r.run}: {msg}")
    return "; ".join(errs)


# ── the pass: pr_autoupdate calls these around its update loop ──────────────────────────────────
Heal = dict[int, tuple[Verdict, list[str], dict]]  # number -> (verdict, its comments, the PR row)


def assess_armed(repo: str, armed: list[dict], required: tuple[str, ...], limit: int, read: Gh,
                 comments: Callable[[str, int], list[str]], summary: list[str]) -> tuple[Heal, int]:
    """A verdict for every armed PR red at its head, and the errors. Skipped: a train or a rider
    (the train judges those), a DIRTY PR (the conflict path), and a head whose judgment or spent
    budget is already named on the PR. A PR GitHub would not explain is an ERROR, left as it was --
    never guessed machinery."""
    out: Heal = {}
    errors = 0
    if limit <= 0:
        return out, 0
    for p in armed:
        if (str(p.get("headRefName") or "").startswith(TRAIN_PREFIX)
                or (p.get("mergeStateStatus") or "").upper() == "DIRTY"
                or any(lb.get("name") == RIDING for lb in p.get("labels") or [])
                or not red_checks(p.get("statusCheckRollup"), required)):
            continue
        n, head = int(p["number"]), str(p.get("headRefOid") or "")
        try:
            bodies = comments(repo, n)
            if marked(bodies, _JUDGED, head) or marked(bodies, _EXHAUSTED, head):
                summary.append(f"#{n}: red at {head[:12]}, already named on the PR (smart auto-merge)")
                continue
            out[n] = (assess(repo, p, required, read), bodies, p)
        except (subprocess.CalledProcessError, ValueError, KeyError) as e:
            errors += 1
            err = (getattr(e, "stderr", "") or "").strip()[-300:]
            print(f"::error::#{n}: could not classify its red checks: {e} {err}")
            summary.append(f"#{n}: red, NOT classified (GitHub would not say): {e}")
    return out, errors


def act(repo: str, heal: Heal, updated: set[int], limit: int, write: Gh, summary: list[str]) -> int:
    """Carry out decide() for each assessed PR. Every action is recorded: a PR comment carrying its
    marker, and a summary line. Returns the errors (a refused rerun, a spent budget, a failed record)."""
    errors = 0

    def say(n: int, body: str) -> bool:
        r = write("pr", "comment", str(n), "-R", repo, "--body-file", "-", input_text=body)
        if r.returncode:
            print(f"::error::#{n}: comment failed: {(r.stderr or r.stdout or '').strip()[:300]}")
        return r.returncode == 0

    for n, (v, bodies, p) in sorted(heal.items()):
        behind = (p.get("mergeStateStatus") or "UNKNOWN").upper() in ("BEHIND", "UNKNOWN")
        action, why = decide(v, bodies, behind, limit)
        reds = "; ".join(f"{', '.join(r.checks)}: {r.cls}" for r in v.reds) or ", ".join(v.unrunnable)
        line = f"#{n}: red at {v.head[:12]} ({reds}) -> {action}" + (f": {why}" if why else "")
        if action == "update":
            line += "; updated this run" if n in updated else "; waits its turn in the queue"
        elif action == "rerun":
            done = spent(bodies, v.head, [r.attempt for r in v.reds])
            err = rerun(repo, v, write)
            if err:
                errors += 1
                print(f"::error::#{n}: machinery rerun refused: {err}")
                line += f"; RERUN REFUSED: {err}"
            elif not say(n, rerun_comment(v, done, limit)):
                errors += 1
                line += "; rerun made, its record comment FAILED (the run's attempt counter still spends the budget)"
        elif action == "exhausted":
            errors += 1  # loud: the pool keeps failing this head; a person or a session must look
            print(f"::error::#{n}: still red on machinery after {limit} rerun(s) at {v.head[:12]}")
            if not say(n, exhausted_comment(v, limit)):
                line += "; its comment FAILED"
        elif action == "judgment" and not say(n, judgment_comment(v)):
            errors += 1
            line += "; its comment FAILED"
        summary.append(line)
    return errors
