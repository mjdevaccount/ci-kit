"""The merge train's git: compose a train, verify one, map landed commits back. Real git, no network.

COMPOSE. From main's tip (the ROOT), for each candidate oldest first: three-way merge the PR's
head onto the train so far, with the PR's own merge base on main as the base
(`git merge-tree --write-tree --merge-base`, git >= 2.40), and commit the result as ONE commit
-- title + " (#N)", the PR body, its Co-Authored-By trailers, then the binding trailers. A
conflict, or a PR whose change is already on the train, is skipped and the train goes on. That is
exactly the commit squash-merge would have made had the PRs merged one after another.

VERIFY (the review gate's train path). Every commit between the root and the train head must be
single-parent, in one chain, carry exactly one PR + head trailer, name each PR once, and its tree
must be BYTE-EQUAL to that same merge recomputed here from the named head. The tree is the only
thing that lands, so a commit that passes carries the reviewed head's change and nothing else --
whatever its message or author claims. Anything else is a problem the gate names and fails on.

THE REVIEWED DIFF IS THE LANDED DIFF (session review of ci-kit#70, findings 1-2). Two more things
must hold for a commit to count, at compose (the rider is SKIPPED, back to its own lane) and again
at verify (a problem):
  * BASE-BOUND: the review ran against a base whose merge base with the head is the train root's
    merge base with the head (review_base_ok). A PR reviewed on top of an unreviewed branch and
    retargeted to main would otherwise land that branch's content with no review.
  * CONTEXT KEPT: the commit's own diff (from the commit before it), normalised the way the review
    gate's carry hashes it -- context lines KEPT -- equals the diff the review read (merge base ->
    head). If main or an earlier rider changed the lines around a hunk, the reviewer never saw
    them: that is the semantic-conflict shape ci_kit/review/carry.py re-reviews, so the rider goes
    the normal update + carry path instead of riding.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ci_kit.merge_train import TRAILER_HEAD, TRAILER_PR
from ci_kit.review.carry import normalise

MAX_COMMITS = 50  # a train is <= max-batch commits; more is not a train
_SHA = re.compile(r"^[0-9a-f]{40}$")
_TRAILER = re.compile(r"^([A-Za-z0-9-]+):\s*(.*?)\s*$")
_COAUTHOR = re.compile(r"^co-authored-by:\s*(\S.*<[^<>\s]+@[^<>\s]+>)\s*$", re.I)


class Invalid(ValueError):
    """The train cannot be read as a train at all (structure); the gate fails on it."""


@dataclass(frozen=True)
class Rider:
    number: int
    head: str
    title: str = ""
    body: str = ""
    base: str = ""  # the base sha its passing review ran against (github.review_passed)


@dataclass(frozen=True)
class Identity:
    name: str
    email: str


def git(cwd: Path | str, *args: str, input_text: str | None = None, env: dict[str, str] | None = None,
        check: bool = True) -> subprocess.CompletedProcess[str]:
    # -c core.hooksPath: no hook in any fetched content or global config runs in a train job
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, check=check,
                          capture_output=True, text=True, encoding="utf-8", errors="surrogateescape",
                          input=input_text, env=env)


def out(cwd: Path | str, *args: str) -> str:
    return git(cwd, *args).stdout.strip()


def has_commit(cwd: Path | str, sha: str) -> bool:
    return git(cwd, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0


def apply_onto(cwd: Path | str, onto: str, head: str, root: str) -> str | None:
    """The tree of `head`'s own change (since its merge base with `root`) applied onto `onto`; None
    on a conflict. Raises on anything else (an old git, a missing object): never a silent skip."""
    mb = out(cwd, "merge-base", root, head)
    r = git(cwd, "merge-tree", "--write-tree", "--no-messages", "--merge-base", mb, onto, head, check=False)
    if r.returncode == 1:
        return None
    if r.returncode:
        raise subprocess.CalledProcessError(r.returncode, "git merge-tree", r.stdout, r.stderr)
    return r.stdout.split("\n", 1)[0].strip()


def own_diff(cwd: Path | str, a: str, b: str) -> str:
    """Hash of `git diff a b` normalised like the review gate's carry (blob ids and hunk line
    numbers dropped, CONTEXT KEPT); a and b may be commits or trees."""
    raw = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "diff", "--no-color", "--no-ext-diff",
                          "--binary", "-U3", a, b], cwd=cwd, check=True, capture_output=True).stdout
    return hashlib.sha256(normalise(raw)).hexdigest() if raw.strip() else "empty"


def ensure_commit(cwd: Path | str, sha: str) -> bool:
    """Have `sha` locally, fetching it by id if needed (GitHub serves any reachable commit)."""
    if not has_commit(cwd, sha):
        git(cwd, "fetch", "-q", "--no-tags", "origin", sha, check=False)
    return has_commit(cwd, sha)


def review_base_ok(cwd: Path | str, root: str, head: str, reviewed_base: str) -> str:
    """'' when the review that passed at `head` read the same diff the train lands on `root`: the
    merge base of the reviewed base and the head equals the root's. Else why not."""
    if not reviewed_base:
        return "its passing review names no base"
    if not ensure_commit(cwd, reviewed_base):
        return f"the reviewed base {reviewed_base[:12]} could not be fetched"
    seen = out(cwd, "merge-base", reviewed_base, head)
    lands = out(cwd, "merge-base", root, head)
    if seen != lands:
        return (f"reviewed on {reviewed_base[:12]} (merge base {seen[:12]}), but on main its change is "
                f"since {lands[:12]}: the train would land content the review never read")
    return ""


def context_kept(cwd: Path | str, onto: str, tree: str, root: str, head: str) -> bool:
    """The rider's diff as it lands (onto -> tree) is byte-for-byte the diff its review read."""
    return own_diff(cwd, onto, tree) == own_diff(cwd, out(cwd, "merge-base", root, head), head)


def message(cwd: Path | str, sha: str) -> str:
    return git(cwd, "log", "-1", "--format=%B", sha).stdout


def trailers(msg: str) -> list[tuple[str, str]]:
    """(key, value) of every `Key: value` line of the message's LAST paragraph, when that paragraph
    is all trailers -- else []. Parsed here, not by `%(trailers)` (see the package docstring)."""
    paras = [p for p in re.split(r"\n[ \t]*\n", msg.replace("\r\n", "\n").strip()) if p.strip()]
    if not paras:
        return []
    found = [_TRAILER.match(line) for line in paras[-1].splitlines() if line.strip()]
    if not found or not all(found):
        return []
    return [(m.group(1), m.group(2)) for m in found if m]


def binding(msg: str) -> tuple[int, str] | str:
    """(pr, head) from the trailers, or why not."""
    ts = trailers(msg)
    prs = [v for k, v in ts if k.lower() == TRAILER_PR.lower()]
    heads = [v for k, v in ts if k.lower() == TRAILER_HEAD.lower()]
    if len(prs) != 1 or len(heads) != 1:
        return f"needs exactly one {TRAILER_PR} and one {TRAILER_HEAD} trailer (has {len(prs)} and {len(heads)})"
    if not prs[0].isdigit() or not _SHA.match(heads[0]):
        return f"malformed trailers ({TRAILER_PR}: {prs[0][:20]!r}, {TRAILER_HEAD}: {heads[0][:50]!r})"
    return int(prs[0]), heads[0]


def co_authors(cwd: Path | str, root: str, head: str) -> list[str]:
    """`Co-Authored-By:` lines from the PR's own commits, deduplicated, in first-seen order."""
    mb = out(cwd, "merge-base", root, head)
    text = git(cwd, "log", "--no-merges", "--reverse", "--format=%B", f"{mb}..{head}").stdout
    seen: dict[str, str] = {}
    for line in text.splitlines():
        m = _COAUTHOR.match(line.strip())
        if m:
            seen.setdefault(m.group(1).lower(), m.group(1))
    return [f"Co-Authored-By: {v}" for v in seen.values()]


def squash_message(r: Rider, coauthors: list[str]) -> str:
    title = " ".join(r.title.split()) or f"PR #{r.number}"
    body = "\n".join(line.rstrip() for line in r.body.replace("\r\n", "\n").strip().splitlines())
    tail = [*coauthors, f"{TRAILER_PR}: {r.number}", f"{TRAILER_HEAD}: {r.head}"]
    return f"{title} (#{r.number})\n\n" + (f"{body}\n\n" if body else "") + "\n".join(tail) + "\n"


def _commit(cwd: Path | str, tree: str, parent: str, msg: str, author: Identity, committer: Identity) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": author.name, "GIT_AUTHOR_EMAIL": author.email,
           "GIT_COMMITTER_NAME": committer.name, "GIT_COMMITTER_EMAIL": committer.email}
    return git(cwd, "commit-tree", tree, "-p", parent, "-F", "-", input_text=msg, env=env).stdout.strip()


def author_of(cwd: Path | str, sha: str) -> Identity:
    name, email = out(cwd, "log", "-1", "--format=%an%x1f%ae", sha).split("\x1f", 1)
    return Identity(name or "unknown", email or "unknown@users.noreply.github.com")


def compose(cwd: Path | str, root: str, riders: list[Rider], committer: Identity
            ) -> tuple[str, list[tuple[Rider, str]], list[tuple[Rider, str]]]:
    """(tip, [(rider, train commit)], [(rider, why skipped)]). Each rider's head must be fetched."""
    tip, included, skipped = root, [], []
    for r in riders:
        if not has_commit(cwd, r.head):
            skipped.append((r, "its head is not fetched"))
            continue
        tree = apply_onto(cwd, tip, r.head, root)
        if tree is None:
            skipped.append((r, "conflicts with the PRs ahead of it in this train"))
            continue
        if tree == out(cwd, "rev-parse", f"{tip}^{{tree}}"):
            skipped.append((r, "its change is already on main (an empty squash)"))
            continue
        if not context_kept(cwd, tip, tree, root, r.head):
            skipped.append((r, "the lines around its change moved since its review (main or a PR ahead of it "
                               "in this train): it needs its own update and review, not a train"))
            continue
        msg = squash_message(r, co_authors(cwd, root, r.head))
        tip = _commit(cwd, tree, tip, msg, author_of(cwd, r.head), committer)
        included.append((r, tip))
    return tip, included, skipped


def riders_of(cwd: Path | str, root: str, head: str) -> list[tuple[int, str, str]]:
    """[(pr, head, train commit)] of a train we composed, oldest first; unbound commits skipped."""
    rows = out(cwd, "rev-list", "--reverse", f"{root}..{head}").split()
    got = []
    for sha in rows:
        b = binding(message(cwd, sha))
        if isinstance(b, tuple):
            got.append((b[0], b[1], sha))
    return got


def verify(cwd: Path | str, base: str, head: str, ensure: Callable[[int, str], None],
           reviewed: Callable[[int, str], tuple]) -> tuple[list[tuple[int, str, str]], list[str]]:
    """([(pr, head, train commit)], problems). An empty problem list = every commit is its PR's
    reviewed head and nothing else. `ensure(pr, head)` fetches a head (raises if it cannot);
    `reviewed(pr, head)` = (passed, why, reviewed base sha) from the caller's review gate at that
    exact head (github.review_passed); a verdict naming no base is not a pass.
    Raises Invalid when the branch is not a chain of commits on main at all."""
    root = out(cwd, "merge-base", base, head)
    rows = [line.split() for line in out(cwd, "rev-list", "--reverse", "--parents", f"{root}..{head}").splitlines()]
    if not rows:
        raise Invalid("the train has no commits on top of main")
    if len(rows) > MAX_COMMITS:
        raise Invalid(f"{len(rows)} commits: more than a train ({MAX_COMMITS})")
    constituents: list[tuple[int, str, str]] = []
    problems: list[str] = []
    prev, seen = root, set()
    for sha, *parents in rows:
        if parents != [prev]:
            raise Invalid(f"{sha[:12]} is not a single-parent commit on {prev[:12]} (a merge, or off the chain)")
        b = binding(message(cwd, sha))
        if isinstance(b, str):
            problems.append(f"{sha[:12]}: {b}")
            prev = sha
            continue
        pr, h = b
        if pr in seen:
            problems.append(f"{sha[:12]}: #{pr} appears twice")
        seen.add(pr)
        try:
            ensure(pr, h)
        except (subprocess.CalledProcessError, OSError, ValueError) as e:
            problems.append(f"{sha[:12]}: #{pr} head {h[:12]} could not be fetched ({str(e)[:120]})")
            prev = sha
            continue
        tree = apply_onto(cwd, prev, h, root)
        if tree is None:
            problems.append(f"{sha[:12]}: #{pr} at {h[:12]} does not apply cleanly onto {prev[:12]}")
        elif tree != out(cwd, "rev-parse", f"{sha}^{{tree}}"):
            problems.append(f"{sha[:12]}: its content is NOT #{pr} at {h[:12]} applied onto {prev[:12]}")
        elif not context_kept(cwd, prev, sha, root, h):
            problems.append(f"{sha[:12]}: #{pr} at {h[:12]} lands with different context than its review read")
        else:
            got = tuple(reviewed(pr, h)) + ("", "", "")
            ok, why, rbase = bool(got[0]), str(got[1]), str(got[2])
            bad = review_base_ok(cwd, root, h, rbase) if ok else why
            if not bad:
                constituents.append((pr, h, sha))
            else:
                problems.append(f"{sha[:12]}: #{pr} at {h[:12]}: {bad}")
        prev = sha
    return constituents, problems


def landed(cwd: Path | str, tip: str, wanted: set[tuple[int, str]], depth: int = 200) -> dict[int, str]:
    """{pr: the main commit carrying its (pr, head) binding}, newest first from `tip`. GitHub's
    rebase merge rewrites every commit (new committer, new sha) but keeps the message."""
    found: dict[int, str] = {}
    text = git(cwd, "log", "--first-parent", f"-n{depth}", "--format=%H%x1f%B%x1e", tip).stdout
    for rec in text.split("\x1e"):
        if "\x1f" not in rec:
            continue
        sha, msg = rec.strip().split("\x1f", 1)
        b = binding(msg)
        if isinstance(b, tuple) and b in wanted and b[0] not in found:
            found[b[0]] = sha
        if len(found) == len(wanted):
            break
    return found
