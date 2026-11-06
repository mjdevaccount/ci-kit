"""The review gate's two pre-steps: carry a verdict across a base-only update, and name siblings.

WHY (trade-data 2026-09-26, its docs/decisions/concurrent-agent-merge-setup-2026-09.md phase 1).
Once the armed-PR updater (`pr-autoupdate.yml`, this kit) merges main into every armed PR on each push to main, a
PR's head moves without its author touching it. Re-running the Opus reviewer on each of those
moves costs a review per open PR per merge and finds nothing new: the PR's OWN change did not
move. So the gate hashes the PR's own diff and, when it equals the diff a previous run PASSED,
passes with "verdict carried from <sha>" instead of paying again.

WHAT IS HASHED. `git diff <merge-base(base, head)> <head>` -- the PR's net change against the
main it now sits on, with every merged-in main commit excluded by construction -- with blob ids
and hunk line numbers stripped (`normalise`) but context lines KEPT, via `git hash-object`. So:
  * main moves elsewhere            -> same hash -> carried (no Opus run);
  * the PR's own lines change       -> new hash  -> full review;
  * main edits lines ADJACENT to a PR hunk -> new hash -> full review. Conservative on purpose:
    context the reviewer read has changed under the PR, which is exactly a semantic-conflict
    shape.

WHERE THE LAST VERDICT LIVES. An artifact `review-verdict-<pr>` holding {hash, head_sha, result}
-- not a PR comment. A comment can be edited by anyone holding the operator's token (every
session), so a forged marker would skip review; an artifact can only come from a workflow run.
It is written by the gate's separate `record` JOB, on a fresh runner, from step outputs frozen
BEFORE the PR's own code ran (the reviewer runs pytest) -- a record written in the review job
could be rewritten by a PR test (reviewer finding, 2026-09-26). Every FULL review records, pass
AND block, and the NEWEST record wins: an older pass at the same hash never outvotes a newer
block. Only a pass is ever carried.

RESIDUAL (CI review gate, 2026-09-26): the review job runs the PR's own pytest, and if its child
processes inherit ACTIONS_RUNTIME_TOKEN they could upload a `review-verdict-<pr>` artifact in the
same run. The separate `record` job closes the rewrite-a-file path, not that one. Same class as
the workflow's residual: PR code already runs on that runner.

Binary files: the diff is taken with `--binary`, so a change to a binary's bytes changes the
hash (without it, the stripped `index` line was the only trace of the content).

SIBLINGS. `siblings` lists open PRs whose changed files intersect this one's, as plain text the
workflow appends to the reviewer's prompt. The reviewer stays read-only: it gets the list, not
`gh`.

Exit 2 = this script could not finish looking (git/gh failed). The workflow reads that as "no
carry" -> a full review, never as a pass.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

ARTIFACT_PREFIX = "review-verdict-"
MAX_RUNS_SCANNED = 30  # newest completed gate runs on this branch searched for a verdict artifact
MAX_SIBLINGS = 15


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def normalise(diff: bytes) -> bytes:
    """Drop what a base-only move changes without changing the PR: blob ids and line numbers."""
    out = []
    for line in diff.splitlines():
        if line.startswith(b"index "):
            continue
        if line.startswith(b"@@"):
            end = line.find(b"@@", 2)
            line = b"@@" + (line[end + 2:] if end > 0 else b"")
        out.append(line)
    return b"\n".join(out)


def own_diff_hash(base_sha: str, head_sha: str) -> str:
    """Hash of the PR's own change against the main it sits on; 'empty' for no change.

    Context lines ARE hashed (git patch-id drops them, which is why it is not used): main
    editing a line next to a PR hunk changes what the reviewer read, so it must re-review.
    """
    mb = _git("merge-base", base_sha, head_sha).strip()
    diff = subprocess.run(["git", "diff", "--no-color", "--no-ext-diff", "--binary", "-U3", mb, head_sha],
                          check=True, capture_output=True).stdout
    if not diff.strip():
        return "empty"
    return subprocess.run(["git", "hash-object", "--stdin"], input=normalise(diff), check=True,
                          capture_output=True).stdout.decode().strip()


def carry_decision(current_hash: str, record: dict | None) -> tuple[bool, str]:
    """(carry?, why). Pure: the policy the tests pin."""
    if record is None:
        return False, "no earlier passing review of this PR"
    if record.get("result") != "pass":
        return False, "the last recorded verdict was not a pass"
    if not record.get("hash") or record.get("hash") != current_hash:
        return False, f"the PR's own diff changed since {str(record.get('head_sha', '?'))[:12]}"
    return True, f"verdict carried from {record.get('head_sha', '?')}"


def find_record(repo: str, branch: str, pr: int, current_run: str,
                workflow: str = "review-gate.yml") -> dict | None:
    runs = json.loads(subprocess.run(
        ["gh", "run", "list", "-R", repo, "--workflow", workflow, "--branch", branch,
         "--status", "completed", "--limit", str(MAX_RUNS_SCANNED), "--json", "databaseId"],
        check=True, capture_output=True, text=True).stdout)
    for run in runs:
        rid = str(run["databaseId"])
        if rid == current_run:
            continue
        with tempfile.TemporaryDirectory() as d:
            got = subprocess.run(["gh", "run", "download", rid, "-R", repo,
                                  "-n", f"{ARTIFACT_PREFIX}{pr}", "-D", d], capture_output=True,
                                 text=True)
            if got.returncode and "no artifact" not in (got.stderr + got.stdout).lower() \
                    and "no valid artifacts" not in (got.stderr + got.stdout).lower():
                # auth / permission / API failure is NOT "no history": exit 2 names it (the
                # outcome is still a full review, but the summary says the lookup broke)
                raise subprocess.CalledProcessError(got.returncode, "gh run download",
                                                    got.stdout, got.stderr)
            f = Path(d) / "verdict.json"
            if got.returncode == 0 and f.exists():
                rec = json.loads(f.read_text(encoding="utf-8"))
                if rec.get("pr") == pr:
                    return rec
    return None


def sibling_lines(me: int, my_files: set[str], prs: list[dict]) -> list[str]:
    out = []
    for p in prs:
        if p.get("number") == me:
            continue
        shared = sorted(my_files & {f.get("path", "") for f in p.get("files") or []})
        if shared:
            title = " ".join(str(p.get("title", "")).split())[:100]
            out.append(f"- #{p['number']} ({title}): {', '.join(shared[:8])}"
                       + (f" (+{len(shared) - 8} more)" if len(shared) > 8 else ""))
    return out[:MAX_SIBLINGS]


def _emit(name: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        print(f"{name}={value}")
        return
    delim = f"EOF_{secrets.token_hex(8)}"  # a sibling title must not be able to close the block
    with open(out, "a", encoding="utf-8") as fh:
        fh.write(f"{name}<<{delim}\n{value}\n{delim}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("carry")
    s = sub.add_parser("siblings")
    for p in (c, s):
        p.add_argument("--repo", required=True)
        p.add_argument("--pr", type=int, required=True)
        p.add_argument("--base", required=True)
        p.add_argument("--head", required=True)
    c.add_argument("--branch", required=True)
    c.add_argument("--run-id", required=True)
    # the CALLER's workflow file: a reusable gate's runs (and artifacts) are listed under it
    c.add_argument("--workflow", default="review-gate.yml")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "carry":
            h = own_diff_hash(args.base, args.head)
            carried, why = carry_decision(h, find_record(args.repo, args.branch, args.pr, args.run_id,
                                                               args.workflow))
            # the sha THIS run's record names: a carried pass keeps its first reviewer's sha. Emitted
            # as step outputs (frozen at step end), and written to an artifact only by the `record`
            # job on its own runner -- never a file the PR's code could reach.
            _emit("origin", why.removeprefix("verdict carried from ") if carried else args.head)
            _emit("carried", "true" if carried else "false")
            _emit("why", why)
            _emit("hash", h)
            print(f"carried={carried}: {why} (hash {h})")
            return 0
        mb = _git("merge-base", args.base, args.head).strip()
        mine = set(_git("diff", "--name-only", mb, args.head).split())
        prs = json.loads(subprocess.run(
            ["gh", "pr", "list", "-R", args.repo, "--state", "open", "--limit", "100",
             "--json", "number,title,files,isCrossRepository"],
            check=True, capture_output=True, text=True).stdout)
        # a fork's title is outsider text going into the reviewer's prompt: same-repo PRs only
        lines = sibling_lines(args.pr, mine, [p for p in prs if not p.get("isCrossRepository")])
        _emit("text", "\n".join(lines) if lines else "(none: no open PR touches these files)")
        print(f"{len(lines)} sibling PR(s) share files")
        return 0
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as e:
        print(f"::warning::ci_kit.review.carry {args.cmd} could not finish: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
