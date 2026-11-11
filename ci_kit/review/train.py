"""The review gate's MERGE-TRAIN path: pass a train PR without a model call, or fail it loudly.

A train PR (head branch `train/...`, opened by ci-kit's ci-merge-train) carries N commits, each
ONE already-reviewed PR squashed onto the one before it. Re-reviewing the combined diff would pay
for N reviews again and judge a diff nobody wrote; skipping review would land whatever a
`train/` branch holds. So the gate VERIFIES instead (ci_kit/merge_train/git.verify):
  * the head is this repository's, and the branch is a single-parent chain on main;
  * every commit names exactly one PR and one head sha (its trailers), each PR once;
  * every commit's tree is BYTE-EQUAL to that head's own change three-way merged onto the commit
    before it -- recomputed here, so the commit can carry nothing but the reviewed change;
  * that PR is open, same-repository, and the caller's review gate PASSED at that exact head
    (ci_kit/merge_train/github.review_passed: the newest completed gate run at that sha, for that
    PR, concluded success -- a model SHIP / SHIP WITH NOTED RISK, a carried verdict, or a
    docs-only / version-bump skip).
All hold -> pass, no model call. Any one fails -> BLOCK, every problem named in the comment.
Could not finish looking (git or gh failed, an old git without `merge-tree --merge-base`) ->
BLIND. Both are red. The keying is the branch prefix alone, so ANY PR on a `train/` branch takes
this path and is red unless it IS a verified train: a hand PR cannot opt itself out of review by
branch name, and a train cannot opt itself into the model.

Writes body.md (the sticky `<!-- review-gate -->` comment) and the step outputs `result`
(pass | block | blind) and `title`. Exit 0 once written; a crash before that fails the step, and
the job, red.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

from ci_kit.merge_train import git as tgit
from ci_kit.merge_train import github
from ci_kit.review import tools
from ci_kit.review.verdict import MARKER


def _ensure(pr: int, head: str) -> None:
    if not tgit.has_commit(".", head):
        tgit.git(".", "fetch", "-q", "--no-tags", "origin", f"refs/pull/{pr}/head")
    if not tgit.has_commit(".", head):
        tgit.git(".", "fetch", "-q", "--no-tags", "origin", head)
    if not tgit.has_commit(".", head):
        raise ValueError("not on the remote")


def judge(repo: str, head_repo: str, base: str, head: str, workflow: str) -> tuple[str, list[tuple[int, str, str]], list[str]]:
    """(result, constituents, problems)."""
    if head_repo != repo:
        return "block", [], [f"the head is a fork ({head_repo}); a train is only ever this repository's branch"]
    try:
        cons, problems = tgit.verify(".", base, head, _ensure,
                                     lambda pr, h: github.review_passed(repo, workflow, pr, h))
    except tgit.Invalid as e:
        return "block", [], [str(e)]
    except (subprocess.CalledProcessError, OSError, ValueError) as e:
        err = (getattr(e, "stderr", "") or "").strip()
        return "blind", [], [f"could not finish verifying: {e}" + (f" ({err[-300:]})" if err else "")]
    return ("block" if problems else "pass"), cons, problems


def body(result: str, cons: list[tuple[int, str, str]], problems: list[str], gate: str) -> str:
    lines = [MARKER, "### review gate (merge train)", ""]
    if result == "pass":
        lines.append(f"**PASS, merge train verified** -- each of the {len(cons)} commit(s) is exactly its PR's "
                     "reviewed head applied onto the commit before it, and that PR's review gate passed at that "
                     "head. The combined diff is not re-reviewed.")
    elif result == "block":
        lines.append("**BLOCKED: not a verified merge train.** A `train/` branch passes review only when every "
                     "commit is a reviewed PR head and nothing else:")
    else:
        lines.append("**BLIND** -- the train could not be verified. This is a red check, not a pass:")
    lines += [f"- {p}" for p in problems]
    if cons:
        lines += ["", "| PR | head | train commit |", "|---|---|---|"]
        lines += [f"| #{n} | `{h[:12]}` | `{sha[:12]}` |" for n, h, sha in cons]
    lines += ["", f"_gate: {gate}_"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--body-out", required=True)
    a = ap.parse_args(argv)
    e = os.environ
    wf = e.get("WORKFLOW_REF", "").split("@", 1)[0].rsplit("/", 1)[-1] or "review-gate.yml"
    result, cons, problems = judge(e.get("GITHUB_REPOSITORY", ""), e.get("HEAD_REPO", ""),
                                   e["BASE_SHA"], e["HEAD_SHA"], wf)
    with open(a.body_out, "w", encoding="utf-8") as fh:
        fh.write(body(result, cons, problems, e.get("GATE", "ci-kit")))
    titles = {"pass": f"review gate passed: merge train of {len(cons)} verified",
              "block": f"review gate BLOCKED: merge train not verified ({len(problems)} problem(s))",
              "blind": "review gate BLIND: the merge train could not be verified"}
    tools.emit("result", result)
    tools.emit("title", titles[result])
    tools.summary(f"merge train: {result}" + "".join(f"\n- {p}" for p in problems))
    print(f"{result}: {titles[result]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
