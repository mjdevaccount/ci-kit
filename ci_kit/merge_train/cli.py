#!/usr/bin/env python3
"""ci-merge-train: one pass of the merge train -- finalize, judge the open train, compose the next.

ONE PASS, IN ORDER (the caller runs it on a cron, on dispatch, on a train PR closing, on a check
workflow finishing on a train branch, and when a PR gets the `merge-train` label):
  1. FINALIZE every train PR merged and not yet finalized: each constituent whose head is still
     the one that rode is commented "landed as <sha> via train #M", labelled `landed-via-train`
     and closed; one whose head moved since is told so and left open with the rest. The train PR
     gets `landed-via-train` last, so a crash part-way is finished by the next pass.
  2. JUDGE the open train (policy.phase): pending -> stop here; green -> merge it (rebase);
     red -> close it and bisect (policy.split), commenting the failing checks on every rider;
     stale (main moved) / stuck (pending past --stale-minutes) -> close it, release the riders.
  3. RELEASE strays: an `in-train` label on a PR no open train carries (pr_autoupdate skips it).
  4. COMPOSE when >= --min-batch candidates (policy.candidates) remain, oldest-armed first, at
     most --max-batch: each one's head fetched and its review verdict re-read at that head
     (github.review_passed), the train composed (git.compose), pushed to `train/<run>`, opened as
     a PR labelled `merge-train` with a constituent table, armed (`--rebase --auto`), and every
     rider labelled `in-train` and commented. A conflicting PR is commented once per head and
     skipped; with one candidate nothing runs -- that PR's own auto-merge lands it.

A TRAIN NEVER force-pushes main and never bypasses protection: it lands only through GitHub's
own merge of the train PR, whose required checks (the review gate's train path among them,
ci_kit/review/train.py) ran on exactly the tree that lands. A rebase merge makes one main commit
per PR with the PR's own title, body and trailers; the tree of the last one is the train's tested
tree, so a caller's "tested-tree" reuse (trade-data ci.yml) re-tests nothing on that push.

THE TOKEN is the caller's fine-grained PAT (`GH_TOKEN`: contents, pull requests and workflows
RW, actions read) -- a push or PR made with GITHUB_TOKEN fires no workflow, so the train's checks
would never start. Check runs are read with `CI_KIT_READ_TOKEN` (GITHUB_TOKEN), as pr_autoupdate
does: GitHub refuses a fine-grained PAT the check runs.

TELEMETRY. Each pass writes its summary (step summary + log): the workflow run is the
append-only record, and every train is ALSO a PR whose body and comments say what rode, what
was skipped and why, and how it ended. What watches from outside: mcp scripts/pr_reconcile.py
(hourly) names a train PR open > 1h, a constituent closed as landed whose commit is not on main,
and an `in-train` label with no open train, in the brief's PR-LAND line.

Exit 0 = the pass finished; 1 = it finished but something it tried failed (named in the log);
2 = could not finish looking (gh or git could not answer).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ci_kit import pr_list
from ci_kit.merge_train import (
    COMMENT_MARK,
    LABELS,
    LANDED,
    RIDING,
    TRAIN_LABEL,
    TRAIN_PREFIX,
    github,
    policy,
)
from ci_kit.merge_train import git as tgit
from ci_kit.pr_autoupdate import _gh_read, add_label


@dataclass
class Opts:
    repo: str
    base: str = "main"
    required: tuple[str, ...] = ()
    max_batch: int = 8
    min_batch: int = 2
    stale_minutes: float = 90
    review_workflow: str = "review-gate.yml"
    run_id: str = "local"
    dry_run: bool = False


@dataclass
class Pass:
    summary: list[str] = field(default_factory=list)
    errors: int = 0

    def say(self, line: str) -> None:
        self.summary.append(line)

    def error(self, line: str) -> None:
        self.errors += 1
        print(f"::error::{line}")
        self.summary.append(f"ERROR: {line}")


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _age_min(ts: str | None) -> float:
    if not ts:
        return 0.0
    return (now() - dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))).total_seconds() / 60


# ── GitHub writes (each one a PAT call; a failure is named, never raised past the pass) ──────────
def _gh_ok(p: Pass, what: str, *args: str, input_text: str | None = None) -> bool:
    r = github.gh(*args, check=False, input_text=input_text)
    if r.returncode:
        p.error(f"{what}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.returncode == 0


def comment(o: Opts, p: Pass, n: int, text: str) -> None:
    if o.dry_run:
        p.say(f"(dry run) would comment on #{n}: {text.splitlines()[0][:120]}")
        return
    _gh_ok(p, f"comment on #{n}", "pr", "comment", str(n), "-R", o.repo, "--body-file", "-",
           input_text=f"{COMMENT_MARK}\n{text}\n")


def label(o: Opts, p: Pass, n: int, add: str = "", remove: str = "") -> None:
    if o.dry_run:
        return
    if add:
        color, desc = LABELS.get(add, ("ededed", "merge-train"))
        if not add_label(o.repo, n, add, color=color, description=desc)[0]:
            p.error(f"#{n}: could not add label `{add}`")
    if remove:
        github.gh("pr", "edit", str(n), "-R", o.repo, "--remove-label", remove, check=False)


def close(o: Opts, p: Pass, n: int, delete_branch: bool = False) -> None:
    if o.dry_run:
        p.say(f"(dry run) would close #{n}")
        return
    _gh_ok(p, f"close #{n}", "pr", "close", str(n), "-R", o.repo, *(["--delete-branch"] if delete_branch else []))


def comments_of(repo: str, n: int) -> list[str]:
    got = github.gh_json("api", f"repos/{repo}/issues/{n}/comments?per_page=100", "--jq", "[.[].body]")
    return [str(b) for b in got] if isinstance(got, list) else []


# ── git workspace ──────────────────────────────────────────────────────────────────────────────
def workspace(repo: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix="merge-train-"))
    tgit.git(d, "init", "-q")
    tgit.git(d, "remote", "add", "origin", f"https://github.com/{repo}.git")
    # the PAT reaches git through gh's own credential helper (GH_TOKEN): never argv, never a URL
    tgit.git(d, "config", "credential.https://github.com.helper", "")
    tgit.git(d, "config", "--add", "credential.https://github.com.helper", "!gh auth git-credential")
    return d


def fetch(d: Path, *refs: str) -> None:
    tgit.git(d, "fetch", "-q", "--no-tags", "origin", *refs)


def fetch_head(d: Path, number: int, sha: str) -> None:
    if not tgit.has_commit(d, sha):
        fetch(d, f"+refs/pull/{number}/head:refs/remotes/pull/{number}")
    if not tgit.has_commit(d, sha):
        fetch(d, sha)


# ── 1. finalize ────────────────────────────────────────────────────────────────────────────────
def finalize(o: Opts, p: Pass, d: Path) -> None:
    merged = github.gh_json("pr", "list", "-R", o.repo, "--state", "merged", "--label", TRAIN_LABEL,
                            "--limit", "20", "--json", "number,mergeCommit,labels,mergedAt")
    for t in merged if isinstance(merged, list) else []:
        if LANDED in {lb.get("name") for lb in t.get("labels") or []} or _age_min(t.get("mergedAt")) > 7 * 24 * 60:
            continue
        m = int(t["number"])
        tip = (t.get("mergeCommit") or {}).get("oid") or ""
        msgs = github.gh_json("api", f"repos/{o.repo}/pulls/{m}/commits?per_page=100",
                              "--jq", "[.[].commit.message]")
        rode = [b for b in (tgit.binding(str(x)) for x in (msgs or [])) if isinstance(b, tuple)]
        fetch(d, f"+refs/heads/{o.base}:refs/remotes/origin/{o.base}")
        where = tgit.landed(d, tip, set(rode)) if tip and tgit.has_commit(d, tip) else {}
        ok = True
        for n, h in rode:
            sha = where.get(n) or tip  # a train merged by squash: every rider landed in its one commit
            pr = github.pull(o.repo, n)
            cur = (pr.get("head") or {}).get("sha")
            if pr.get("state") != "open":
                continue
            if cur == h:
                comment(o, p, n, f"Landed as {sha} via merge train #{m} (this PR's head {h}).\n"
                                 f"<!-- merge-train:landed train={m} head={h} sha={sha} -->")
                label(o, p, n, add=LANDED, remove=RIDING)
                close(o, p, n)
                p.say(f"#{n}: landed as {sha[:12]} via train #{m}, closed")
            else:
                comment(o, p, n, f"Merge train #{m} landed this PR at {h} as {sha}. The head has moved since "
                                 f"(now {cur}): those later commits are NOT on {o.base}, so this PR stays open "
                                 "with them.")
                label(o, p, n, remove=RIDING)
                p.say(f"#{n}: train #{m} landed {h[:12]}; head moved to {str(cur)[:12]}, left open")
        if not rode:
            ok = False
            p.error(f"train #{m} merged but none of its commits carry a binding: nothing was closed")
        if ok and not o.dry_run:
            label(o, p, m, add=LANDED)
            p.say(f"train #{m}: finalized")


# ── 2. judge the open train ────────────────────────────────────────────────────────────────────
def judge(o: Opts, p: Pass, d: Path, train: dict, main_sha: str) -> tuple[bool, set[int], frozenset[int]]:
    """(stop this pass?, PR numbers still riding, PR numbers ejected now)."""
    m, head = int(train["number"]), str(train["headRefOid"])
    fetch(d, f"+refs/pull/{m}/head:refs/remotes/train/{m}")
    root = tgit.out(d, "merge-base", main_sha, head)
    rode = tgit.riders_of(d, root, head)
    numbers = [n for n, _, _ in rode]
    ph, names = policy.phase(train, root, main_sha, _age_min(train.get("createdAt")), o.stale_minutes, o.required)
    p.say(f"train #{m} ({len(rode)} PRs: {', '.join('#' + str(n) for n in numbers)}): {ph}"
          + (f" -- {', '.join(names[:6])}" if names else ""))
    if ph == "pending":
        return True, set(numbers), frozenset()
    if ph == "green":
        if not o.dry_run:
            r = github.gh("pr", "merge", str(m), "-R", o.repo, "--rebase", "--match-head-commit", head, check=False)
            if r.returncode:
                # auto-merge (armed at open) may already be landing it; the next pass finalizes
                print(f"::warning::train #{m}: merge said {(r.stderr or r.stdout).strip()[:200]}")
        return True, set(numbers), frozenset()
    if ph == "red":
        keep, eject = policy.split(numbers)
        checks = ", ".join(names[:6])
        for n in numbers:
            if n in eject:
                comment(o, p, n, f"Ejected from merge train #{m}: the train went red on **{checks}** "
                                 f"({train.get('url')}). This PR now lands through its own auto-merge, "
                                 "tested alone against main.")
                label(o, p, n, add=policy.EJECTED, remove=RIDING)
            else:
                comment(o, p, n, f"Merge train #{m} went red on **{checks}**. Bisecting: this PR is in the "
                                 "older half and rides the next train without the newer half.")
                label(o, p, n, remove=RIDING)
        comment(o, p, m, f"Red on **{checks}**. Bisect: keep {keep or 'none'}, eject {eject} "
                         "(ejected PRs land on their own auto-merge).")
        close(o, p, m, delete_branch=True)
        p.say(f"train #{m}: red, closed; ejected {eject}, kept {keep}")
        return False, set(), frozenset(eject)
    # stale / stuck: no evidence against any PR -- release them all and recompose
    why = names[0] if ph == "stale" else f"checks never finished after {o.stale_minutes:.0f} min: {', '.join(names[:6])}"
    comment(o, p, m, f"Closed ({ph}): {why}. Its PRs are recomposed into a fresh train.")
    for n in numbers:
        label(o, p, n, remove=RIDING)
    close(o, p, m, delete_branch=True)
    if ph == "stuck":
        p.error(f"train #{m} was stuck ({why}); a check that never reports will stall every train")
    return False, set(), frozenset()


# ── 4. compose ─────────────────────────────────────────────────────────────────────────────────
def committer() -> tgit.Identity:
    me = github.gh_json("api", "user")
    if isinstance(me, dict) and me.get("login") and me.get("id"):
        return tgit.Identity(str(me["login"]), f"{me['id']}+{me['login']}@users.noreply.github.com")
    raise ValueError("gh api user: no login")


def compose(o: Opts, p: Pass, d: Path, cands: list[dict], main_sha: str) -> None:
    riders: list[tgit.Rider] = []
    for c in cands[: o.max_batch]:
        n, h = int(c["number"]), str(c["headRefOid"])
        fetch_head(d, n, h)
        pr = github.pull(o.repo, n)
        if (pr.get("head") or {}).get("sha") != h:
            p.say(f"#{n}: head moved while composing, left for the next pass")
            continue
        if o.review_workflow:
            ok, why = github.review_passed(o.repo, o.review_workflow, n, h, pr)
            if not ok:
                p.say(f"#{n}: not riding -- {why}")
                continue
        riders.append(tgit.Rider(n, h, str(pr.get("title") or ""), str(pr.get("body") or "")))
    if len(riders) < o.min_batch:
        p.say(f"{len(riders)} PR(s) ready to ride (< {o.min_batch}): no train; auto-merge lands a single PR")
        return
    tip, included, skipped = tgit.compose(d, main_sha, riders, committer())
    for r, why in skipped:
        p.say(f"#{r.number}: skipped -- {why}")
        mark = f"<!-- merge-train:skipped head={r.head} -->"
        if not o.dry_run and not any(mark in b for b in comments_of(o.repo, r.number)):
            comment(o, p, r.number, f"Not in this merge train: {why}. It lands through its own auto-merge, "
                                    f"or rides a later train.\n{mark}")
    if len(included) < o.min_batch:
        p.say(f"{len(included)} PR(s) composed cleanly (< {o.min_batch}): no train")
        return
    branch = f"{TRAIN_PREFIX}{o.run_id}"
    rows = "\n".join(f"| #{r.number} | {r.title[:80]} | `{r.head}` | `{sha[:12]}` | passed at head |"
                     for r, sha in included)
    body = (f"{COMMENT_MARK}\nMerge train: {len(included)} armed PRs tested ONCE on one tree and landed by "
            f"**rebase** merge, one commit per PR (ci-kit ci-merge-train, run {o.run_id}).\n\n"
            f"Root: `{main_sha}` ({o.base}).\n\n| PR | title | head that rode | train commit | review |\n"
            f"|---|---|---|---|---|\n{rows}\n\n"
            + ("Skipped:\n" + "\n".join(f"- #{r.number}: {why}" for r, why in skipped) + "\n\n" if skipped else "")
            + "The review gate does not re-review this diff: it verifies every commit is exactly its PR's "
              "reviewed head (ci-kit ci_kit/review/train.py). Do not push to this branch.\n")
    title = "merge train: " + " ".join(f"#{r.number}" for r, _ in included)
    if o.dry_run:
        p.say(f"(dry run) would open {branch} at {tip[:12]}: {title}")
        return
    tgit.git(d, "push", "-q", "origin", f"{tip}:refs/heads/{branch}")
    for name, (color, desc) in LABELS.items():
        github.gh("label", "create", name, "-R", o.repo, "--color", color, "--description", desc, check=False)
    r = github.gh("pr", "create", "-R", o.repo, "--base", o.base, "--head", branch, "--title", title,
                  "--body-file", "-", "--label", TRAIN_LABEL, check=False, input_text=body)
    if r.returncode:
        p.error(f"open the train PR for {branch}: {(r.stderr or r.stdout).strip()[:300]}")
        tgit.git(d, "push", "-q", "origin", f":refs/heads/{branch}", check=False)
        return
    url = r.stdout.strip().splitlines()[-1]
    m = int(url.rstrip("/").rsplit("/", 1)[-1])
    _gh_ok(p, f"arm train #{m} (rebase)", "pr", "merge", str(m), "-R", o.repo, "--rebase", "--auto",
           "--match-head-commit", tip)
    for rr, sha in included:
        label(o, p, rr.number, add=RIDING)
        comment(o, p, rr.number, f"Riding merge train #{m} ({url}) at head `{rr.head}` as `{sha[:12]}`. "
                                 "It lands as its own commit when the train is green; pr-autoupdate leaves it alone meanwhile.")
    p.say(f"train #{m} opened: {title} ({url})")


# ── the pass ───────────────────────────────────────────────────────────────────────────────────
def run(o: Opts, p: Pass) -> int:
    d = workspace(o.repo)
    fetch(d, f"+refs/heads/{o.base}:refs/remotes/origin/{o.base}")
    main_sha = tgit.out(d, "rev-parse", f"refs/remotes/origin/{o.base}")
    finalize(o, p, d)
    prs = pr_list.rows(json.loads(_gh_read(*pr_list.gh_args(o.repo)).stdout))
    trains = sorted((x for x in prs if policy.is_train(x)), key=lambda x: int(x["number"]))
    riding: set[int] = set()
    ejected: frozenset[int] = frozenset()
    for extra in trains[:-1]:  # one train at a time: an older open one is superseded
        comment(o, p, int(extra["number"]), "Closed: a newer train is open.")
        close(o, p, int(extra["number"]), delete_branch=True)
    if trains:
        stop, riding, ejected = judge(o, p, d, trains[-1], main_sha)
        if stop:
            for n in policy.stray_riders(prs, riding):
                label(o, p, n, remove=RIDING)
                p.say(f"#{n}: stray `{RIDING}` label removed")
            return 1 if p.errors else 0
    for n in policy.stray_riders(prs, riding):
        label(o, p, n, remove=RIDING)
    cands, no = policy.candidates(prs, o.required, o.base, ejected)
    p.say(f"{len(cands)} candidate(s) of {len(prs)} open PR(s)"
          + (f": {', '.join('#' + str(c['number']) for c in cands)}" if cands else ""))
    for x, why in no:
        if x.get("autoMergeRequest") and not policy.is_train(x):
            p.say(f"#{x['number']}: {why}")
    if len(cands) >= o.min_batch:
        compose(o, p, d, cands, main_sha)
    else:
        p.say(f"fewer than {o.min_batch} candidates: no train (a single PR lands on its own auto-merge)")
    return 1 if p.errors else 0


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
    ap.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", "local"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.min_batch < 2:
        ap.error("--min-batch must be >= 2 (one PR is what auto-merge is for)")
    o = Opts(repo=a.repo, base=a.base, required=tuple(s.strip() for s in a.required_checks.split(",") if s.strip()),
             max_batch=a.max_batch, min_batch=a.min_batch, stale_minutes=a.stale_minutes,
             review_workflow=a.review_workflow, run_id=a.run_id, dry_run=a.dry_run)
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
