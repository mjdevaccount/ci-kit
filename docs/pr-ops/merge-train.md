# The merge train (`ci-merge-train`)

`ci_kit/merge_train/` (v1.25.0; run cancel v1.26.0), run by the reusable `merge-train.yml`
([inputs](../reusable-workflows.md#merge-trainyml)). The design, the trailers and the bisect are in
`ci_kit/merge_train/__init__.py` and `policy.py`; one pass is `ci_kit/merge_train/cli.py`. Free:
GitHub's merge queue needs an organisation, and Mergify, Trunk and Aviator are paid.

It batches the armed, reviewed, green-but-behind PRs into ONE `train/<run>` PR: one squash commit
per PR (title `(#N)`, body, co-authors, `Merge-Train-PR` / `Merge-Train-Head` trailers), lowest PR
number first, 2..8. CI runs once.

- **A green train is NOT auto-merged.** The pass re-checks every rider at land time (open, armed,
  not a draft, no `autoland-off`, head unchanged, review still passing on this base) and only then
  lands it by REBASE (per-PR history kept), each constituent closed `landed-via-train` with its
  main sha.
- **Red ejects the newer half** (n>=4, else all) back to individual auto-merge, unless the red is
  machinery (a check that never started, a BLIND gate): then a bounded retry, never an ejection.
- **Stale/stuck recomposes.** Stale is never held against a rider; a rider released from 2 STUCK
  trains at one head is held until its head changes.
- **A rider boards only if its review passed against THIS base** (its passing run was created
  after the PR's last base change, from the PR timeline's `filteredCount` +
  `BaseRefChangedEvent`; a timeline GitHub will not answer skips that rider, BLIND, never the
  pass) and its diff lands with the context the review read. A rider skipped for moved context is
  labelled `train-needs-update`, which pr-autoupdate updates even while a train is open.
- **The pass acts only on a train it composed** (a composition record in the PR body, checked
  against its own run). The review gate verifies a train instead of re-reviewing it
  (`ci_kit/review/train.py`, the same checks plus the record; [review-gate.md](../review-gate.md)).
- **Every action is a PR comment** (riders carry `rode` / `released` / `landed` markers) plus the
  run summary; a stuck train, a retry, a hold, or a green train GitHub would not merge turns the
  run red.
- **Every train close** (stale, stuck, red, land-time recheck, superseded) goes through ONE
  `ops.close_train`, which also cancels the train branch's unfinished workflow runs (v1.26.0; a
  closed train's CI once held a consumer's one heavy runner ~25 min), never the train workflow's
  own runs, never an open PR's; a cancel GitHub refuses turns the run red.
- **The lane** (v1.35.0): before it pushes a train the composer holds the queue head for it
  (`queue/position` pending), then re-reads main. Main moved = no train opened and the lane given
  back (a lock-maintenance PR once landed during a 6-minute compose). The lane goes back on every
  exit without a train, a raise included, and success only to a PR still the head; a pass that
  ends with no train open gives a held PR what the queue hold would post at once (success only if
  it is still the head, `ops.release_lane`), rather than on pr-autoupdate's next pass.

pr-autoupdate skips riders and holds non-conflict updates while a train is open
([autoupdate.md](autoupdate.md#the-queue-hold)).

## The workflow

`merge-train.yml` runs on the caller's SECURE pool with a PAT (GITHUB_TOKEN pushes fire no CI, so
the train's checks would never start; unconfigured is RED). The PAT needs contents, pull requests
and workflows RW (a train carrying a workflow edit is refused without workflows) and actions RW (a
closed train's unfinished runs are cancelled; without it each close is a red run naming the 403).
There is no checkout: git reads PR heads as data in a temp repo. The caller template is the file's
header: an hourly backstop cron + `workflow_dispatch` (pr-autoupdate's `train-workflow` dispatches
the rest) + `pull_request_target` labeled/closed + `workflow_run` of CI/review.
`pull_request_target`, not `pull_request`: the BASE branch's copy of the caller decides what runs
with the token, so a PR cannot edit its own trigger into a token exfiltration.

A caller must also have: rebase merges ALLOWED on the base branch (the train lands by rebase so
main keeps one commit per PR); the review gate at a ci-kit with the train path (v1.25.0+), or every
train is red at its review gate; pr-autoupdate at v1.25.0+. A train never force-pushes and never
bypasses protection: it is NOT armed; the pass merges it (rebase, `--match-head-commit`) only when
its required checks are green AND every rider re-verifies, and GitHub still enforces protection on
that merge.

## Woken by the queue

`ci_kit/train_kick.py` (v1.34.0). With pr-autoupdate's `train-workflow` set, each pass dispatches
the caller's merge-train workflow only when a pass would find work:

- 2+ candidates by the train's OWN rule (`merge_train.policy.candidates`);
- no armed PR into the base landing on its own auto-merge (CLEAN; a candidate or not, v1.35.0);
- no train open, no pass queued or running;
- a candidate ready (last required check, or arming) after the newest pass was created (that is
  the dedup; no state kept).

A PASS is a `schedule` or `workflow_dispatch` run, never skipped or cancelled: the train's
`workflow_run` / label runs are mostly job-`if:` skips (9 of 10 on one consumer) that judged
nothing. The train's cron becomes an hourly backstop: a 20-minute cron found work on 3 of 52
passes (one consumer, 2026-11-14), each a secure-pool job. An unreadable run list or a refused
dispatch is a red run naming Actions: Read and write; every decision is a `merge train:` summary
line.
