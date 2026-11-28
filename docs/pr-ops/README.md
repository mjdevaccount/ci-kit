# PR operations

Strict branch protection ("require branches to be up to date before merging") is the honest
setting: a PR merges only after CI ran against the tree it will become. Without help it is also a
stall: every armed PR goes BEHIND each time main moves, and nobody is there to press "Update
branch". The PR-ops kit is the help, built from free parts: GitHub's merge queue needs an
organisation, and Mergify, Trunk and Aviator are paid.

| piece | module | reusable workflow | page |
|---|---|---|---|
| the armed-PR updater and its serial queue | `ci_kit/pr_autoupdate.py`, `ci_kit/pr_queue.py`, `ci_kit/pr_list.py` | `pr-autoupdate.yml` | [autoupdate.md](autoupdate.md) |
| Renovate arming | `ci_kit/pr_renovate.py` | (inside `pr-autoupdate.yml`) | [autoupdate.md](autoupdate.md#renovate-arming) |
| smart auto-merge (red classification + rerun) | `ci_kit/redclass.py`, `ci_kit/pr_heal.py` | (inside `pr-autoupdate.yml`) | [autoupdate.md](autoupdate.md#smart-auto-merge) |
| base-driven retry | `ci_kit/pr_baseretry.py` | (inside `pr-autoupdate.yml`) | [autoupdate.md](autoupdate.md#base-driven-retry) |
| the PR scheduler and head fail-fast | `ci_kit/pr_sched.py`, `ci_kit/pr_admit.py` | (inside `pr-autoupdate.yml`, `review-gate.yml`) | [autoupdate.md](autoupdate.md#the-pr-scheduler) |
| the queue hold | `ci_kit/pr_hold.py` | (inside `pr-autoupdate.yml`) | [autoupdate.md](autoupdate.md#the-queue-hold) |
| doomed-run cancel | `ci_kit/doomed_runs.py` | (inside `pr-autoupdate.yml`) | [autoupdate.md](autoupdate.md#cancel-what-cannot-merge) |
| keeping held PRs current | `ci_kit/pr_held.py` | (inside `pr-autoupdate.yml`) | [autoupdate.md](autoupdate.md#keeping-held-prs-current-update-held) |
| the heal ladder | `ci_kit/pr_ladder.py`, `ci_kit/pr_regen.py`, `ci_kit/pr_apply.py`, `ci_kit/pr_fix.py`, `ci_kit/pin_rehash.py` | `pr-heal.yml` | [heal-ladder.md](heal-ladder.md) |
| the dependency-bot relock | `ci_kit/pr_relock.py` | `relock.yml` | [heal-ladder.md](heal-ladder.md#relocking-a-dependency-bots-pr) |
| the merge train | `ci_kit/merge_train/`, `ci_kit/train_kick.py` | `merge-train.yml` | [merge-train.md](merge-train.md) |
| the push token (GitHub App or PAT) | `ci_kit/pr_token.py`, `.github/actions/pr-token/action.yml` | every token job | [push-token.md](push-token.md) |
| runner-wait measurement | `ci_kit/queue_wait.py` | none | [../gates/supply-chain.md](../gates/supply-chain.md) |

How the pieces fit:

1. **On every push to main** (and on arming, a new head, and a watched check finishing), the
   updater merges main into armed PRs. With `max-in-flight` it is a serial queue: at most N armed
   PRs are tested against current main at once, lowest PR number first.
2. **A red PR is classified by its jobs.** Machinery (a lost runner, a cancelled run) is rerun;
   judgment (the PR's own step failed) gets one comment, or, with `heal: true`, the heal ladder.
3. **The queue head is protected.** The scheduler parks non-head work on scarce runner pools, the
   queue hold makes an out-of-turn auto-merge impossible, and doomed runs are cancelled.
4. **Green-but-behind PRs can ride a merge train** that tests N of them once and lands them as N
   commits.
5. **Every pusher acts with one token**, decided by one composite, with one record line per job.

Every action these tools take is a PR comment marker or a run-summary line, and every refused
action is a red run: the run log is the append-only record, and a red run is what a person sees.
