#!/usr/bin/env python3
"""ci-merge-train: one pass of the merge train -- finalize, judge the open train, compose the next.

ONE PASS, IN ORDER (the caller runs it on a cron, on dispatch, on a train PR closing, on a check
workflow finishing on a train branch, and when a PR gets the `merge-train` label):
  1. FINALIZE every train PR merged and not yet finalized (phases.finalize): each constituent
     whose head is still the one that rode is commented "landed as <sha> via train #M" (once),
     closed, and only then labelled `landed-via-train`; one whose head moved since is told whether
     it moved forward or was rewritten, and left open. The train PR gets `landed-via-train` only
     when every rider is settled, so a crash or a failed close is finished by the next pass.
  2. JUDGE the open train this workflow composed (phases.authority; any other `train/` PR is
     never touched, only named, red): pending -> stop here; green -> re-verify every rider and
     merge it (phases.land); red -> close it and bisect (policy.split), unless the red is no
     evidence (a check that never ran, a BLIND/BLOCKED gate) -> release every rider for a retry;
     stale (main moved) / stuck (pending past --stale-minutes) -> close it, release the riders.
  3. RELEASE strays: an `in-train` label on a PR no open train carries (pr_autoupdate skips it).
  4. COMPOSE when >= --min-batch candidates (policy.candidates) remain, oldest-armed first, at
     most --max-batch boarding: each one's release history (policy.rider_gate: eject / hold), its
     head fetched and its review re-read at that head ON THIS BASE (github.review_passed +
     git.review_base_ok), the train composed (git.compose), pushed to `train/<run>`, opened as a
     PR labelled `merge-train` with a constituent table and its composition record, NOT armed,
     and every rider labelled `in-train` and commented with a `rode` marker. A conflicting PR is
     commented once per head and skipped; with one candidate nothing runs.

A TRAIN NEVER force-pushes main and never bypasses protection: it lands only through GitHub's
own merge of the train PR (requested by this CLI after the land-time re-check), whose required
checks (the review gate's train path among them, ci_kit/review/train.py) ran on exactly the tree
that lands. A rebase merge makes one main commit
per PR with the PR's own title, body and trailers; the tree of the last one is the train's tested
tree, so a caller's "tested-tree" reuse (trade-data ci.yml) re-tests nothing on that push.

THE TOKEN is the caller's fine-grained PAT (`GH_TOKEN`: contents, pull requests and workflows
RW, actions read) -- a push or PR made with GITHUB_TOKEN fires no workflow, so the train's checks
would never start. Check runs are read with `CI_KIT_READ_TOKEN` (GITHUB_TOKEN), as pr_autoupdate
does: GitHub refuses a fine-grained PAT the check runs.

TELEMETRY. Each pass writes its summary (step summary + log): the workflow run is the
append-only record, and every train is ALSO a PR whose body and comments say what rode, what
was skipped and why, and how it ended. Every rider carries machine-readable markers in its own
comments (`rode`, `released ... why=`, `landed`), which is how a rider's history survives across
trains. LOUD (exit 1, ::error) rather than logged: a stuck train, a retry on machinery, an
ejection or hold for repeated releases, a green train GitHub would not merge, a `train/` PR this
workflow did not compose, a merged train not fully settled. What watches from outside: mcp
scripts/train_watch.py via pr_reconcile.py (hourly) names a RIDER in trains at one head for
hours (across trains, from its `rode` markers), a constituent closed as landed whose commit is
not on main, and an `in-train` label with no open train, in the brief's PR-LAND line; mcp
ci-schedule-audit names the merge-train workflow itself going quiet or red.

Exit 0 = the pass finished; 1 = it finished but something it tried failed (named in the log);
2 = could not finish looking (gh or git could not answer).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ci_kit import pr_list
from ci_kit.merge_train import RIDING, ops, phases, policy
from ci_kit.merge_train import git as tgit
from ci_kit.merge_train.ops import Opts, Pass, close, comment, fetch, label
from ci_kit.pr_autoupdate import _gh_read

__all__ = ["Opts", "Pass", "main", "run", "workspace"]


def workspace(repo: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix="merge-train-"))
    tgit.git(d, "init", "-q")
    tgit.git(d, "remote", "add", "origin", f"https://github.com/{repo}.git")
    # the PAT reaches git through gh's own credential helper (GH_TOKEN): never argv, never a URL
    tgit.git(d, "config", "credential.https://github.com.helper", "")
    tgit.git(d, "config", "--add", "credential.https://github.com.helper", "!gh auth git-credential")
    return d


# ── the pass ───────────────────────────────────────────────────────────────────────────────────
def run(o: Opts, p: Pass) -> int:
    d = workspace(o.repo)
    fetch(d, f"+refs/heads/{o.base}:refs/remotes/origin/{o.base}")
    main_sha = tgit.out(d, "rev-parse", f"refs/remotes/origin/{o.base}")
    phases.finalize(o, p, d)
    prs = pr_list.rows(json.loads(_gh_read(*pr_list.gh_args(o.repo)).stdout))
    me = ops.me()
    trains: list[dict] = []
    for t in sorted((x for x in prs if policy.is_train(x)), key=lambda x: int(x["number"])):
        why = phases.authority(o, t, me.name)
        if why:  # never judged, merged or closed here; the review gate blocks it too
            p.error(f"#{t['number']} is a `train/` PR this workflow did not compose ({why}): left alone")
        else:
            trains.append(t)
    riding: set[int] = set()
    exclude: frozenset[int] = frozenset()
    for extra in trains[:-1]:  # one train at a time: an older open one is superseded
        comment(o, p, int(extra["number"]), "Closed: a newer train is open.")
        close(o, p, int(extra["number"]), delete_branch=True)
    if trains:
        stop, riding, exclude = phases.judge(o, p, d, trains[-1], main_sha)
        if stop:
            for n in policy.stray_riders(prs, riding):
                label(o, p, n, remove=RIDING)
                p.say(f"#{n}: stray `{RIDING}` label removed")
            return 1 if p.errors else 0
    for n in policy.stray_riders(prs, riding):
        label(o, p, n, remove=RIDING)
    cands, no = policy.candidates(prs, o.required, o.base, exclude)
    p.say(f"{len(cands)} candidate(s) of {len(prs)} open PR(s)"
          + (f": {', '.join('#' + str(c['number']) for c in cands)}" if cands else ""))
    for x, why in no:
        if x.get("autoMergeRequest") and not policy.is_train(x):
            p.say(f"#{x['number']}: {why}")
    if len(cands) >= o.min_batch:
        phases.compose(o, p, d, cands, main_sha, me)
    else:
        p.say(f"fewer than {o.min_batch} candidates: no train (a single PR lands on its own auto-merge)")
    return 1 if p.errors else 0


def _own_workflow() -> str:
    return os.environ.get("GITHUB_WORKFLOW_REF", "").split("@", 1)[0].rsplit("/", 1)[-1] or "merge-train.yml"


def _run_id() -> str:
    """`<run>-<attempt>`: a re-run attempt gets its own `train/` branch, the record keeps the run."""
    run = os.environ.get("GITHUB_RUN_ID", "local")
    return f"{run}-{os.environ['GITHUB_RUN_ATTEMPT']}" if os.environ.get("GITHUB_RUN_ATTEMPT", "1") != "1" else run


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ci-merge-train", description=__doc__.split("\n", 1)[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--base", default="main")
    ap.add_argument("--required-checks", default=os.environ.get("CI_KIT_REQUIRED_CHECKS", ""),
                    help="comma-separated required check names (default env CI_KIT_REQUIRED_CHECKS; '' = every check)")
    ap.add_argument("--max-batch", type=int, default=8)
    ap.add_argument("--min-batch", type=int, default=2)
    ap.add_argument("--stale-minutes", type=float, default=90)
    ap.add_argument("--review-workflow", default=os.environ.get("CI_KIT_REVIEW_WORKFLOW", "review-gate.yml"),
                    help="the caller's review-gate workflow file ('' = no review pre-check; default env"
                         " CI_KIT_REVIEW_WORKFLOW, else review-gate.yml)")
    ap.add_argument("--train-workflow", default=_own_workflow(),
                    help="this caller's merge-train workflow file, which a train's composition record must"
                         " name (default: the running workflow, GITHUB_WORKFLOW_REF, else merge-train.yml)")
    ap.add_argument("--run-id", default=_run_id())
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.min_batch < 2:
        ap.error("--min-batch must be >= 2 (one PR is what auto-merge is for)")
    o = Opts(repo=a.repo, base=a.base, required=tuple(s.strip() for s in a.required_checks.split(",") if s.strip()),
             max_batch=a.max_batch, min_batch=a.min_batch, stale_minutes=a.stale_minutes,
             review_workflow=a.review_workflow, train_workflow=a.train_workflow, run_id=a.run_id,
             dry_run=a.dry_run)
    p = Pass()
    try:
        rc = run(o, p)
    except (subprocess.CalledProcessError, ValueError, KeyError, OSError) as e:
        err = (getattr(e, "stderr", "") or "").strip()
        p.say(f"DID NOT FINISH: {e}" + (f" -- said: {err[-500:]}" if err else ""))
        rc = 2
    text = "\n".join(f"- {s}" for s in p.summary)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write("### merge train\n\n" + text + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())
