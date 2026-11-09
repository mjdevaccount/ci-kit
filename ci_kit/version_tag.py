"""ci-version-tag: every version bump on the default branch gets its tag, and every tag its release.

WHY. In a VERSION-FIRST repo the release PR moves `version` in pyproject.toml, and someone then
pushes the `vX.Y.Z` tag that starts the release workflow. That push is a hand step nothing
checks: llm-ports 1.19.0 merged (2026-11-09) and was never tagged -- no wheel, no PyPI upload, no
GitHub release, nothing red. This turns the hand step into a check that also acts.

WHAT IT DOES, for every version the version file took on HEAD's first-parent history (the commit
that first set it is the BUMP commit; `.devN` and `+local` versions, and any below `--since`, are
skipped):

  no tag                 create `<prefix>X.Y.Z` at the bump commit (GitHub API), verify the ref,
                         then dispatch `--release-workflow` on the tag. A tag made with the job's
                         GITHUB_TOKEN starts no `push` workflow; `workflow_dispatch` is the one
                         event that token may start. So the release workflow must treat ANY run
                         on a tag ref (`github.ref_type == 'tag'`) as the release, whatever the
                         event -- and a dispatch runs the workflow file AS IT IS AT THE TAG, so a
                         tag cut before the workflow learned that must be pushed by hand once
                         (or kept below `--since`).
  tag, released          ok. "Released" = a GitHub release for the tag (`--github-release`) and
                         a file on the index (`--pypi NAME`); with neither flag a tag is enough.
  tag, not released      the release workflow's runs for that tag decide: one queued or running
                         = pending (ok); none = dispatch it; the newest one FINISHED and the
                         release is still missing = finding (exit 1). A failed publish is never
                         re-dispatched blind: a person reruns the failed job.

A tag on another commit than the bump is printed, never moved: a pushed release tag is immutable.

SAFETY. Writes happen only on the repository's default branch. Anywhere else (a PR, a dispatch on
a feature ref) the run is a dry run, so an unmerged bump is never tagged. `--dry-run` prints the
same plan and writes nothing.

EXIT. 0 every bump tagged and released, or pending - 1 a release that ran and is still missing,
or a write the remote did not take - 2 could not look: not a git checkout, a version file that does
not parse, no bump found at all, gh or the index unreachable.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

PROG = "ci-version-tag"
ACTIVE = frozenset({"queued", "in_progress", "waiting", "requested", "pending"})
_PEP440 = re.compile(r"^(\d+(?:\.\d+)*)(?:[-_.]?(a|b|rc)[-_.]?(\d+))?(?:[-_.]?post[-_.]?(\d+))?$",
                     re.IGNORECASE)
_PRE = {"a": 0, "b": 1, "rc": 2}


class Blind(Exception):
    """Could not look: the run ends with exit 2."""


def is_release(version: str) -> bool:
    """A version that ships under a tag: not a `.devN` build, not a `+local` one."""
    return not re.search(r"dev\d*$", version, re.IGNORECASE) and "+" not in version


def pep440_key(version: str) -> tuple:
    """Order release versions the way PEP 440 does (release, then a < b < rc < final, then post).

    Raises:
        ValueError: a version this subset does not read (epochs, dev, local).
    """
    m = _PEP440.match(version.strip())
    if not m:
        raise ValueError(f"not a release version: {version!r}")
    rel = [int(x) for x in m.group(1).split(".")]
    while len(rel) > 1 and rel[-1] == 0:
        rel.pop()
    pre = (_PRE[m.group(2).lower()], int(m.group(3))) if m.group(2) else (3, 0)
    return (tuple(rel), pre, int(m.group(4) or -1))


# ---------------------------------------------------------------- the version history (local git)

def _git(*args: str) -> str:
    try:
        r = subprocess.run(["git", *args], capture_output=True, text=True, check=False,
                           encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Blind(f"git {args[0]}: {exc}") from exc
    if r.returncode != 0:
        raise Blind(f"git {' '.join(args[:3])} failed: {r.stderr.strip()[-300:] or 'no output'}")
    return r.stdout


def version_in(text: str) -> str | None:
    """`[project].version` of a pyproject.toml text; None when it declares none (dynamic).

    Raises:
        Blind: the text is not TOML.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise Blind(f"version file does not parse: {exc}") from exc
    v = (data.get("project") or {}).get("version")
    return v if isinstance(v, str) else None


def bumps(path: str = "pyproject.toml", ref: str = "HEAD") -> list[tuple[str, str]]:
    """(version, bump commit) for every version `path` took on `ref`'s first-parent history, oldest
    first. The bump commit is the FIRST commit carrying that version; a later return to it (a
    revert) does not move it."""
    seen: dict[str, str] = {}
    prev: str | None = None
    for sha in _git("log", "--first-parent", "--reverse", "--format=%H", ref, "--", path).split():
        v = version_in(_git("show", f"{sha}:{path}"))
        if v is not None and v != prev and v not in seen:
            seen[v] = sha
        prev = v if v is not None else prev
    return list(seen.items())


# ---------------------------------------------------------------- the remote (GitHub, the index)

class Remote(Protocol):
    def default_branch(self) -> str: ...
    def tags(self) -> dict[str, str]: ...
    def releases(self) -> set[str]: ...
    def indexed(self, project: str) -> set[str]: ...
    def newest_run(self, workflow: str, tag: str) -> tuple[str, str, str] | None: ...
    def create_tag(self, tag: str, sha: str) -> None: ...
    def ref_sha(self, tag: str) -> str | None: ...
    def dispatch(self, workflow: str, tag: str) -> None: ...


class GitHub:
    """`Remote` over `gh api` (auth from GH_TOKEN) and the PyPI JSON API."""

    def __init__(self, repo: str, index: str = "https://pypi.org/pypi") -> None:
        self.repo, self.index = repo, index.rstrip("/")

    def _gh(self, *args: str, ok_404: bool = False) -> str | None:
        try:
            r = subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=False,
                               encoding="utf-8", errors="replace", timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Blind(f"gh api: {exc}") from exc
        if r.returncode != 0:
            if ok_404 and "HTTP 404" in r.stderr:
                return None
            raise Blind(f"gh api {args[-1] if args else ''} failed: "
                        f"{r.stderr.strip()[-300:] or 'no output'}")
        return r.stdout

    def default_branch(self) -> str:
        return (self._gh(f"repos/{self.repo}", "--jq", ".default_branch") or "").strip()

    def tags(self) -> dict[str, str]:
        """Remote tag -> the COMMIT it names. An annotated tag's ref names a tag object, so it is
        peeled through the local checkout's copy; one the checkout lacks maps to "" (unknown)."""
        out = self._gh("--paginate", f"repos/{self.repo}/git/matching-refs/tags/",
                       "--jq", '.[] | [.ref, .object.sha, .object.type] | @tsv') or ""
        local: dict[str, str] = {}
        for ln in _git("for-each-ref", "refs/tags", "--format=%(refname:short)\t%(*objectname)").splitlines():
            name, _, peeled = ln.partition("\t")
            if peeled:
                local[name] = peeled
        tags: dict[str, str] = {}
        for ln in out.splitlines():
            ref, sha, kind = (ln.split("\t") + ["", ""])[:3]
            name = ref.removeprefix("refs/tags/")
            tags[name] = sha if kind == "commit" else local.get(name, "")
        return tags

    def releases(self) -> set[str]:
        out = self._gh("--paginate", f"repos/{self.repo}/releases?per_page=100",
                       "--jq", ".[].tag_name") or ""
        return set(out.split())

    def indexed(self, project: str) -> set[str]:
        url = f"{self.index}/{project}/json"
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 -- fixed https base
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return set()  # not on the index yet: every version is missing there
            raise Blind(f"{url}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise Blind(f"{url}: {exc}") from exc
        return {v for v, files in (data.get("releases") or {}).items() if files}

    def newest_run(self, workflow: str, tag: str) -> tuple[str, str, str] | None:
        out = self._gh(f"repos/{self.repo}/actions/workflows/{workflow}/runs?branch={tag}&per_page=1",
                       "--jq", '.workflow_runs[] | [.status, (.conclusion // ""), .html_url] | @tsv')
        rows = [ln.split("\t") for ln in (out or "").splitlines() if ln.strip()]
        return (rows[0][0], rows[0][1], rows[0][2]) if rows else None

    def create_tag(self, tag: str, sha: str) -> None:
        try:
            self._gh("-X", "POST", f"repos/{self.repo}/git/refs",
                     "-f", f"ref=refs/tags/{tag}", "-f", f"sha={sha}")
        except Blind:
            if self.ref_sha(tag) is None:  # 422 "already exists" from a racing run is fine
                raise

    def ref_sha(self, tag: str) -> str | None:
        out = self._gh(f"repos/{self.repo}/git/ref/tags/{tag}", "--jq", ".object.sha", ok_404=True)
        return out.strip() if out else None

    def dispatch(self, workflow: str, tag: str) -> None:
        self._gh("-X", "POST", f"repos/{self.repo}/actions/workflows/{workflow}/dispatches",
                 "-f", f"ref={tag}")


# ---------------------------------------------------------------- the decision

@dataclass
class Row:
    version: str
    tag: str
    state: str   # ok | pending | tagged | dispatched | would-tag | would-dispatch | FINDING
    detail: str = ""


@dataclass
class Opts:
    prefix: str = "v"
    since: str = ""
    release_workflow: str = ""
    github_release: bool = False
    pypi: str = ""
    dry_run: bool = False


def reconcile(history: list[tuple[str, str]], remote: Remote, o: Opts) -> list[Row]:
    """One row per release version in `history`, acting on the remote unless `o.dry_run`.

    Raises:
        Blind: the remote could not be read, or `--since` / a version does not parse.
    """
    floor = pep440_key(o.since) if o.since else None
    todo = [(v, sha) for v, sha in history if is_release(v)]
    if floor is not None:
        try:
            todo = [(v, sha) for v, sha in todo if pep440_key(v) >= floor]
        except ValueError as exc:
            raise Blind(str(exc)) from exc
    tags = remote.tags()
    need_release = o.github_release or bool(o.pypi)
    releases = remote.releases() if o.github_release else set()
    indexed = remote.indexed(o.pypi) if o.pypi else set()
    rows: list[Row] = []
    for v, sha in todo:
        tag = o.prefix + v
        if tag not in tags:
            rows.append(_tag(v, tag, sha, remote, o))
            continue
        missing = [what for what, gone in (("GitHub release", o.github_release and tag not in releases),
                                           (f"{o.pypi} on the index", bool(o.pypi) and v not in indexed))
                   if gone]
        note = "" if tags[tag] in ("", sha) else f"tag names {tags[tag][:8]}, bump commit {sha[:8]}"
        if not need_release or not missing:
            rows.append(Row(v, tag, "ok", note))
            continue
        rows.append(_heal(v, tag, missing, remote, o))
    return rows


def _tag(v: str, tag: str, sha: str, remote: Remote, o: Opts) -> Row:
    then = f" + dispatch {o.release_workflow}" if o.release_workflow else ""
    if o.dry_run:
        return Row(v, tag, "would-tag", f"at {sha[:8]}{then}")
    remote.create_tag(tag, sha)
    got = remote.ref_sha(tag)
    if got != sha:
        return Row(v, tag, "FINDING", f"created {tag} at {sha[:8]} but the remote says {got}")
    if o.release_workflow:
        remote.dispatch(o.release_workflow, tag)
        return Row(v, tag, "tagged", f"at {sha[:8]}, dispatched {o.release_workflow}")
    return Row(v, tag, "tagged", f"at {sha[:8]}")


def _heal(v: str, tag: str, missing: list[str], remote: Remote, o: Opts) -> Row:
    gone = " and ".join(missing) + " missing"
    if not o.release_workflow:
        return Row(v, tag, "FINDING", f"{gone}; no --release-workflow to run")
    run = remote.newest_run(o.release_workflow, tag)
    if run is None:
        if o.dry_run:
            return Row(v, tag, "would-dispatch", f"{gone}; no {o.release_workflow} run for {tag}")
        remote.dispatch(o.release_workflow, tag)
        return Row(v, tag, "dispatched", f"{gone}; no run for {tag} until now")
    status, conclusion, url = run
    if status in ACTIVE:
        return Row(v, tag, "pending", f"{gone}; {o.release_workflow} {status}: {url}")
    return Row(v, tag, "FINDING", f"{gone} after {o.release_workflow} finished {conclusion or status}: "
                                  f"{url} -- rerun its failed jobs")


# ---------------------------------------------------------------- CLI

def _branch() -> str:
    ref = os.environ.get("GITHUB_REF", "")
    if ref.startswith("refs/heads/"):
        return ref.removeprefix("refs/heads/")
    if ref:
        return ""  # a tag or PR ref: never the default branch
    try:
        return _git("symbolic-ref", "--quiet", "--short", "HEAD").strip()
    except Blind:
        return ""  # detached


def main(argv: list[str] | None = None, remote: Remote | None = None) -> int:
    ap = argparse.ArgumentParser(prog=PROG, description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""),
                    help="owner/name (default: $GITHUB_REPOSITORY)")
    ap.add_argument("--file", default="pyproject.toml", help="the version file ([project].version)")
    ap.add_argument("--prefix", default="v", help="tag = prefix + version (default: v)")
    ap.add_argument("--since", default="", help="ignore versions below this one (history before the tags)")
    ap.add_argument("--release-workflow", default="",
                    help="workflow file dispatched on a new or unreleased tag (e.g. release.yml)")
    ap.add_argument("--github-release", action="store_true", help="a tag needs a GitHub release")
    ap.add_argument("--pypi", default="", help="a tag needs this project's files on PyPI")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    a = ap.parse_args(argv)
    if not a.repo:
        print(f"{PROG}: no --repo and no $GITHUB_REPOSITORY -- exit 2", file=sys.stderr)
        return 2
    o = Opts(a.prefix, a.since, a.release_workflow, a.github_release, a.pypi, a.dry_run)
    remote = remote or GitHub(a.repo)
    try:
        history = bumps(a.file)
        if not history:
            raise Blind(f"no version found in {a.file} on HEAD's history -- nothing was checked")
        if not o.dry_run:
            here, default = _branch(), remote.default_branch()
            if here != default:
                print(f"{PROG}: on {here or 'a detached/non-branch ref'}, not {default}: dry run")
                o.dry_run = True
        rows = reconcile(history, remote, o)
    except Blind as exc:
        print(f"{PROG}: {exc} -- exit 2, could not look", file=sys.stderr)
        return 2
    for r in rows:
        print(f"{r.state:15} {r.tag:14} {r.detail}".rstrip())
    bad = [r for r in rows if r.state == "FINDING"]
    summary = (f"{PROG}: {len(rows)} release versions checked, {len(bad)} finding(s)"
               f"{' (dry run)' if o.dry_run else ''}")
    print(summary)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(summary + "\n\n" + "\n".join(f"- `{r.tag}` {r.state} {r.detail}" for r in rows
                                                  if r.state != "ok") + "\n")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
