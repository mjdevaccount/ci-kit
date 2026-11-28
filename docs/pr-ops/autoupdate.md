# The armed-PR updater (`ci-pr-autoupdate`)

`ci_kit/pr_autoupdate.py`, run by the reusable `pr-autoupdate.yml`
([inputs](../reusable-workflows.md#pr-autoupdateyml)). It merges main into every open PR with
auto-merge **armed**, so "require branches up to date" is affordable; a conflict gets one label
and one page, never more. Output `relock` (lockfile-only conflicts, for a caller relock job) and
`heal` (the rows for the caller's `pr-heal.yml` matrix, [heal-ladder.md](heal-ladder.md)).

```yaml
# on push to main
jobs:
  update:
    uses: mjdevaccount/ci-kit/.github/workflows/pr-autoupdate.yml@v1
    # the PR list (with check results) is read with GITHUB_TOKEN via one narrow GraphQL query
    # (ci_kit/pr_list.py): a fine-grained PAT cannot read check runs
    permissions: { contents: read, pull-requests: read, checks: read, statuses: read }
    secrets:
      token: ${{ secrets.YOUR_PR_UPDATE_TOKEN }}   # fine-grained PAT; GITHUB_TOKEN fires no CI
      ntfy-url: ${{ secrets.YOUR_NTFY_URL }}       # optional
```

The secret **names are yours**: the workflow declares `token` and `ntfy-url` and you map onto
them; `--label` renames the conflict label. The full caller template (triggers, concurrency, the
job's `if:`) is the header of `.github/workflows/pr-autoupdate.yml`. That `if:` decides the
event-only skips in the update job itself, with **no separate gate job**: on a single-use pool a
gate costs a whole runner, as much as the pass it debounces (measured on a consumer, one day: 289
gates, 142 said go). List `synchronize` beside `auto_merge_enabled` in the caller's
`pull_request_target` types, or a new head on an armed PR starts no pass.

The job runs on the caller's SECURE pool (`secure-runner`, else the `CI_SECURE_RUNNER` switch for
the fixed labels `[self-hosted, home-secure]`, a pool with no docker socket), reading the token
from its `ci-secure` environment; hosted otherwise ([push-token.md](push-token.md#pools)).

## The serial queue

`--max-in-flight N` makes it a **serial queue** (`ci_kit/pr_queue.py`): at most N armed PRs tested
against current main at once, lowest PR number first, skipping a PR whose own checks are red or
still running. Under strict protection only one PR merges per main sha, so updating all of them
threw away 70% of PR CI runs (measured on a consumer repo, 2026-10-24). A PR skipped for a running
review hands its slot to the next in line, and a `conflict` label comes off any PR GitHub reports
mergeable again (v1.26.1).

### The PR list (`ci_kit/pr_list.py`)

The updater's open-PR list is ONE narrow GraphQL query (check-run name/status/conclusion, status
context/state, nothing more), returned in `gh pr list --json` shape. `gh pr list --json
statusCheckRollup` also selects each check's workflow run, which a token without `actions: read`
is refused (v1.7.0 still was, live 2026-10-24).

`settle()` (v1.43.0): an ARMED same-repo PR whose `mergeStateStatus` reads UNKNOWN (GitHub
computes it lazily; the updater fires right after a base push) is re-read up to
`CI_KIT_UNKNOWN_TRIES` (2) times `CI_KIT_UNKNOWN_WAIT_S` (5s) apart before `pr_queue` plans it as
behind. A non-strict base never reports BEHIND (seen live: behind_by=2, green, CLEAN), so planning
UNKNOWN as behind merged main into PRs that could land. Still UNKNOWN after that = planned as
before and named in the summary; a failed re-read keeps the last good rows, named; a garbage knob
falls back to its default.

## Renovate arming

With a queue, every pass first ARMS each Renovate PR whose own body says `**Automerge**: Enabled.`
(`ci_kit/pr_renovate.py`, v1.32.0): the GraphQL arm, never `gh pr merge --auto`. `autoland-off`,
a hand disarm, and Automerge Disabled (the database group, majors, `fails-green`) stay out; a
conflicting one gets its Renovate rebase box ticked; a refused arm or tick is a red run.

**An idle queue LANDS a CLEAN one** (v1.60.0, `pr_renovate.land_idle`). GitHub refuses to arm a PR
that is already CLEAN, and Renovate never merges in a queue repo (`pr-comment`), so a CLEAN
Enabled PR used to wait for a push to main that a quiet repo never gets (trade-infra#977/#978,
2026-11-28, `queue: 0/1 in flight, 0 to attempt` on every pass until a hand merge). Now, with
nothing in flight, nothing next and no train open, the pass MERGES the lowest-numbered one: squash,
`expectedHeadOid` = the head it read, only when GitHub says CLEAN (every check green, required or
not), the PR's own rollup reads green, and no workflow run on that head is still going. One per
pass: the merge moves main and the push-to-main pass arms the rest. UNSTABLE (a red check, or
`renovate/stability-days` still pending) is never landed. A refused merge is a red run; a moved
head or base is a race, decided next pass.

## Smart auto-merge

`--rerun-limit` (v1.27.0, default 2, 0 = off). An armed PR red at its head is classified by its
JOBS (`ci_kit/redclass.py`):

- **MACHINERY** (a lost runner, a log GitHub no longer has, a cancelled run, a setup step's
  network error, an alls-green aggregator red only because of those) is rerun
  (`rerun-failed-jobs`, needs the token's Actions: Read and write) at most N times per head, or
  simply updated when BEHIND.
- **JUDGMENT** (any of the PR's own steps failed) gets ONE PR comment naming the class and
  nothing else (with the heal ladder on, the ladder instead, [heal-ladder.md](heal-ladder.md)).

Every action is a PR-comment marker (`<!-- smart-merge:rerun|exhausted|judgment head=... -->`,
the budget and the record); a refused rerun or a spent budget turns the run red. The comment and
rerun logic is `ci_kit/pr_heal.py`.

## Base-driven retry

`ci_kit/pr_baseretry.py` (v1.36.0, input `base-retry-cap`). **On by default since v1.43.0** at cap
1 with `base-retry-bot-authors: renovate` (ci-kit#114/#120 sat red + BEHIND overnight with nobody
to push); `base-retry-cap: 0` turns it off, and off it reads nothing.

An armed PR held BEHIND and red on a required check that is not machinery ("the author's fix
re-queues it") is updated from main only on a **RED -> GREEN TRANSITION ON MAIN** for every
failing check, matched by (app, workflow file, name): a finished failing run on the PR's merge
base or the 3 commits before it, AND SUCCESS as the newest finished result on the main commits the
PR lacks (neutral, skipped, cancelled are neither). Main never red there = the old hold:
green-on-main alone is true of every judgment red under strict protection.

- **NOT COVERED:** a check red only on the PR's run while main's own runs stayed green (a
  time-dependent test that broke after main's last run): main recorded no red, so the PR is held
  as before.
- **Once per head**; queued AFTER every green eligible PR (`pr_queue.plan` `base_retry`), never
  the queue head. Never an unarmed, draft, fork, train, rider, parked, `conflict`,
  `autoland-off`, DIRTY or CONFLICTING PR, nor a head the heal ladder took; a PR it retries goes
  to neither the ladder nor `pr_heal` that pass (one actor per head).
- **The cap** counts retries since the last NON-UPDATER commit (first parents through 2-parent
  merges; a plain update does not reset it); at the cap, ONE comment per head and a red pass.
- **Markers** count only when the push token's own login wrote them
  (`merge_train.ops.comments_by`, the one comment reader, now with authors; `comments_of` is
  unchanged) and are counted along the head's ancestry: `<!-- smart-merge:base-retry head= base= -->`
  written BEFORE the PUT (no record, no update), `<!-- smart-merge:base-retry-spent head= -->`.
- An unreadable cap, or one above 30 (the most merges the count walks; a walk that runs out
  counts as spent), refuses only this feature (named, red pass). A failed or raced update still
  SPENDS that head's retry: the marker is written before the PUT by design, so a retry is never
  repeated unrecorded.
- **Reads are bounded:** `base-retry-max-per-pass` (default 3) candidates get main's runs read per
  pass, lowest PR number first, the rest named in the summary; one the record settles reads only
  its comments.
- **The no-author case** (`base-retry-bot-authors`, exact logins of Bot accounts; the reusable
  workflow passes `renovate` since v1.43.0 when cap > 0 and a queue runs, the library default
  stays empty = off; set while the cap or max-in-flight is 0 = a `::warning::`): a PR by a listed
  author (nobody will push its fix) is retried WITHOUT red-at-base evidence when main is green on
  every failing check now and moved after the PR's red run finished (the date-dependent test
  main's runs predate); a check main never runs (a review verdict) still holds it; same markers
  and cap.

Not covered elsewhere: GitHub's merge queue needs an org, Renovate is `rebaseWhen: conflicted` in
queue repos, and the heal ladder fixes the PR's own code.

## The PR scheduler

`ci_kit/pr_sched.py` (v1.29.0; policy `ci_kit/pr_queue.py` `head`/`schedule`/`admit`, pure). The
queue HEAD (lowest-numbered armed PR) goes first on the caller's one-runner pools: ORDER and
TIMING only. Nothing is skipped; a parked run is red until re-run. Inputs `scarce-pools`,
`preempt-after-minutes`, `head-stall-minutes` turn it on. With
`--scarce-pools '{"home-sealed":1,"home-heavy":1}'` each run:

- **parks** (label `queue:parked`, then cancel) a non-head run queued on a pool the head waits
  for, or one that started there under `--preempt-after-min` (3) ago;
- **re-runs** parked work in landing order (the head's at once, a non-head's one per pass when no
  scarce pool has a queued job);
- **updates** a BEHIND head as soon as its review verdict is recorded;
- **pages** a later PR merged past an in-flight head;
- **goes red** (`queue-head-starved`) when the head waited `--head-stall-min` (15) while a
  non-head held its pool. The next parked non-head waiting past twice that is named
  `PARKED STARVED` in every pass and paged ONCE per head (marker
  `<!-- queue:parked-starved sha=... -->`, ntfy's `recent()` when the marker is unreadable), never
  red (v1.35.0: on a busy day every red pass was one standing park).

The review gate's `queue-priority` admission (`python -m ci_kit.pr_admit`) parks a later PR's full
review while the head needs the sealed runner; it fails OPEN (a failed look reviews) and never
parks a 4th attempt. `redclass` reads `review deferred` (a job until v1.35.0, a step of `verdict`
since) as PARKED: `pr_heal` leaves it to the scheduler.

X=3 / N=15 come from one consumer repo's 2026-11-07..11-14 runs: sealed wait p90 12.7 min, run p50
1.7; heavy wait p90 36.5, run p50 6.1; 85/90 sealed and 196/245 heavy inversions were inside 5 min
of the holder starting.

### Head fail-fast

`head-fail-fast` (v1.45.0, default on; with `scarce-pools`; `pr_sched.review_young`):

- A BEHIND head whose review has spent under `preempt-after-minutes` of model time is updated at
  once instead of when the verdict records, so its one full CI run is at a head that can merge.
- **The head holds a slot** (`pr_queue.reserve`, with the queue hold): a stale head that can land
  takes a `max-in-flight` slot, so the next PR is not updated beside it.
- **The head's verdict is waited for** (`pr_autoupdate._await_head`): past that bound the pass's
  tail re-reads the review every 15 s, up to `head-verdict-wait-seconds` (240, 0 = off, clamped to
  300: the job is `timeout-minutes: 10`), and updates the head itself when the verdict is on
  record, on a fresh read of the PR (`tests/test_pr_head_fail_fast.py`). A pass that hands on a
  relock or a heal does NOT wait (a job's outputs reach the job that needs them only when this job
  ends), and a new head pushed during the wait ends it at once.
- **What the wait holds:** the updater's `secure-runner`, and the review gate's `verdict` job it
  waits for runs on the same pool. A pool that gives the repo ONE runner blocks its own verdict for
  the whole wait, so set `head-verdict-wait-seconds: 0` there or give the pool a second runner
  (nothing refuses that configuration).

## The queue hold

`ci_kit/pr_hold.py` (v1.30.0, `queue-hold: true`). Commit status `queue/position`: success on the
queue HEAD and a merge train's PR, pending on every other same-repo open PR (Renovate's included:
it lands in number order once armed, and `ci_kit/pr_renovate.py` arms each automerge one), posted
FIRST in every pass. Branch protection requiring it makes an out-of-turn auto-merge impossible (a
consumer once landed PR N+1 past head N, 2026-11-14).

- The head is the lowest armed PR that CAN land (`pr_queue.blocked`: not DIRTY, not stacked, no
  required check failing or cancelled unless parked/machinery and behind main): the same head for
  the updater and the scheduler, so a red head never freezes main.
- Order only: no check removed.
- A refused post on the head, or a head holding success unmerged twice `head-stall-minutes` after
  its last check (a required check that never reported), is red: a marker comment on the first
  pass, ONE ntfy page on the second.
- Token: Commit statuses RW. Turn it on BEFORE requiring the context, and keep `queue/position`
  out of `required-checks`.
- **The train owns the lane** (v1.35.0): while a LIVE merge train is open the head is held too
  (pending, `held for merge train #M`), so main cannot move under the train (measured: every
  unlanded train in a day closed `stale: main moved`, the head having landed beside them). A red,
  BEHIND/DIRTY or older-than-`train-stale-minutes` train releases the lane AND the update hold,
  each named in the summary (`hold: lane HELD/RELEASED ...`). The train never waits on the head,
  so no deadlock.

## Cancel what cannot merge

`ci_kit/doomed_runs.py` (v1.34.0, `cancel-doomed-runs`, default on): cancels the queued/running
`pull_request` runs at

- (a) the head the queue just replaced,
- (b) an armed DIRTY head on a STRICT base,
- (c) an armed BEHIND head on a strict base ONLY in a repo with no merge train (a train takes
  BEHIND PRs green at their head; v1.48.0: a `merge-train` workflow that has NEVER RUN, such as a
  reusable `workflow_call` definition like ci-kit's own, is no train), and only after `compare`
  proves it behind.

Strict is read (classic or ruleset), never assumed. Never the review workflow, never a non-PR
event, never an unarmed/train/riding/parked PR. A cancelled check blocks the merge like a missing
one: nothing reads passed. Each cancel is a summary line; a refused cancel or read is a red pass;
no Actions RW or no Administration read is one warning naming the grant.

## Waking the merge train

`train-workflow` / `train-min-batch` (v1.34.0, `ci_kit/train_kick.py`): each pass dispatches the
caller's merge-train workflow only when a pass would find work ([merge-train.md](merge-train.md#woken-by-the-queue)).

## Keeping held PRs current (`update-held`)

The update pass merges the base into ARMED PRs only. A PR you hold for review (open, auto-merge not
enabled, often labelled `autoland-off`) is never updated, so under "require branches to be up to
date" it falls BEHIND and stays there. `update-held: true` (default `false`: nothing changes for a
caller that does not set it; `ci_kit/pr_held.py`) keeps those current too, under a cost guard,
because every update re-runs the PR's CI. The input is available from v1.41.0:

- only on a `schedule` or `workflow_dispatch` pass, never on push, `workflow_run` or arming, so
  add a cron to the caller's `on:` (e.g. `schedule: [{cron: "17 * * * *"}]`);
- LAST in that pass, after the armed pass has written its outputs (`relock`, `heal`) and its
  summary, and only when no armed PR is still on its way to current: none the queue picked to
  update this pass, none BEHIND waiting only on its own running checks, none whose merge state is
  not computed yet. An armed PR held for a FAILING check does not block it (nothing updates that
  PR until its author pushes). No merge train may be open. The skip line names each blocking PR
  and its reason;
- only in the free `max-in-flight` slots (a held PR whose own checks are running holds one too, so
  armed plus held never exceed it; armed PRs never wait on held ones), lowest PR number first;
  with no queue, every eligible one. The skip line names every slot holder.

Worst case, plainly: on each tick (one scheduled or dispatched pass) every eligible held PR can be
re-merged with the base and its CI re-run, at most once per PR per tick. The update gives it a new
head, so the same PR is not eligible again until that head is 10 minutes old AND its CI has
finished, i.e. one update per PR per max(10 min, CI time), rounded up to your next tick. A day
costs at most (held PRs) x (ticks per day) CI runs, and fewer in practice: a PR already current
with the base is never touched. With `max-in-flight: 0` (no queue) ALL eligible held PRs are
updated in the same tick; with a queue, at most the free slots are.

Eligible: same-repo, not a draft, BEHIND, cleanly mergeable, a head commit at least 10 minutes old
(younger, or of unreadable age: someone may still be pushing), and no check of ANY name running or
failing on its head (`required-checks` narrows the armed queue, not this). Never touched: forks,
drafts, conflicting PRs, any bot's PR (a Bot account or a `[bot]` login, Renovate included: a bot
rebases its own branch) and merge-train branches. A held PR is never labelled, paged or relocked:
a conflict or an error on its update is one summary line, `#N: held, not updated: <why>`.
`autoland-off` opts a PR out of auto-LANDING only; it is still kept current. A failure of the held
pass never turns the run red: it is one `update-held: FAILED` line and a warning annotation. The
summary names every held PR it skipped and why, and ends
`update-held: N held PR(s) updated of M eligible, K skipped, E error(s)`.

## ci-kit's own updater

`.github/workflows/pr-autoupdate-self.yml` (2026-11-14) is not reusable: it calls
`pr-autoupdate.yml` from this checkout on push to main and `ci` completing, serial
(`max-in-flight: 1`, required `test`, `gitleaks (secret scan) / scan`). Before it, ci-kit's armed
PRs were updated by nothing.
