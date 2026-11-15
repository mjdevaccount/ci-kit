"""The merge train's options, its pass record, and every GitHub WRITE it makes (each a PAT call; a
failure is named in the pass, never raised past it) plus the git fetches over its workspace."""
from __future__ import annotations

import datetime as dt
import json
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from ci_kit import pr_token
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


def items(paged: str) -> list[object]:
    """`gh api --paginate --jq '[...]'` prints one JSON array PER PAGE, back to back: their items. Pure."""
    out: list[object] = []
    dec, i, text = json.JSONDecoder(), 0, paged.strip()
    while i < len(text):
        got, i = dec.raw_decode(text, i)
        out += list(got) if isinstance(got, list) else []
        while i < len(text) and text[i].isspace():
            i += 1
    return out


def bodies(paged: str) -> list[str]:
    """`gh api --paginate --jq '[.[].body]'` prints one JSON array PER PAGE, back to back. Pure."""
    return [str(b) for b in items(paged)]


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


def _status(o: Opts, sha: str, state: str, desc: str) -> str:
    """Post `queue/position` on `sha`; '' or what GitHub said."""
    from ci_kit.pr_hold import CONTEXT  # lazy: pr_hold -> merge_train.policy
    r = github.gh("api", "-X", "POST", f"repos/{o.repo}/statuses/{sha}", "-f", f"state={state}",
                  "-f", f"context={CONTEXT}", "-f", f"description={desc[:140]}", check=False)
    return (r.stderr or r.stdout or "").strip()[:200] if r.returncode else ""


def take_lane(o: Opts, p: Pass, prs: list[dict], train: str) -> list[tuple[dict, str, str]]:
    """THE TRAIN TAKES THE LANE (v1.35.0): every PR holding `queue/position` (pr_hold.lane: the head's
    success, or a hold for an older train) goes pending, held for `train`, BEFORE the train opens --
    so the queue head cannot land between composing and pr-autoupdate's next pass (measured: 3.4 min).
    Returns what was taken, for give_back. A refused post is an ERROR (a red run): that head may land
    under the train, the exact waste this exists to stop -- but the train still opens, no worse than
    before."""
    from ci_kit.pr_hold import held_for, lane
    taken = lane(prs, o.base)
    for pr, _, _ in taken:
        n, sha = int(pr["number"]), str(pr.get("headRefOid") or "")
        if o.dry_run:
            p.say(f"(dry run) would hold #{n} for the train")
            continue
        err = _status(o, sha, "pending", held_for(train, o.stale_minutes))
        if err:
            p.error(f"#{n}: could not hold the queue head for the train ({err}): it may land under it")
        else:
            p.say(f"#{n}: queue head held for merge train {train}")
    return taken


def queue_now(o: Opts, prs: list[dict]) -> dict[int, tuple[str, str]]:
    """What `queue/position` each PR should read with NO train open: pr_hold.desired over `prs`,
    skipping pr_queue.blocked (the same head pr-autoupdate's hold derives, minus the parked /
    machinery sets only it reads -- a PR it would also skip simply waits for its next pass)."""
    from ci_kit import pr_queue
    from ci_kit.pr_hold import desired
    return desired(prs, o.base, pr_queue.blocked(prs, o.required, base=o.base))


def _restore(o: Opts, p: Pass, n: int, sha: str, state: str, desc: str, what: str) -> None:
    err = _status(o, sha, state, desc)
    if err:
        p.error(f"#{n}: could not {what} ({err}); pr-autoupdate's next pass does")
    else:
        p.say(f"#{n}: {what} ({state}): {desc[:80]}")


def give_back(o: Opts, p: Pass, taken: list[tuple[dict, str, str]], why: str, prs_now: list[dict] | None) -> None:
    """Undo take_lane against the queue AS IT IS NOW (`prs_now`, re-read by the caller): a PR that
    held success gets it back only if it is STILL the head -- a lower-numbered PR armed meanwhile
    is the head now, and restoring the snapshot would let two PRs hold the lane. A non-head gets
    the pending pr_hold would post. A pending hold for an older train is restored as it was.
    `prs_now` None = the re-read failed (the caller named it): nothing is restored, the head stays
    held until pr-autoupdate's next pass re-derives it. A refused post is an ERROR."""
    if o.dry_run:
        return
    if prs_now is None:
        p.error(f"lane NOT given back ({why}): the open PRs could not be re-read; pr-autoupdate's next pass re-derives it")
        return
    want = queue_now(o, prs_now)
    for pr, state, desc in taken:
        n, sha = int(pr["number"]), str(pr.get("headRefOid") or "")
        if state == "success" and want.get(n, ("", ""))[0] != "success":
            if n not in want:
                p.say(f"#{n}: no longer open into {o.base}: nothing to give back ({why})")
                continue
            state, desc = want[n]
            _restore(o, p, n, sha, state, desc, f"lane NOT given back, no longer the head ({why})")
            continue
        _restore(o, p, n, sha, state, desc, f"lane given back: {why}")


def release_lane(o: Opts, p: Pass, prs: list[dict]) -> None:
    """No train is open after this pass: a PR still held for one (pending, pr_hold.TRAIN_HELD) gets
    what pr_hold would post NOW, not on pr-autoupdate's next pass (a closed train starts none):
    success only if it is the queue head (queue_now), else its pending. The held PR was the head
    when the lane was taken; a lower PR armed since is the head now, and giving the held one
    success would put the lane on a non-head. A PR that holds no train hold is not touched (the
    head that never held it gets success from pr-autoupdate's hold). A refused post is an ERROR."""
    from ci_kit.pr_hold import lane
    want = queue_now(o, prs)
    for pr, state, _ in lane(prs, o.base):
        n = int(pr["number"])
        if state != "pending" or n not in want:
            continue
        if o.dry_run:
            p.say(f"(dry run) would release #{n} from its train hold")
            continue
        new, desc = want[n]
        what = ("lane released, no train open -- the queue head lands on its own" if new == "success"
                else "train hold dropped, no train open -- not the queue head")
        _restore(o, p, n, str(pr.get("headRefOid") or ""), new, desc, what)


def comments_of(repo: str, n: int) -> list[str]:
    """EVERY comment body, oldest first. Paginated: the release/rerun markers counted from these
    are budgets, and a first-page-only read silently reset one past 100 comments. UNAUTHENTICATED:
    any commenter's marker is read. Fail-safe only where a forged marker can merely SUPPRESS an action
    (a rerun spent, a judgment already named); a marker that GRANTS an action reads `comments_by`."""
    return [body for _, body in comments_by(repo, n)]


def comments_by(repo: str, n: int) -> list[tuple[str, str]]:
    """(author login, body) for EVERY comment, oldest first: the one reader, with the author kept, so
    a caller can trust only its own token's markers (ci_kit/pr_baseretry.py, v1.36.0)."""
    got = github.gh("api", "--paginate", f"repos/{repo}/issues/{n}/comments?per_page=100",
                    "--jq", "[.[] | [(.user.login // \"\"), .body]]").stdout or ""
    return [(str(x[0]), str(x[1])) if isinstance(x, list) and len(x) == 2 else ("", str(x)) for x in items(got)]


def fetch(d: Path, *refs: str) -> None:
    tgit.git(d, "fetch", "-q", "--no-tags", "origin", *refs)


def fetch_head(d: Path, number: int, sha: str) -> None:
    if not tgit.has_commit(d, sha):
        fetch(d, f"+refs/pull/{number}/head:refs/remotes/pull/{number}")
    if not tgit.has_commit(d, sha):
        fetch(d, sha)


def me() -> tgit.Identity:
    """The train token's user: the committer of every train commit, the author of every train PR.
    Under ci-kit's token step (v1.37.0) the login is CI_KIT_TOKEN_LOGIN -- an App installation token
    gets 403 on `GET /user` -- and its id comes from the public `GET /users/<login>` (the App's bot
    user: `<id>+<slug>[bot]@users.noreply.github.com`, the email GitHub attributes to it). Without
    it, `GET /user` as before. Unreadable = raise: the pass never guesses who it is."""
    login = pr_token.acting_login()
    got = github.gh_json("api", f"users/{login}") if login else github.gh_json("api", "user")
    if isinstance(got, dict) and got.get("login") and got.get("id"):
        if login and str(got["login"]) != login:
            raise ValueError(f"gh api users/{login}: answered for {got['login']}")
        return tgit.Identity(str(got["login"]), f"{got['id']}+{got['login']}@users.noreply.github.com")
    raise ValueError(f"gh api {'users/' + login if login else 'user'}: no login")


def trusted(me: tgit.Identity) -> frozenset[str]:
    """Whose train PRs this pass may judge, land or close: its own identity plus the OTHER
    configured token identity (pr_token.trusted_logins), so a train composed before a PAT <-> App
    flip is finished, not orphaned as "a train this workflow did not compose"."""
    return pr_token.trusted_logins(me.name)
