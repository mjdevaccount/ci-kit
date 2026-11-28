"""ci-migration-open-prs: a migration number this branch adds must not be taken by the base branch
or by an OLDER open PR.

MOVED FROM trade-data (v1.50.0, `scripts/lint_migration_open_prs.py`). Each PR's CI compares
against the base only, so two open PRs can both add `387_*.sql` and both go green (it happened:
two sessions, an hour apart). Merged, the second 387 is a duplicate -- and a migrator that skips
any file <= MAX(version) never runs it: no error, no row. Neither "check the live max" nor "ls the
migrations dir" can see another open PR.

THE RULE. For every `<--dir>/<--pattern>` file this branch ADDS (vs `--base`):
  * its number is already on the base under another name          -> finding
  * its number is added by another OPEN PR with a LOWER number      -> finding (the older PR keeps it)
A newer PR holding the same number is that PR's finding: whoever opened second renumbers. Not a
PR yet (a local run) = newest, so any open PR's claim wins. DRAFTS hold no number. A file another
PR ADDS, RENAMES or COPIES onto the number counts. STALENESS: the check runs when THIS PR's CI
runs; if an older PR adds the number later, a younger green PR can still land first, and the
older PR's own update then hits the 'already on the base' finding -- nothing is silently skipped.

ONE GraphQL call reads every open PR's files (a PR past 100 files is re-read in full over REST).
The token is `GH_TOKEN` / `GITHUB_TOKEN`, else `gh auth token`; the repo is `--repo`, else
`$GITHUB_REPOSITORY`. `--prs-json FILE` replays a saved list (`[{number, head, added: [paths]}]`)
instead of asking GitHub -- offline runs and must-fire fixtures.

  ci-migration-open-prs [--dir migrations] [--pattern '(\\d{3,})_[^/]+\\.sql'] [--repo OWNER/NAME]
                        [--base origin/main] [--head HEAD] [--prs-json FILE] [--root DIR] [--selftest]

Exit 0 clean, 1 findings, 2 could not look (no token, no repo, GitHub unreachable, git failed, an
unreadable `--prs-json`): a checker that cannot reach the PR list must not claim the number free.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence

from ci_kit import site_lint

GATE = "migration-open-prs"
TAKES = ("ADDED", "RENAMED", "COPIED")
REST_TAKES = ("added", "renamed", "copied")
API = "https://api.github.com"
QUERY = """query($owner:String!,$name:String!,$after:String){repository(owner:$owner,name:$name){
pullRequests(states:OPEN,first:50,after:$after){pageInfo{hasNextPage endCursor}
nodes{number headRefName isDraft files(first:100){pageInfo{hasNextPage}nodes{path changeType}}}}}}"""


class Blind(Exception):
    """Could not look: exit 2."""


def numbers(paths: Sequence[str], mig: re.Pattern[str]) -> dict[str, str]:
    """PURE. {number: path} for the migration files among `paths`."""
    return {m.group(1): p for p in paths if (m := mig.match(p))}


def findings(mine: dict[str, str], base: dict[str, str], prs: list[dict], me: int | None, branch: str,
             mig: re.Pattern[str]) -> list[str]:
    """PURE. `prs` = [{number, head, added: [paths]}]; `me` = this PR's number or None."""
    out = []
    for n, path in sorted(mine.items()):
        if n in base and base[n] != path:
            out.append(f"{path}: {n} is already on the base as {base[n]} -- renumber above its max")
        for pr in prs:
            if pr["number"] == me or (me is None and pr["head"] == branch):
                continue
            theirs = numbers(pr["added"], mig)
            if n in theirs and theirs[n] != path and (me is None or pr["number"] < me):
                out.append(f"{path}: {n} is taken by OLDER open PR #{pr['number']} ({theirs[n]}) -- renumber; "
                           "a migrator that skips a file <= MAX(version) would never run whichever lands second")
    return out


def _token() -> str:
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True,
                              timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        raise Blind(f"no GH_TOKEN/GITHUB_TOKEN and `gh auth token` failed ({e})") from e


def http(tok: str) -> Callable[[str, str, dict | None], object]:
    """A JSON request function over urllib (stdlib: the kit takes no HTTP dependency)."""
    def call(method: str, url: str, body: dict | None = None) -> object:
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                     headers={"Authorization": f"bearer {tok}", "Accept": "application/json",
                                              "Content-Type": "application/json", "User-Agent": "ci-kit"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - fixed https API host
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise Blind(f"GitHub unreachable ({type(e).__name__}: {e})") from e
    return call


def open_prs(repo: str, call: Callable[[str, str, dict | None], object]) -> list[dict]:
    """Every non-draft open PR with the files it adds, renames or copies onto a path."""
    owner, name = repo.split("/", 1)
    prs, after = [], None
    while True:
        d = call("POST", f"{API}/graphql", {"query": QUERY, "variables": {"owner": owner, "name": name, "after": after}})
        if not isinstance(d, dict) or d.get("errors") or not d.get("data"):
            raise Blind(f"GitHub GraphQL answered no data: {str(d)[:300]}")
        conn = d["data"]["repository"]["pullRequests"]
        for n in conn["nodes"]:
            if n.get("isDraft"):
                continue
            files = n["files"]
            added = (_rest_added(repo, n["number"], call) if files["pageInfo"]["hasNextPage"]
                     else [f["path"] for f in files["nodes"] if f["changeType"] in TAKES])
            prs.append({"number": n["number"], "head": n["headRefName"], "added": added})
        if not conn["pageInfo"]["hasNextPage"]:
            return prs
        after = conn["pageInfo"]["endCursor"]


def _rest_added(repo: str, num: int, call: Callable[[str, str, dict | None], object]) -> list[str]:
    paths, page = [], 1
    while True:
        batch = call("GET", f"{API}/repos/{repo}/pulls/{num}/files?per_page=100&page={page}", None)
        if not isinstance(batch, list):
            raise Blind(f"PR #{num} files: not a list ({str(batch)[:200]})")
        paths += [f["filename"] for f in batch if f.get("status") in REST_TAKES]
        if len(batch) < 100:
            return paths
        page += 1


def prs_from_file(path: pathlib.Path) -> list[dict]:
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        return [{"number": int(r["number"]), "head": str(r["head"]), "added": list(r["added"])} for r in rows]
    except (OSError, ValueError, TypeError, KeyError) as e:
        raise Blind(f"--prs-json {path} unreadable ({e})") from e


def _me() -> int | None:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            num = (json.load(f).get("pull_request") or {}).get("number")
        return int(num) if num else None
    return None


def selftest() -> list[str]:
    mig = re.compile(r"^migrations/(\d{3,})_[^/]+\.sql$")
    mine = {"387": "migrations/387_a.sql"}
    pr = lambda n, *p: {"number": n, "head": f"b{n}", "added": list(p)}  # noqa: E731
    cases = [
        ("older PR holds it", findings(mine, {}, [pr(10, "migrations/387_b.sql")], 20, "me", mig), 1),
        ("newer PR holds it", findings(mine, {}, [pr(30, "migrations/387_b.sql")], 20, "me", mig), 0),
        ("local run: any PR wins", findings(mine, {}, [pr(30, "migrations/387_b.sql")], None, "me", mig), 1),
        ("own PR skipped", findings(mine, {}, [pr(20, "migrations/387_a.sql")], 20, "me", mig), 0),
        ("own branch skipped", findings(mine, {}, [dict(pr(5, "migrations/387_a.sql"), head="me")], None, "me", mig), 0),
        ("base holds it", findings(mine, {"387": "migrations/387_z.sql"}, [], 20, "me", mig), 1),
        ("different number", findings(mine, {}, [pr(10, "migrations/388_b.sql")], 20, "me", mig), 0),
        ("not a migration", findings(mine, {}, [pr(10, "migrations/387_b.txt")], 20, "me", mig), 0),
    ]
    return [f"{name}: want {want} finding(s), got {got}" for name, got, want in cases if len(got) != want]


def main(argv: Sequence[str] | None = None) -> int:
    ap = site_lint.args_parser("ci-migration-open-prs", __doc__)
    ap.add_argument("--dir", default="migrations")
    ap.add_argument("--pattern", default=r"(\d{3,})_[^/]+\.sql")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--prs-json", type=pathlib.Path, default=None)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if a.selftest:
        return site_lint.selftest_exit(GATE, selftest)
    root = site_lint.gate_root(a.root)
    d = site_lint.norm_dir(a.dir)
    try:
        mig = re.compile(f"^{re.escape(d)}/{a.pattern}$")
        if mig.groups < 1:
            raise re.error("the pattern needs one group: the migration number")
    except re.error as e:
        print(f"{GATE}: --pattern {a.pattern!r}: {e}", file=sys.stderr)
        return 2

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout

    def scan() -> list[str]:
        try:
            added = git("diff", "--name-only", "--diff-filter=A", a.base, a.head, "--", f"{d}/").split()
            on_base = git("ls-tree", "--name-only", a.base, f"{d}/").split()
        except subprocess.CalledProcessError as e:
            print(f"{GATE}: could not look: git failed ({(e.stderr or '').strip()})", file=sys.stderr)
            raise SystemExit(2) from e
        mine = numbers(added, mig)
        if not mine:
            return []
        branch = os.environ.get("GITHUB_HEAD_REF") or git("branch", "--show-current").strip()
        try:
            if a.prs_json is not None:
                prs = prs_from_file(site_lint.under(root, a.prs_json))
            elif "/" not in a.repo:
                raise Blind("no --repo OWNER/NAME and no $GITHUB_REPOSITORY")
            else:
                prs = open_prs(a.repo, http(_token()))
        except Blind as e:
            print(f"{GATE}: could not look: {e} -- cannot prove {sorted(mine)} free", file=sys.stderr)
            raise SystemExit(2) from e
        base_nums = {k: v for k, v in numbers(on_base, mig).items() if v not in added}
        return findings(mine, base_nums, prs, _me(), branch, mig)

    return site_lint.check([], gate=GATE, scan=scan, selftest=selftest)


if __name__ == "__main__":
    sys.exit(main())
