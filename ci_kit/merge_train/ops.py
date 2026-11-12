"""The merge train's options, its pass record, and every GitHub WRITE it makes (each a PAT call; a
failure is named in the pass, never raised past it) plus the git fetches over its workspace."""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path

from ci_kit.merge_train import COMMENT_MARK, LABELS, github
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


def bodies(paged: str) -> list[str]:
    """`gh api --paginate --jq '[.[].body]'` prints one JSON array PER PAGE, back to back. Pure."""
    out: list[str] = []
    dec, i, text = json.JSONDecoder(), 0, paged.strip()
    while i < len(text):
        got, i = dec.raw_decode(text, i)
        out += [str(b) for b in got] if isinstance(got, list) else []
        while i < len(text) and text[i].isspace():
            i += 1
    return out


def comments_of(repo: str, n: int) -> list[str]:
    """EVERY comment body, oldest first. Paginated: the release/rerun markers counted from these
    are budgets, and a first-page-only read silently reset one past 100 comments."""
    return bodies(github.gh("api", "--paginate", f"repos/{repo}/issues/{n}/comments?per_page=100",
                            "--jq", "[.[].body]").stdout or "")


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
