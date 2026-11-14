"""Which red needs judgment and which only says the MACHINERY failed: the one classifier.

ONE CLASSIFIER, THREE READERS. pr_queue (which armed PR may take the update slot), pr_heal (rerun
a machinery red, comment a judgment one) and the merge train (release a red train's riders
instead of bisecting them, ci_kit/merge_train/policy.red_is_infra) all ask the same question of
a failed check: is this evidence against the code? This module answers it, purely, from two
depths of GitHub's answer:

  * the ROLLUP (a check run's conclusion, a commit status's state): enough only for a check that
    never started -- STARTUP_FAILURE, a status ERROR. `rollup_infra` is the train's original
    test, moved here unchanged.
  * the JOBS of the workflow run behind a check (REST `actions/runs/{id}/jobs`), their steps,
    their check-run annotations and, for a failed SETUP step, the tail of its log. This is the
    depth that sees trade-data#2988's red (run 43115550872, 2026-11-11 22:55-23:11Z): the sealed
    reviewer job "failed" while its VM restarted -- no step failed (the `Review` step never
    concluded), the annotation says "The self-hosted runner lost communication with the server",
    and the log API answers BlobNotFound/404. Its alls-green `review gate` job then failed as a
    CONSEQUENCE. The rollup saw two FAILUREs and nothing else.

THE CLASSES of one failed job (`job_class`):
  machinery    no evidence against the code: STARTUP_FAILURE; CANCELLED; a FAILURE in which no
               step failed (the runner died, was lost, or never ran a step); a runner-lost or
               never-acquired annotation; a TIMED_OUT job that never ran a step; a failed SETUP
               step (set up job, checkout, setup-*, an action download, a container start) whose
               log tail names a network/TLS/DNS/5xx error, or whose log GitHub no longer has.
  consequence  an aggregator (re-actors/alls-green) that failed because a job it needs failed.
               It is machinery when its run holds a machinery job, judgment otherwise.
  parked       the review gate's `review deferred` job (v1.29.0): the PR queue (ci_kit/pr_sched)
               parked the review so the queue head gets the sealed runner. Not a failure of the PR
               and not the machinery's either: the scheduler re-runs it (a plain rerun-failed-jobs
               would park it again), so no reader retries it or calls it judgment.
  judgment     everything else: a test, a lint, a review verdict, a step that failed on its own.
A run (`run_class`) is judgment when ANY failed job is; else parked when one is parked; else
machinery only when EVERY failed job in it is machinery, or a consequence of one.
Anything unrecognised is judgment: a guess the wrong way costs a CI rerun at worst, but
a real failure read as machinery would be retried -- so the default is never to retry.
"""
from __future__ import annotations

import re

PASS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
# CANCELLED / STALE are NOT failures for the train's phase: a cancelled check is no evidence
# against any PR. pr_heal treats a cancelled required check on a PR's head as machinery to rerun.
FAIL = {"FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
INFRA = {"STARTUP_FAILURE"}  # the check never ran its steps: no evidence against the code
RED_RUN = {"failure", "cancelled", "startup_failure", "timed_out"}  # a workflow run worth classifying

MACHINERY, CONSEQUENCE, JUDGMENT, PARKED = "machinery", "consequence", "judgment", "parked"
# the review gate's job that fails, on purpose, when the PR queue parks a review ("<caller> / review deferred")
DEFERRED_JOB = re.compile(r"(^|/ )review deferred$")

# A step that prepares the job and runs none of the PR's code. Its failure is the runner's or the
# network's, never the change's -- but only with the log's word for it (below).
SETUP_STEP = re.compile(
    r"^(set up job|set up runner|initialize containers|complete runner|download action repository"
    r"|(post )?run (actions/(checkout|setup-[\w-]+|cache|download-artifact|upload-artifact)"
    r"|astral-sh/setup-uv|docker/(login|setup-[\w-]+)-action)@)", re.I)
AGGREGATOR_STEP = re.compile(r"re-actors/alls-green", re.I)
# The log line that says the network, not the code, failed the setup step.
NETWORK = re.compile(
    r"tls handshake|ssl[_ ]error|certificate verify failed|econnreset|etimedout|econnrefused"
    r"|could not resolve host|temporary failure in name resolution|name or service not known"
    r"|connection (reset|refused|timed out)|network is unreachable|i/o timeout|dial tcp"
    r"|getaddrinfo|socket hang up|rpc failed|early eof|the remote end hung up|unexpected disconnect"
    r"|\b(502|503|504)\b.{0,40}(bad gateway|service unavailable|gateway time-?out)"
    r"|received unexpected http status: 5\d\d|http (error )?5\d\d|api rate limit exceeded"
    r"|failed to download action|tar: unexpected eof", re.I)
# A check-run annotation GitHub writes when the RUNNER, not a step, ended the job.
RUNNER_LOST = re.compile(
    r"runner lost communication|received a shutdown signal|was not acquired by runner"
    r"|runner has been (deleted|removed)|lost communication with the server", re.I)
# A check-run annotation GitHub writes when the job's OWN timeout-minutes expired (conclusion: cancelled).
TIMEOUT = re.compile(r"exceeded the maximum execution time", re.I)
LOG_GONE = re.compile(r"blobnotfound|http 404|not found", re.I)


def _concl(x: dict) -> str:
    return str(x.get("conclusion") or "").lower()


def job_class(job: dict, annotations: list[str] = (), log_error: str = "", log_tail: str = "") -> tuple[str, str]:
    """(class, why) for ONE job of a workflow run (REST shape: conclusion, steps[], runner_name).
    `annotations` = its check run's annotation messages; `log_error` = what the logs API said when
    the log could not be read ('' = read or not asked); `log_tail` = the failed step's last lines."""
    c = _concl(job)
    if c in ("success", "skipped", "neutral", ""):
        return "pass", ""
    if c == "startup_failure":
        return MACHINERY, "the job never started (startup failure)"
    if c == "failure" and DEFERRED_JOB.search(str(job.get("name") or "")):
        return PARKED, "parked by the PR queue so the queue head gets the runner -- not a failure of this PR"
    steps = [s for s in job.get("steps") or [] if isinstance(s, dict)]
    failed = [s for s in steps if _concl(s) == "failure"]
    own = [s for s in failed if not SETUP_STEP.search(str(s.get("name") or ""))
           and not AGGREGATOR_STEP.search(str(s.get("name") or ""))]
    if own:  # a step of the job's own work failed: evidence, whatever happened to the runner after
        return JUDGMENT, "step failed: " + ", ".join(str(s.get("name")) for s in own)  # even if cancelled after
    ran = any(_concl(s) for s in steps)
    hung = next((a for a in annotations if TIMEOUT.search(a or "")), "")
    if hung and ran:  # GitHub reports a job timeout-minutes expiry as CANCELLED: a hang is the PR's
        return JUDGMENT, f"hit its time limit while running its steps: {hung.strip()[:120]}"
    lost = next((a for a in annotations if RUNNER_LOST.search(a or "")), "")
    if lost:
        return MACHINERY, f"the runner ended the job: {lost.strip()[:120]}"
    if c == "cancelled":
        return MACHINERY, "cancelled before it finished"
    if c == "timed_out":
        if not ran:
            return MACHINERY, "timed out before a runner ran any step (provisioning)"
        return JUDGMENT, "timed out while running its steps"
    if not failed:
        # td#2988's shape ONLY: a runner ran steps and one began and never concluded. A failure with
        # no step at all is a workflow-expression error (strategy, runs-on, if:) -- the PR's.
        if ran and any(not _concl(s) for s in steps):
            gone = " (its log is gone: " + log_error.strip()[:60] + ")" if log_error and LOG_GONE.search(log_error) else ""
            return MACHINERY, f"a step began and never concluded -- the runner was lost{gone}"
        return JUDGMENT, "failed with no failed step and no sign the runner was lost"
    if len(failed) == 1 and AGGREGATOR_STEP.search(str(failed[0].get("name") or "")):
        return CONSEQUENCE, "an aggregator red because a job it needs failed"
    if all(SETUP_STEP.search(str(s.get("name") or "")) for s in failed):
        names = ", ".join(str(s.get("name")) for s in failed)
        if log_error and LOG_GONE.search(log_error):
            return MACHINERY, f"setup step failed ({names}) and its log is gone"
        hit = NETWORK.search(log_tail or "")
        if hit:
            return MACHINERY, f"setup step failed ({names}) on the network: {hit.group(0)}"
        return JUDGMENT, f"setup step failed ({names}) with no network error in its log"
    return JUDGMENT, "steps failed: " + ", ".join(str(s.get("name")) for s in failed)  # aggregator + setup mix


def run_class(classes: list[tuple[str, str, str]]) -> tuple[str, str]:
    """(machinery | judgment | pass, why) for one run from [(job name, class, why)] of its jobs."""
    red = [(n, c, w) for n, c, w in classes if c != "pass"]
    if not red:
        return "pass", ""
    verdicts = [(n, w) for n, c, w in red if c == JUDGMENT]
    if verdicts:
        return JUDGMENT, "; ".join(f"`{n}`: {w}" for n, w in verdicts)
    parked = [(n, w) for n, c, w in red if c == PARKED]
    if parked:  # the aggregator beneath it is its consequence; the scheduler re-runs the whole run
        return PARKED, "; ".join(f"`{n}`: {w}" for n, w in parked)
    mach = [(n, w) for n, c, w in red if c == MACHINERY]
    if not mach:  # an aggregator red with nothing beneath it that failed: say so, never retry it
        return JUDGMENT, "; ".join(f"`{n}`: an aggregator red with no failed job beneath it" for n, _, _ in red)
    return MACHINERY, "; ".join(f"`{n}`: {w}" for n, w in mach)


def rollup_infra(rollup: list[dict] | None, required: tuple[str, ...]) -> bool:
    """Every failing required check failed as MACHINERY (never started), none as a verdict.
    Rollup depth only; the jobs depth is run_class."""
    bad = []
    for c in rollup or []:
        name = c.get("name") or c.get("context") or ""
        if required and name not in required:
            continue
        if c.get("__typename") == "StatusContext" or ("state" in c and "status" not in c):
            state = (c.get("state") or "").upper()
            if state in ("FAILURE", "ERROR"):
                bad.append("infra" if state == "ERROR" else "verdict")
        elif (c.get("status") or "").upper() == "COMPLETED" and (c.get("conclusion") or "").upper() in FAIL:
            bad.append("infra" if (c.get("conclusion") or "").upper() in INFRA else "verdict")
    return bool(bad) and all(b == "infra" for b in bad)


def judgment_kind(check: str, why: str) -> str:
    """A one-word name for a judgment red, for the PR comment and the outside watcher."""
    s = f"{check} {why}".lower()
    if "review" in s:
        return "review"
    if re.search(r"\btest|pytest|vitest|jest", s):
        return "test"
    if re.search(r"lint|ruff|mypy|eslint|format|ratchet|baseline|contract|doc", s):
        return "lint"
    return "check"
