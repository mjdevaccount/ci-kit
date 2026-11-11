"""The merge train: test N armed PRs ONCE, together, and land them as N commits.

WHY (trade-data operator, 2026-11-11): "when we get like 8 prs queued up in trade data ... merge
them into a common branch and let CI run against that. Keep the commit history per PR at least."
Under strict protection ("up to date before merging") every merge to main puts every other armed
PR BEHIND, and the updater (pr_autoupdate) re-tests them one at a time: N queued PRs cost N
sequential CI + review-gate cycles, and before the serial queue they cost O(N^2) runs. GitHub's
native merge queue needs an organisation (and Enterprise Cloud for a private repo); Mergify,
Trunk and Aviator are paid for private repos. So this is ours, on GitHub Actions.

THE SHAPE, one module per job:
  * policy.py  -- PURE: which PRs are candidates, the train's phase, the bisect split.
  * git.py     -- real git, no network: compose a train (one squash commit per PR, the shape
                  squash-merge gives today) and VERIFY one (each commit is exactly its PR's head
                  merged onto the commit before it), and map landed commits back to their PRs.
  * github.py  -- the GitHub reads both callers share: the PR, and "did the caller's review gate
                  PASS at this exact head" (the gate's own workflow runs, never a comment).
  * cli.py     -- `ci-merge-train`, one pass of the state machine (finalize / judge / compose).
The review gate's train path is ci_kit/review/train.py, which calls git.verify + github.

THE TRAILERS are the binding between a train commit and its PR: `Merge-Train-PR: <n>` and
`Merge-Train-Head: <sha>` in the commit's LAST paragraph, parsed here (never `%(trailers)`, whose
`---` divider rule would read a PR body's horizontal rule as the end of the message). They are
claims, not proof: the gate re-derives every commit's tree from the named head and fails on any
byte that differs, so a forged trailer can only reproduce reviewed content.
"""
from __future__ import annotations

TRAIN_PREFIX = "train/"          # a train PR's head branch; the review gate keys on it
TRAIN_LABEL = "merge-train"      # on every train PR
RIDING = "in-train"              # on a constituent while its train is open: pr_autoupdate skips it
LANDED = "landed-via-train"      # a constituent closed because its commit landed; also a finalized train PR
EJECTED = "train-ejected"        # out of trains for good: back to its own auto-merge
NEEDS_UPDATE = "train-needs-update"  # skipped: its context moved since review; pr-autoupdate updates it anyway
OPT_OUT = "autoland-off"         # a label or a title tag: never in a train (as never armed)
TRAILER_PR = "Merge-Train-PR"
TRAILER_HEAD = "Merge-Train-Head"
COMMENT_MARK = "<!-- merge-train -->"

LABELS = {
    TRAIN_LABEL: ("5319e7", "merge-train: a train PR (ci-kit ci-merge-train)"),
    RIDING: ("c5def5", "merge-train: riding an open train; pr-autoupdate skips it"),
    LANDED: ("0e8a16", "merge-train: landed as its own commit via a train"),
    EJECTED: ("d93f0b", "merge-train: ejected from a red train; lands on its own auto-merge"),
    NEEDS_UPDATE: ("fbca04", "merge-train: skipped, the lines around its change moved; updated even while a train is open"),
}
