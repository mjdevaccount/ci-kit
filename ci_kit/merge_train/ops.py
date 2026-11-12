"""The merge train's options, its pass record, and every GitHub WRITE it makes (each a PAT call; a
failure is named in the pass, never raised past it) plus the git fetches over its workspace."""
from __future__ import annotations

import datetime as dt
import json
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from ci_kit.merge_train import COMMENT_MARK, LABELS, TRAIN_PREFIX, github
from ci_kit.merge_train import git as tgit
from ci_kit.pr_autoupdate import add_label


@dataclass
class Opts:
    repo: str
    base: str = "main"
    required: tuple[str, ...] = ()
    max_batch: int = 8
    min_batch: int = 2
    stale_minutes: float = 90
    review_workflow: str = "review-gate.yml"
    train_workflow: str = "merge-train.yml"
    run_id: str = "local"
    dry_run: bool = False

    @property
    def run(self) -> str:
        """The workflow run id (`run_id` is `<run>-<attempt>`)."""
        return self.run_id.split("-", 1)[0]


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


def age_min(ts: str | None) -> float:
    if not ts:
        return 0.0
    return (now() - dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))).total_seconds() / 60


def gh_ok(p: Pass, what: str, *args: str, input_text: str | None = None) -> bool:
    r = github.gh(*args, check=False, input_text=input_text)
    if r.returncode:
        p.error(f"{what}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.returncode == 0


def comment(o: Opts, p: Pass, n: int, text: str) -> bool:
    if o.dry_run:
        p.say(f"(dry run) would comment on #{n}: {text.splitlines()[0][:120]}")
        return True
    return gh_ok(p, f"comment on #{n}", "pr", "comment", str(n), "-R", o.repo, "--body-file", "-",
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


def close(o: Opts, p: Pass, n: int, delete_branch: bool = False) -> bool:
    if o.dry_run:
        p.say(f"(dry run) would close #{n}")
        return True
    return gh_ok(p, f"close #{n}", "pr", "close", str(n), "-R", o.repo,
                 *(["--delete-branch"] if delete_branch else []))


def cancel_runs(o: Opts, p: Pass, branch: str) -> list[int]:
    """Cancel every unfinished workflow run whose head branch is train branch `branch`; returns the
    ids cancelled. WHY (trade-data 2026-11-12): train #2998 was closed (recomposed as #2999) and its
    CI run kept the ONE heavy runner for ~25 min while real PRs queued. Closing a PR and deleting its
    branch cancels nothing. Only a `train/` branch of THIS repository (never a fork's same-named
    one), never this pass's own run nor any run of the
    merge-train workflow (a queued pass must not be dropped). A cancel GitHub refuses is an ERROR
    (the run goes red): a run left burning a slot is exactly what this exists to prevent. A run
    that finished between the list and the cancel (409) is not a failure."""
    if not branch.startswith(TRAIN_PREFIX):
        p.error(f"refused to cancel runs on `{branch}`: not a `{TRAIN_PREFIX}` branch")
        return []
    if o.dry_run:
        p.say(f"(dry run) would cancel unfinished runs on {branch}")
        return []
    q = urllib.parse.urlencode({"branch": branch, "per_page": 100})
    r = github.gh("api", f"repos/{o.repo}/actions/runs?{q}", "--jq",
                  "[.workflow_runs[] | {id, status, head_branch, path, repo: .head_repository.full_name}]", check=False)
    if r.returncode:
        p.error(f"list the runs on {branch} to cancel them: {(r.stderr or r.stdout).strip()[:300]}")
        return []
    own = o.train_workflow.rsplit("/", 1)[-1]
    done: list[int] = []
    for run in json.loads(r.stdout or "[]") or []:
        rid = int(run.get("id") or 0)
        if (run.get("head_branch") != branch or run.get("repo") != o.repo  # a fork's same-named branch
                or run.get("status") == "completed" or str(rid) == o.run
                or str(run.get("path") or "").split("@")[0].rsplit("/", 1)[-1] == own):
            continue
        c = github.gh("api", "-X", "POST", f"repos/{o.repo}/actions/runs/{rid}/cancel", check=False)
        said = (c.stderr or c.stdout).strip()
        if c.returncode and "HTTP 409" not in said:
            p.error(f"cancel run {rid} on closed train branch {branch} (it keeps its runner): {said[:300]}")
            continue
        if not c.returncode:
            done.append(rid)
    p.say(f"{branch}: cancelled {len(done)} unfinished run(s)" + (f": {', '.join(map(str, done))}" if done else ""))
    return done


def close_train(o: Opts, p: Pass, train: dict) -> bool:
    """THE one way a train PR is closed: close it, delete its branch, then cancel its unfinished runs
    (cancel_runs). A close that failed leaves the train open, so its runs are left alone too."""
    m, branch = int(train["number"]), str(train.get("headRefName") or "")
    if not close(o, p, m, delete_branch=True):
        return False
    cancel_runs(o, p, branch)
    return True


def comments_of(repo: str, n: int) -> list[str]:
    got = github.gh_json("api", f"repos/{repo}/issues/{n}/comments?per_page=100", "--jq", "[.[].body]")
    return [str(b) for b in got] if isinstance(got, list) else []


def fetch(d: Path, *refs: str) -> None:
    tgit.git(d, "fetch", "-q", "--no-tags", "origin", *refs)


def fetch_head(d: Path, number: int, sha: str) -> None:
    if not tgit.has_commit(d, sha):
        fetch(d, f"+refs/pull/{number}/head:refs/remotes/pull/{number}")
    if not tgit.has_commit(d, sha):
        fetch(d, sha)


def me() -> tgit.Identity:
    """The train token's user: the committer of every train commit, the author of every train PR."""
    got = github.gh_json("api", "user")
    if isinstance(got, dict) and got.get("login") and got.get("id"):
        return tgit.Identity(str(got["login"]), f"{got['id']}+{got['login']}@users.noreply.github.com")
    raise ValueError("gh api user: no login")
