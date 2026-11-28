# ci-kit

Reusable CI gates, each proven to **fire** before it is trusted: a Python package (`ci_kit`,
zero dependencies, Python 3.11+), a composite action that runs it, reusable GitHub workflows, and
a pre-commit hooks manifest. Pin a release tag.

**Every tool exits 0 clean · 1 findings · 2 did-not-finish-looking.** 2 is never folded into 0
or 1: a crashed detector that returns 0 reads as healthy to every caller, and a gate that scanned
nothing has verified nothing.

## What is here

| path | what |
|---|---|
| `ci_kit/doc_lint.py` | `ci-doc-lint`: a doc names a path that **existed and is gone**. Not "does this resolve today" (~90% noise over a real corpus) but Tan/Wagner/Treude's question (EMSE 2024): *did this path's count fall from a positive integer to zero?* Routes, flag lists and prose were never files, so they go quiet with **no allowlist**. Also reads source files for `docs/**.md` pointers and `.claude/rules/*.md` auto-load triggers. Shrink-only baseline; `<!-- doc-rot: historical -->` opts a teardown record out, in the file itself |
| `ci_kit/secret_argv.py` | `ci-secret-argv-lint`: no secret expanded on the **host** into a `docker exec` / `curl` / `ssh` argv (it lands in `docker events`, `docker inspect`, `ps`), nor hoisted with `$(docker exec c printenv KEY)`. The fix is to let it expand where it lives: `docker exec c sh -c '... "$KEY" ...'` |
| `ci_kit/hidden_unicode.py` | `ci-hidden-unicode` (v1.31.0, moved from trade-data; `--renovate` v1.49.0 ALSO refuses NBSP, the Unicode spaces and U+2028/2029 -- Renovate's pinned `hiddenUnicodeCharactersRegex`, so the gate fires before the dependency dashboard does; `json_escaped()` writes JSON that passes either set): no invisible character in ANY tracked text file -- zero-width, bidi embed/override/isolate (Trojan Source), variation selectors, tag characters, Hangul fillers, soft hyphen, CGJ, Braille blank, C0/C1 controls (a raw NUL in a `.ts`/`.cs` string included), FEFF past byte 0 -- in file contents AND file names. Write the escape; no waiver. A file is skipped as binary only when its suffix is declared binary (the kit's list + `--binary-suffix .EXT`; a text suffix is refused) AND its bytes are not UTF-8, so no suffix can hide UTF-8 text; any other non-UTF-8 file, or a tracked path missing on disk, is exit 2 and EVERY such file is named with its fix |
| `ci_kit/declared_literal.py` | `ci-declared-literal` (v1.39.0): a literal that must live in ONE declared file (an address, a hostname, a port, an account id) appears in no other tracked file. The kit ships no policy: each `--rule LABEL` carries its `--pattern` (regex, OR'd), `--declared` files and `--message`; `--exempt-suffix` exempts a file kind. Comment lines are exempt, never more than the predecessor exempted (a differential test holds it); a PowerShell `<# #>` block is stripped and the rest scanned. No rule, a bad or empty-matching regex, or zero files read is exit 2. Every rule carries an `--example` it must flag (its own must-fire proof); comment openers are per file kind. The summary line names files read, exempt and rules loaded |
| `ci_kit/pr_autoupdate.py` | `ci-pr-autoupdate`: merge main into every open PR with auto-merge **armed**, so "require branches up to date" is affordable; a conflict gets one label and one page, never more. `--max-in-flight N` makes it a **serial queue** (`ci_kit/pr_queue.py`), and with a queue every pass first ARMS each Renovate PR whose own body says `**Automerge**: Enabled.` (`ci_kit/pr_renovate.py`, v1.32.0: the GraphQL arm, never `gh pr merge --auto`; `autoland-off`, a hand disarm, Automerge Disabled -- the database group, majors, `fails-green` -- stay out; a conflicting one gets its Renovate rebase box ticked; a refused arm or tick is a red run): at most N armed PRs tested against current main at once, lowest PR number first, skipping a PR whose own checks are red or still running. Under strict protection only one PR merges per main sha, so updating all of them threw away 70% of PR CI runs (trade-data, 2026-10-24). A PR skipped for a running review hands its slot to the next in line, and a `conflict` label comes off any PR GitHub reports mergeable again (v1.26.1) **Smart auto-merge (v1.27.0, `--rerun-limit`, default 2, 0 = off):** an armed PR red at its head is classified by its JOBS (`ci_kit/redclass.py`): MACHINERY (a lost runner, a log GitHub no longer has, a cancelled run, a setup step's network error, an alls-green aggregator red only because of those) is rerun (`rerun-failed-jobs`, needs the token's Actions: Read and write) at most N times per head, or simply updated when BEHIND; JUDGMENT (any of the PR's own steps failed) gets ONE PR comment naming the class and nothing else. Every action is a PR-comment marker (`<!-- smart-merge:rerun\|exhausted\|judgment head=... -->`, the budget and the record); a refused rerun or a spent budget turns the run red. `ci_kit/pr_heal.py` |
| `ci_kit/pr_baseretry.py` | BASE-DRIVEN RETRY (v1.36.0, pr-autoupdate `base-retry-cap`; ON BY DEFAULT since v1.43.0 at cap 1 with `base-retry-bot-authors: renovate` -- ci-kit#114/#120 sat red + BEHIND overnight with nobody to push; `base-retry-cap: 0` turns it off, and off it reads nothing): an armed PR held BEHIND and red on a required check that is not machinery -- "the author's fix re-queues it" -- is updated from main only on a RED -> GREEN TRANSITION ON MAIN for every failing check, matched by (app, workflow file, name): a finished failing run on the PR's merge base or the 3 commits before it, AND SUCCESS as the newest finished result on the main commits the PR lacks (neutral, skipped, cancelled are neither). Main never red there = the old hold: green-on-main alone is true of every judgment red under strict protection. NOT COVERED: a check red only on the PR's run while main's own runs stayed green (a time-dependent test that broke after main's last run) -- main recorded no red, so the PR is held as before. Once per head; queued AFTER every green eligible PR (`pr_queue.plan` `base_retry`), never the queue head; never an unarmed, draft, fork, train, rider, parked, `conflict`, `autoland-off`, DIRTY or CONFLICTING PR, nor a head the heal ladder took; a PR it retries goes to neither the ladder nor pr_heal that pass (one actor per head). Cap: retries since the last NON-UPDATER commit (first parents through 2-parent merges; a plain update does not reset it); at the cap ONE comment per head and a red pass. Markers count only when the push token's own login wrote them (`merge_train.ops.comments_by`, the one comment reader, now with authors; `comments_of` is unchanged) and are counted along the head's ancestry: `<!-- smart-merge:base-retry head= base= -->` written BEFORE the PUT (no record, no update), `<!-- smart-merge:base-retry-spent head= -->`. An unreadable cap, or one above 30 (the most merges the count walks; a walk that runs out counts as spent), refuses only this feature (named, red pass). A failed or raced update still SPENDS that head's retry: the marker is written before the PUT by design, so a retry is never repeated unrecorded. Reads are bounded: `base-retry-max-per-pass` (default 3) candidates get main's runs read per pass, lowest PR number first, the rest named in the summary; one the record settles reads only its comments. THE NO-AUTHOR CASE (`base-retry-bot-authors`, exact logins of Bot accounts; the reusable workflow passes `renovate` since v1.43.0 when cap > 0 and a queue runs, the library default stays empty = off; set while the cap or max-in-flight is 0 = a `::warning::`): a PR by a listed author (nobody will push its fix) is retried WITHOUT red-at-base evidence when main is green on every failing check now and moved after the PR's red run finished -- the date-dependent test main's runs predate; a check main never runs (a review verdict) still holds it; same markers and cap. Not covered elsewhere: GitHub's merge queue needs an org, Renovate is `rebaseWhen: conflicted` in queue repos, the heal ladder fixes the PR's own code |
| `ci_kit/pr_token.py` + `.github/actions/pr-token/action.yml` | THE PUSH TOKEN (v1.37.0): every token job (pr-autoupdate, merge-train, ci-green, pr-heal's claim/push/report) gets its token from ONE composite that mints a GitHub App installation token (`actions/create-github-app-token`, SHA-pinned; this repo only, the job's permissions only) or falls back to the PAT, staged by `token-source` / the caller's `PR_TOKEN_SOURCE` variable (default `pat`), and names the acting identity (`CI_KIT_TOKEN_LOGIN`) plus the other configured one (`CI_KIT_TRUSTED_LOGINS`) to base-retry and the merge train, whose markers and trains survive a flip either way. Every run records `pr-token: source=... mode=... reason=...` (see "The push token" below) |
| `ci_kit/pr_ladder.py` | THE HEAL LADDER (v1.33.0, pr-autoupdate's `heal: true`): an armed PR that goes red or conflicts heals itself, cheapest tier first. 0 a clean merge (the updater) / a machinery rerun (pr_heal); 1 a lockfile-only conflict relocked (uv, npm, pnpm, NuGet; built in); 2 a conflict or red confined to GENERATED files regenerated from the caller's `regen` map, and a release-binary `Pin` whose version Renovate moved gets its sha256 table recomputed (`ci_kit/pin_rehash.py`); 3 Claude Code in Actions (`ci_kit/pr_fix.py`) on the SEALED pool for a judgment red or a real conflict -- at most once per head, `heal-claude-per-pass` / `-per-pr` budgets, never on a Renovate major, an `autoland-off`, unarmed, draft, fork or database-group PR, never a test edit (it declines; the pusher refuses); 4 ONE ntfy page per head with the PR, the failing check and what was tried. A conflicted Renovate PR (armed = its body says `Automerge: Enabled`) gets its rebase box ticked instead: Renovate rebuilds its own branch. Every dispatch writes `<!-- smart-merge:heal head= mode= claude= at= -->` BEFORE it runs (the once-per-head budget), the heal job `<!-- smart-merge:healed head= tier= result= -->`, a page `<!-- smart-merge:paged head= why= -->`, and tier 3 `<!-- smart-merge:attempt head= tier=claude run= -->` from a token-only `claim` job BEFORE Claude runs (a second run or a re-run at the head is refused); a dispatch with no record after `heal-stale-minutes` is paged by the next pass (and turns it red). With the ladder on, a judgment red gets the ladder instead of pr_heal's one comment and a conflict the ladder instead of the page; machinery is still pr_heal's. A tier 3 that CANNOT START (v1.46.0) is the runner's fault, not the PR's: `pr_fix.sandbox_check` asks the runner for the sandbox the CLI's env scrub demands (bubblewrap + socat on PATH, and a bwrap that can create a user and pid namespace) BEFORE the CLI runs, `pr_fix.judge` reads the CLI's own `could not start` and a run whose every Bash call the sandbox refused (a call the permission rules denied is left out of "every"; a red command that printed its own `bwrap: ` line is not a refusal), a CLI that is not installed (`exit=not-run`) is the same kind, and the record says `result=cannot-start` with a page titled `<repo>: heal tier 3 CANNOT START (the runner, every PR)` instead of `#N: heal ladder could not fix it` (17 attempts on 15 trade-data PRs, 11-15..26, were each recorded as that PR's `failed`). A cannot-start head keeps its once-per-head dispatch and does not count toward `heal-claude-per-pr`: the next head, on a runner that works, still gets tier 3 |
| `ci_kit/pr_regen.py` / `ci_kit/pr_apply.py` | tiers 1-2 and the ONE pusher. `pr_regen` merges the base with `--no-commit` (a `.gitattributes` `merge=union` file is resolved by git itself -- only for truly append-only files, never a shrink-only baseline: union re-adds a deleted line), takes main's side of each covered file, runs the generators and saves the files they changed; a write outside the map, a marker, a grown `shrink: true` baseline or a failed generator hands the PR to tier 3. `pr_apply` runs on a fresh SECURE-pool runner with the push token and no PR code: it re-checks the head, redoes the merge (git's conflicted set must equal the tier's), re-checks SCOPE (regen: generated/lockfile/sha256-only-pin files; claude: the PR's own + conflicted + generated files, no test, nothing under `.github/`, `max-fix-lines`), commits a merge or one fix commit as `github-actions[bot]` (Renovate's `gitIgnoredAuthors`) and pushes fast-forward only. Since v1.38.0 it never writes THROUGH a symlink (`unsafe_destination`): a destination tracked as a link, a linked parent directory, or a path resolving outside the repository or into `.git/` refuses the whole change, red (`destination symlink: ...`) |
| `ci_kit/pr_relock.py` | THE DEPENDENCY-BOT RELOCK (v1.38.0, `.github/workflows/relock.yml`): a bot PR whose manifest moved without its lockfile (a regex manager's pin bump) gets the lockfile pushed to it, through pr_regen + pr_apply -- the heal ladder's one way to regenerate a lockfile with no secret and push it with a token -- plus what a relock alone needs: NO merge of the base; the lockfile set is the caller's `lockfiles` input (tracked, a `LOCKFILES` kind); the event gate (`pull_request`, same repo, `branch-prefix`) in both jobs; a STRICT artifact check on the push side (exactly `manifest.json` + `files/<p>` for p in the push job's own copy of the input, regular files, no delete, no merge claim); NO LOOP (a relock needed on top of its own relock commit is refused). Commits as `github-actions[bot]`, subject `chore(lock): relock <paths>` |
| `ci_kit/pr_sched.py` | THE PR SCHEDULER (v1.29.0; policy `ci_kit/pr_queue.py` `head`/`schedule`/`admit`, pure). The queue HEAD (lowest-numbered armed PR) goes first on the caller's one-runner pools, ORDER and TIMING only -- nothing is skipped, a parked run is red until re-run. pr-autoupdate's `--scarce-pools '{"home-sealed":1,"home-heavy":1}'` makes each run: park (label `queue:parked`, then cancel) a non-head run queued on a pool the head waits for, or one that started there under `--preempt-after-min` (3) ago; re-run parked work in landing order (the head's at once, a non-head's one per pass when no scarce pool has a queued job); update a BEHIND head as soon as its review verdict is recorded; page a later PR merged past an in-flight head; and go red (`queue-head-starved`) when the head waited `--head-stall-min` (15) while a non-head held its pool; the next parked non-head waiting past twice that is named `PARKED STARVED` in every pass and paged ONCE per head (marker `<!-- queue:parked-starved sha=... -->`, ntfy's recent() when the marker is unreadable), never red (v1.35.0: on a busy day every red pass was one standing park). The review gate's `queue-priority` admission (`python -m ci_kit.pr_admit`) parks a later PR's full review while the head needs the sealed runner; it fails OPEN (a failed look reviews) and never parks a 4th attempt. `redclass` reads `review deferred` (a job until v1.35.0, a step of `verdict` since) as PARKED: pr_heal leaves it to the scheduler. X=3/N=15 from trade-data 11-07..11-14 (sealed wait p90 12.7 min, run p50 1.7; heavy wait p90 36.5, run p50 6.1; 85/90 sealed and 196/245 heavy inversions were inside 5 min of the holder starting) HEAD FAIL-FAST (v1.45.0, `review_young`): a BEHIND head whose review has spent under `preempt-after-minutes` of model time is updated at once instead of when the verdict records, so its one full CI run is at a head that can merge; THE HEAD HOLDS A SLOT (`pr_queue.reserve`, with the queue hold): a stale head that can land takes a `max-in-flight` slot, so the next PR is not updated beside it; THE HEAD'S VERDICT IS WAITED FOR (`pr_autoupdate._await_head`): past that bound the pass's tail re-reads the review every 15 s, up to `head-verdict-wait-seconds`, and updates the head itself when the verdict is on record, on a fresh read of the PR (tests/test_pr_head_fail_fast.py) |
| `ci_kit/pr_hold.py` | THE QUEUE HOLD (v1.30.0): commit status `queue/position` -- success on the queue HEAD and a merge train's PR, pending on every other same-repo open PR (Renovate's included: it lands in number order once armed, and `ci_kit/pr_renovate.py` arms each automerge one) -- posted FIRST in every pr-autoupdate pass (`queue-hold: true`). Branch protection requiring it makes an out-of-turn auto-merge impossible (td#3083 landed past head #3082, 2026-11-14). The head is the lowest armed PR that CAN land (`pr_queue.blocked`: not DIRTY, not stacked, no required check failing or cancelled unless parked/machinery and behind main) -- the same head for the updater and the scheduler, so a red head never freezes main. Order only: no check removed. A refused post on the head, or a head holding success unmerged twice `head-stall-minutes` after its last check (a required check that never reported), is red, a marker comment on the first pass, ONE ntfy page on the second. Token: Commit statuses RW. Turn it on BEFORE requiring the context, and keep `queue/position` out of `required-checks`. THE TRAIN OWNS THE LANE (v1.35.0): while a LIVE merge train is open the head is held too (pending, `held for merge train #M`), so main cannot move under the train (measured: every unlanded train in a day closed `stale: main moved`, the head having landed beside them); a red, BEHIND/DIRTY or older-than-`train-stale-minutes` train releases the lane AND pr-autoupdate's update hold, each named in the summary (`hold: lane HELD/RELEASED ...`). The train never waits on the head, so no deadlock |
| `ci_kit/pr_list.py` | library: the updater's open-PR list as ONE narrow GraphQL query (check-run name/status/conclusion, status context/state, nothing more), returned in `gh pr list --json` shape. `gh pr list --json statusCheckRollup` also selects each check's workflow run, which a token without `actions: read` is refused (v1.7.0 still was, live 2026-10-24). `settle()` (v1.43.0): an ARMED same-repo PR whose `mergeStateStatus` reads UNKNOWN (GitHub computes it lazily; the updater fires right after a base push) is re-read up to `CI_KIT_UNKNOWN_TRIES` (2) times `CI_KIT_UNKNOWN_WAIT_S` (5s) apart before pr_queue plans it as behind -- a non-strict base never reports BEHIND (live plans#134: behind_by=2, green, CLEAN), so planning UNKNOWN as behind merged main into PRs that could land. Still UNKNOWN after that = planned as before and named in the summary; a failed re-read keeps the last good rows, named; a garbage knob falls back to its default |
| `ci_kit/merge_train/` | `ci-merge-train` (v1.25.0; run cancel v1.26.0): batch the armed, reviewed, green-but-behind PRs into ONE `train/<run>` PR -- one squash commit per PR (title `(#N)`, body, co-authors, `Merge-Train-PR`/`-Head` trailers), lowest PR number first, 2..8 -- CI runs once; a green train is NOT auto-merged -- the pass re-checks every rider at land time (open, armed, not a draft, no `autoland-off`, head unchanged, review still passing on this base) and only then lands it by REBASE (per-PR history kept), each constituent closed `landed-via-train` with its main sha; red ejects the newer half (n>=4, else all) back to individual auto-merge, unless the red is machinery (a check that never started, a BLIND gate): then a bounded retry, never an ejection; stale/stuck recomposes (stale is never held against a rider; a rider released from 2 STUCK trains at one head is held until its head changes). A rider boards only if its review passed against THIS base (its passing run was created after the PR's last base change, from the PR timeline's `filteredCount` + `BaseRefChangedEvent`; a timeline GitHub will not answer skips that rider, BLIND, never the pass) and its diff lands with the context the review read; a rider skipped for moved context is labelled `train-needs-update`, which pr-autoupdate updates even while a train is open. The pass acts only on a train it composed (a composition record in the PR body, checked against its own run). The review gate verifies a train instead of re-reviewing it (`ci_kit/review/train.py`, same checks plus the record). Every action is a PR comment (riders carry `rode`/`released`/`landed` markers) + the run summary; a stuck train, a retry, a hold, a green train GitHub would not merge turn the run red. Every train close (stale, stuck, red, land-time recheck, superseded) goes through ONE `ops.close_train`, which also cancels the train branch's unfinished workflow runs (v1.26.0; a closed train's CI held trade-data's one heavy runner ~25 min, 2026-11-12) -- never the train workflow's own runs, never an open PR's; a cancel GitHub refuses turns the run red. THE LANE (v1.35.0): before it pushes a train the composer holds the queue head for it (`queue/position` pending), then re-reads main; main moved = no train opened and the lane given back (a lock-maintenance PR once landed during a 6-minute compose); the lane goes back on every exit without a train, a raise included, and success only to a PR still the head; a pass that ends with no train open gives a held PR what the queue hold would post at once -- success only if it is still the head (`ops.release_lane`), rather than on pr-autoupdate's next pass. Free: GitHub's merge queue needs an org, Mergify/Trunk/Aviator are paid |
| `ci_kit/train_kick.py` | WAKE THE TRAIN FROM THE QUEUE (v1.34.0): with pr-autoupdate's `train-workflow` set, each pass dispatches the caller's merge-train workflow only when a pass would find work -- 2+ candidates by the train's OWN rule (`merge_train.policy.candidates`), no armed PR into the base landing on its own auto-merge (CLEAN; a candidate or not, v1.35.0), no train open, no pass queued or running, and a candidate ready (last required check, or arming) after the newest pass was created (that is the dedup; no state kept). A PASS is a `schedule` or `workflow_dispatch` run, never skipped or cancelled: the train's workflow_run/label runs are mostly job-`if:` skips (9 of 10 on trade-data) that judged nothing. The train's cron becomes an hourly backstop: trade-data's 20-min cron found work on 3 of 52 passes (2026-11-14), each a secure-pool job. An unreadable run list or a refused dispatch is a red run naming Actions: Read and write; every decision is a `merge train:` summary line |
| `ci_kit/doomed_runs.py` | CANCEL WHAT CANNOT MERGE AND NOTHING READS (v1.34.0, pr-autoupdate `cancel-doomed-runs`, default on): cancels the queued/running `pull_request` runs at (a) the head the queue just replaced, (b) an armed DIRTY head on a STRICT base, (c) an armed BEHIND head on a strict base ONLY in a repo with no merge train (a train takes BEHIND PRs green at their head; v1.48.0: a `merge-train` workflow that has NEVER RUN -- a reusable `workflow_call` definition, ci-kit's own -- is no train) and only after `compare` proves it behind. Strict is read (classic or ruleset), never assumed. Never the review workflow, never a non-PR event, never an unarmed/train/riding/parked PR. A cancelled check blocks the merge like a missing one -- nothing reads passed. Each cancel is a summary line; a refused cancel or read is a red pass; no Actions RW or no Administration read is one warning naming the grant (local-mcp#499, 11-14) |
| `ci_kit/queue_wait.py` | `ci-queue-wait`: p90 time one workflow's runs waited for a runner over the last N hours, exit 1 over `--max-p90`. Too few waits in `--hours` doubles the window up to `--widen-to-hours` (default 168) and gates the widened sample; NO completed run at the cap is `IDLE`, exit 0, only while the filter's newest completed run is under `--stale-days` (30) old -- a filter that never matched or went stale, runs with nothing to measure, a thin sample within the limits and any exception stay exit 2; a thin sample over a limit exits 1 (v1.44.0: a quiet Sunday read red). The number the treadmill above hid |
| `ci_kit/pin_drift.py` | `ci-pin-drift`: every `<kit>@vX.Y.Z` / `?tag=vX.Y.Z` / `<kit>/releases/download/vX.Y.Z/` / PEP 508 `<kit>[extras]==X.Y.Z` (an index pin, PEP 503 name, read as tag `vX.Y.Z`; v1.12.0) pin in the given files is the newest release tag on `--remote`, else exit 1. A kit fix that never reaches a consumer must not fail silently. `--grace-hours` (default 24 since v1.21.0, 48 since v1.35.0 for the daily kit window): a lag whose EVERY missing release is dated and younger than that (by DATE, not version) is printed PENDING and exits 0 -- Renovate's bump, not yet landed -- so a daily cron no longer goes red the day after every kit release; an unreadable or future release date, a private kit (no credential in the date fetch) and a phantom pin get no grace; `0` = strict. A remote read (`ls-remote`, the date fetch) is tried three times at 40s each (v1.47.0): a stalled connection is a printed failed try and, when a later try answers, a `::warning::`, not a red "could not look"; each date-fetch try uses its own bare repo. |
| `ci_kit/manual_lint.py` | `ci-manual-lint`: every path an agent manual (`CLAUDE.md`) names **resolves today** in `git ls-files`. The question `ci-doc-lint` deliberately cannot answer -- a path that was never real -- asked only of the file every session loads. Quiet by SHAPE (routes, flags, prose with slashes, image tags, formulas), not by allowlist; a sibling-repo pointer is checked when that checkout is present and silent when not. Optional: stale migration ranges, line budget, empty placeholder packages, reference-doc dirs + freshness stamp, a total index |
| `ci_kit/guard_selfproof.py` | `ci-guard-selfproof`: every guard script proves it can FAIL -- `selftest() -> list[str]` (or `--selftest` for a JS guard) rejecting a known-bad input and accepting a known-good one. A guard without one is a shrink-only baseline entry; a NEW one fails. A selftest returning an int, raising, or failing to import is a failure by name |
| `ci_kit/env_reads.py` | `ci-env-reads`: a shell script pulls a key out of a `.env` FILE with a hand pipeline (`sed -n 's/^K=//p' FILE`, `grep '^K=' FILE \| cut -d= -f2-`, `awk -F= '/^K=/' FILE`) instead of the repo's reader. Hand copies disagreed in RESULTS (first vs last duplicate, quotes kept, a value cut at `#`, `$$` left doubled where compose yields `$`). Reading from a pipe (`docker inspect ... \| sed`) is quiet on purpose. Per-file count ratchet (`--baseline`, default `ci/env_reads_baseline.txt`), `--exempt` the reader itself, waiver `# env-read-ok: <why>` |
| `ci_kit/line_cap.py` | `ci-line-cap` (v1.49.0, moved from trade-data): a python module over the line cap (`--cap`, default 400, PHYSICAL lines) may not appear, and one already over may not grow -- a per-file `RatchetCount` (`--baseline`, default `ci/line_cap_baseline.txt`; `--write-baseline` keeps the file's own `#` header). Every `--dir DIR:FLOOR` is read against its OWN floor (a total hides a tree that read nothing), and with any `--unruled DIR:WHY` tracked python under a top-level dir neither ruled nor excused is exit 2 (`.` = repo-root files) |
| `ci_kit/function_clones.py` | `ci-function-clones` (v1.49.0, moved from trade-data): a file may not ADD a function that is an exact structural twin (normalised AST: names/args to `_`, constants to their type, docstring dropped, attributes KEPT; `--min-nodes` 40) of one in ANOTHER file. Per-file count ratchet; a copy into a new file names both files. The rename-proof sibling of `ci-dup-code`'s token windows |
| `ci_kit/blind_parse.py` | `ci-blind-parse` (v1.49.0, moved from trade-data; the other half of `astread`): every `except SyntaxError` (tuple, subclass) handler must END in a raise / `sys.exit` / a never-returning call (`astread.blind`, plus `--noreturn NAME`) -- `continue`/`return`/`pass` answer a parse failure with a clean file's value. Structural, on an AST (a MENTION is not a handler); waiver `# blind-ok: <why>` (a bare one is a finding); fewer than `--min-handlers` handlers is exit 2 (the rule cannot fire). CI runs it on ci-kit itself |
| `ci_kit/quarantine.py` | `ci-quarantine` (v1.49.0, moved from trade-data): every `@pytest.mark.quarantine` (`--mark`) carries literal `reason=`, `issue=` (`#N`, `prefix#N`, `owner/repo#N`, an issues URL) and `until=YYYY-MM-DD` not past and at most `--max-days` (60) out; `**kwargs`, positional or non-literal args are findings. 'Today' is `--tz` (default UTC; an unknown zone is exit 2, never a guessed date) or `--today`; `--summary PATH` appends the mark table (a nightly's step summary) |
| `ci_kit/cog_gate.py` | `ci-cog` (v1.50.0, moved from trade-data; **the `cog` extra**: cogapp): every [cog](https://cog.readthedocs.io/)-generated fact in a TRACKED `--suffix` file (default `*.md`, discovered by `git ls-files`) carrying a `--marker` block matches what its generator renders now -- `cog --check` in-process (`--include` = cog's `-I`, `--rerun` names the fix in the STALE message, `--write` regenerates). Exit 1 = cog's stale; a generator that RAISES, or no tracked file carrying a block, is exit 2 |
| `ci_kit/standins.py` | `ci-test-standins` (v1.50.0, moved from trade-data): a test may not ADD a SIGNATURE-BLIND stand-in -- a `lambda` or a same-file def installed over a `--package` (required) callable by `monkeypatch.setattr` / `mock.patch` / `patch.object` carries its own signature, so a new keyword on the real function passes PR CI and raises later. The fix is `mock.create_autospec(real, side_effect=fake)`. Per-file `RatchetCount` (`--baseline`, default `ci/test_standins_baseline.txt`); waiver `# standin-ok: <why>` (a bare tag is reported) |
| `ci_kit/guards_have_tests.py` | `ci-guards-have-tests` (v1.50.0, moved from trade-data; with `ci_kit/testrefs.py`): every guard script (`--guards` dir, `--prefix`/`--suffix`) is NAMED by a tracked test outside prose (a docstring or comment mention is not a test), and a guard whose only tests carry a `--deselected-mark` (never run in the PR suite) is reported. Shrink-only `RatchetSet` (`ci/guards_without_tests.txt`) |
| `ci_kit/untested_modules.py` | `ci-untested-modules` (v1.50.0, moved from trade-data; with `ci_kit/testrefs.py`): a source module under a `--dir` that no tracked test under `--tests` imports or names may not appear (dotted names resolved against the tree; `__init__`/protobuf stubs skipped; `--exclude-part` drops a path segment). Shrink-only `RatchetSet` (`ci/untested_modules_baseline.txt`); waiver `# untested-ok: <why>` in the first 10 lines |
| `ci_kit/http_clients.py` | `ci-http-clients` (v1.50.0, moved from trade-data): a hand-built HTTP client outside the repo's seam (`--seam PATH`, where timeouts/redaction live) may not grow, per file -- an `httpx.Client` / `AsyncClient` call (aliases followed), an httpx module helper (`httpx.get(` opens its own client), or an import of `requests` / `urllib.request` / `urllib3`. AST, so a docstring is never a client. `--use TEXT` is the fix the finding names. Per-file `RatchetCount` (`ci/http_clients_baseline.txt`) |
| `ci_kit/sql/placement.py` | `ci-sql-placement` (v1.50.0, moved from trade-data; the `sql` extra): SQL outside the repository layer may not grow -- parsed INSERT/UPDATE/DELETE/TRUNCATE statements (through `sqlast.module_sites`) + sqlalchemy `text(` calls, per file, minus `--owner` / `--exempt` path prefixes (`*` = one segment). Per-file `RatchetCount` (`ci/sql_placement_baseline.txt`) |
| `ci_kit/image_pr_filter.py` | `ci-image-pr-filter` (v1.50.0, moved from trade-data; the `workflow` extra; with `ci_kit/dockerfile.py`): every tracked FILE the `--dockerfile` COPYs/ADDs by name is listed in the `--workflow`'s dorny/paths-filter `--filter` key, so a PR renaming it cannot get a green "nothing to build". A COPY of a path the repo does not hold is a finding; no filter key or no COPY is exit 2 |
| `ci_kit/migration_open_prs.py` | `ci-migration-open-prs` (v1.50.0, moved from trade-data; stdlib HTTP): a numbered migration (`--dir`, `--pattern` with one group) this branch ADDS may not reuse a number on `--base` or one an OLDER open, non-draft PR adds -- ONE GraphQL call over every open PR (`--repo`, else `$GITHUB_REPOSITORY`; token `GH_TOKEN`/`GITHUB_TOKEN`/`gh auth token`). `--prs-json` replays a saved list. No token / unreachable = exit 2; so is a pattern that matches nothing on the base or the branch (a repo with no migrations yet included -- a mistyped `--dir` must not pass) |
| `ci_kit/dup_code.py` | `ci-dup-code`: **the same code written twice** fails the PR. [jscpd](https://github.com/kucherenko/jscpd) 5.4.2 (pinned release binary, sha256-verified, cached under `CI_KIT_CACHE_DIR`; no node) over code formats (python, bash, powershell, csharp, js/ts, sql, yaml, ...; json/md/toml are noise, not code) at **70 tokens / 7 lines** -- measured 2026-10-31 on three live repos: below that the hits are 5-line test prologues, above it they are copied helpers and lab-script twins, the complaint itself. jscpd's own fingerprint baseline (`ci/jscpd-baseline.json`, `--write-baseline`): a line shift or a rename is not new, an edit INSIDE a frozen clone splits it into new clones (consolidate, not re-freeze), and `--write-baseline` **refuses to grow**. A clone counts only between two `git ls-files` files (CI == laptop). The verdict is read from the JSON report, never from jscpd's exit code (0 on a missing path and an empty scan): no report or zero files analyzed is **exit 2**. Chosen over pylint `duplicate-code`: one language, no stable clone identity to baseline. Kit ignores: worktrees, venvs, `**/migrations/**`, fixtures/testdata/golden/snapshots, lockfiles, minified; add `--ignore GLOB` / `--format` |
| `ci_kit/api_diff.py` | `ci-api-diff`: a committed OpenAPI contract may **grow, never break**. [oasdiff](https://github.com/oasdiff/oasdiff) 1.35.0 (pinned release binary like jscpd) classifies the spec at `git merge-base <--base-ref> HEAD` against the working-tree file (`--path`, default `docs/api/openapi.json`) as ERR / WARN / INFO; any ERR (a route removed, a response field dropped, a request parameter made required) is **exit 1**. The verdict is parsed from `oasdiff changelog --format json`, never its exit code; no merge base, an unparseable spec or no binary is **exit 2**; a spec NEW at the merge base cannot break (0, said so). A deliberate break is a line in oasdiff's own `--err-ignore FILE`, committed in the breaking PR -- there is no skip flag. `--base A --revision B` compares two files. Library: `changes()` (classified), `structural()` (what moved by operation/component, incl. description-only edits the changelog does not classify) |
| `ci_kit/ci_gate.py` | `ci-gate-lint` (v1.13.0, the `workflow` extra: PyYAML): **a job that can SKIP must funnel into one always-running aggregate.** GitHub counts a skipped job as a passing required check and lets auto-merge through: trade-infra#806 (2026-11-06) merged with four required jobs skipped after its paths-filter `detect` job died on runner DNS. The fix is one `if: always()` job running [re-actors/alls-green](https://github.com/re-actors/alls-green) over `${{ toJSON(needs) }}`, needing every job, as the ONLY required check; this gate keeps that shape from rotting: a job with `needs:` and no aggregate, more than one, an aggregate that is not `always()` or judges something other than `toJSON(needs)`, a job outside its `needs` (the aggregate does not judge it: protection must name it, or it joins `needs`), and an aggregate on a self-hosted or expression runner with no `setup-python` before it (alls-green runs `python -m`; a python3-only image exits 127), and an `allowed-skips` entry (JSON list or comma-separated, as alls-green reads it) that is an upstream (`detect`), has no `if:`, or names no job. `--workflow` (repeatable, default `.github/workflows/ci.yml`); a missing/unparseable/jobless workflow is exit 2. Branch protection naming the aggregate lives outside the checkout -- an estate audit reads that |
| `ci_kit/version_tag.py` | `ci-version-tag` (v1.20.0): in a **version-first** repo (the release PR moves `version` in pyproject.toml) every version bump on the default branch's first-parent history gets its `vX.Y.Z` tag at the bump commit, and every tag its release. An untagged bump is tagged through the API, verified, and `--release-workflow` is dispatched on it (a GITHUB_TOKEN tag fires no push workflow; `workflow_dispatch` is the event that token may start, so the release workflow must read `github.ref_type == 'tag'`, never `event_name == 'push'`). A tag missing its GitHub release (`--github-release`) or its PyPI files (`--pypi NAME`): no run = dispatch, a run in flight = pending, a run that FINISHED and left it missing = exit 1 (a failed publish is rerun by a person, never re-dispatched blind). `.devN`/`+local` and versions below `--since` are skipped; a tag on another commit is printed, never moved. Writes only on the default branch (elsewhere = dry run). Built after llm-ports 1.19.0 merged and was never tagged, with nothing red |
| `ci_kit/ci_green.py` | `ci-green` (v1.22.0): fast-forward a `ci-green` branch to the commit CI just PASSED on the default branch, so a box that BUILDS from its checkout follows `origin/ci-green` instead of `origin/main` and never deploys a merge whose push CI failed (edge-host deploy-reconcile, 2026-11-09). Absent = create, ancestor = advance NON-force, newer already there = no-op (`older`), diverged or not in the default branch's history = exit 1 and never forced; refuses the default branch, `main`, `master`; `--dry-run`; exit 2 when it could not look |
| `ci_kit/release_bin.py` | library: a third-party CLI pinned by **version + sha256 per platform**, fetched once into `$CI_KIT_CACHE_DIR` (`<name>-<version>-<asset>/`), `$CI_KIT_<NAME>` overrides; a 5xx or network error is retried 3x with backoff (a 4xx is not), the binary is written atomically (a shared cache is safe under concurrent jobs), and any failure to obtain it is still exit 2. jscpd and oasdiff state only a `Pin`. Each version line carries `# renovate: datasource=github-releases depName=...` (`renovate/default.json5` reads it): Renovate moves the VERSION, so its PR fails the selftest on the sha256 until the hashes move in the same PR -- a new binary never runs on an unreviewed digest |
| `ci_kit/git_ssh.sh` | a PRIVATE GitHub git dependency in a runner install (the review gate, v1.24.0), three phases = three steps: `load` (the ONLY step holding `CI_KIT_GIT_SSH_KEY`; no PR code runs there) starts an `ssh-agent`, adds the key from stdin, writes GitHub's published host keys to a file of its own; `. git_ssh.sh route` in the install step (no key in its env: a build backend could read it from the parent shell's `/proc/<pid>/environ` even after `unset`) rewrites only `CI_KIT_GIT_SSH_REPOS` (`owner/name`) https -> ssh through env-scoped `GIT_CONFIG_COUNT` with `StrictHostKeyChecking=yes`; `kill` (`always()`; a zombie counts as dead) stops the agent before the step that runs the PR's tests. The agent starts without the key in its environment, but the key is in its MEMORY while Install runs: code with root (a hosted runner's passwordless sudo) can dump it, so the key must be read-only and scoped to one repo. webfactory/ssh-agent was not used: its agent lives to the job's end. **Several private repos, one deploy key each (v1.28.0)**: a deploy key reads ONE repo and GitHub takes the first key an agent offers, so `CI_KIT_GIT_SSH_KEY` may hold N concatenated keys for the N repos of `CI_KIT_GIT_SSH_REPOS`, paired by position (a `|` block of N secrets); each key gets its own agent and a host alias `ci-kit.<owner>.<name>` (HostName github.com, HostKeyAlias github.com, IdentityAgent = its agent) in a config load writes (`ssh-config=`, which `route` reads from `CI_KIT_GIT_SSH_CONFIG`), plus `agents=` (JSON repo -> socket, for a BuildKit `ssh:` id per repo); `pid=` lists every agent and `kill` stops them all. One key with several repos is the old shape unchanged; any other count, the same key twice, or a route for a repo load never named = red Repos named with an EMPTY key (a fork/Dependabot PR, an unmapped secret), a key with no repo, a malformed repo or a key `ssh-add` refuses = `::error::` and exit 1 before the install (`tests/test_git_ssh_must_fire.py`) The routed `GIT_SSH_COMMAND` ends a stalled connection (v1.47.0: `ConnectTimeout=20`, `ServerAliveInterval=15` x 3). Since v1.50.1 it runs through `ci_kit/ssh_retry.sh`, which re-runs ONLY a connection that never came up: an ssh exit 255 whose stderr holds one of OpenSSH's connect-phase lines, anchored (`ssh: connect to host ... port N: ...`, `ssh: Could not resolve hostname`, `Connection timed out during banner exchange`, `kex_exchange_identification:`), before any of git's stdin was read. Each retry and give-up is also appended to `$GITHUB_STEP_SUMMARY` (uv drops git's stderr when the fetch then succeeds), and a signal to the wrapper reaches ssh with its stderr still printed. `CI_KIT_SSH_TRIES` (default 3) bounds the attempts and the pause is n x `CI_KIT_SSH_PAUSE` s (default 2), so a host that is really down costs a connection up to 66 s, not 20. Auth, host-key and mid-session failures, and the remote command's own exit, are returned on the first try (`tests/test_ssh_retry_must_fire.py`). Added after trade-data#3260's review gate went BLIND on one banner timeout, 2026-11-28. |
| `ci_kit/ratchet.py` | library: `RatchetSet` / `RatchetCount`, the shrink-only baseline every ratchet lint shares; `kept_header()` (v1.49.0) = a re-freeze keeps the caller's own baseline header |
| `ci_kit/runners/` | `ci-runners` (v1.51.0, **the `runners` extra**: pyyaml): self-hosted GitHub runner CAPACITY as a library -- the capacity file's schema, the host / guest / runner-VM budget invariants (measured `peak_mb` against real RAM, floors, ceilings, the live room), one JSON render per runner block for a supervisor to read with jq, and the runtime admission rule (`alloc.jq`, shipped as package data, plus its Python twin `allocate()`, pinned equal by a parity test). The domain's specifics live in a KIT file: blocks, instances, prefixes, tier labels, variables, render paths, share duties. See [Self-hosted runner capacity](#self-hosted-runner-capacity-ci-runners) |
| `ci_kit/discover.py` | library: `tracked()` = `git ls-files`, never a filesystem walk (a walk sees your untracked files, CI does not; a checkout under `.claude/worktrees/` once blinded three gates); `require_scanned()` = zero files is exit 2; `count_by_dir()` + `require_scanned_each()` (v1.9.0) = a floor PER ruled dir, so one tree going dark cannot hide under another's hundreds; two NAMED file lists (v1.14.0): `tracked_each()` = absolute paths, every dir contributes or exit 2 naming `gate[dir]` (the sql gates' `iter_files`), and `tracked_or_empty()` = absolute paths, [] when no scan dir exists so the gate exits 2 in its own words (a git failure is still 2) |
| `ci_kit/site_lint.py` | library (v1.49.0: `gate_parser` / `gate_root` / `dir_floors` / `selftest_exit`, the flags a python-tree gate shares: `@FILE`, `--root`, `--dir DIR:FLOOR` per-dir floors, `--selftest`, `--baseline` + `--write-baseline`): the ratchet gate's scan / fixture selftest / CLI -- a per-file count (`RatchetCount`) or a set of keys (`RatchetSet`), with the gate's own words through optional hooks (`header`, `describe`, `ok`, `written`, and `hard` for findings no baseline grants) -- so a second ratchet lint is a predicate, not a copy. `check` is the same CLI for a **pass/fail** gate with no baseline (`--selftest`, the gate's own mode flags, findings -> 1, could not look -> 2, an optional scan floor; voice via `header`/`describe`/`advice`/`ok`, or `report` for output that fits no template). v1.14.0: `waived_lines(text, waiver)` = an inline waiver must carry a reason (>= 8 chars, else it is BARE and reported, not honoured); `repo_on_path(root, marker)` = put the checkout on `sys.path` only when it holds the marker file, so a must-fire fixture repo keeps the installed package |
| `ci_kit/astread.py` | library: read + `ast.parse` a source file, or **exit 2** naming the file and the gate (`blind()`); BOM-tolerant (`utf-8-sig`), never `errors="replace"`. A parse failure answered with `continue` is byte-identical to "read and clean" |
| `ci_kit/testrefs.py` | library (v1.50.0): which names a test file mentions OUTSIDE prose (docstrings and comments are not references) and which repo modules its dotted names resolve to -- shared by `ci-guards-have-tests` and `ci-untested-modules` |
| `ci_kit/dockerfile.py` | library (v1.50.0): a Dockerfile's COPY/ADD instructions as token lists (continuations joined, comments inside them dropped, JSON and shell forms, `--from=` stages skipped), stdlib only |
| `ci_kit/sql/` | **the `sql` extra** (`ci-kit[sql]`, adds sqlglot -- the kit's only dependency, and only here): gates that read SQL embedded in python source **parsed** (sqlglot, postgres dialect), so a docstring, a `-- comment` or a string literal that *mentions* SQL is never read as SQL, and a SQL value that will not parse is **exit 2**. `sqlast` / `sqllit` are the shared readers (statement heads: SELECT, INSERT, UPDATE, DELETE, WITH, CREATE ... AS, and since v1.9.0 TRUNCATE). Each gate keeps its detection here and takes its voice (gate name, re-run command, doc pointer, baseline paths) from a `Config`, so a repo that already had the gate keeps its output byte-identical. Since v1.5.0 every gate's `main` is a `site_lint.check` CLI: `--selftest` works under a wrapper's `Config`, a crash mid-scan is **exit 2** (DID NOT FINISH LOOKING) instead of a traceback, and `--write-baseline` / `--list` are its mode flags -- so a repo wrapper that hands its argv to `ci_kit.sql.<gate>.main` is on the scaffold too |
| `ci_kit/sql/casts.py` | `ci-sql-casts`: asyncpg bind/cast footguns -- `:x::type` (A), `CAST(:x AS timestamptz)` on a str (B, ratchet), `now() - :age` (C), `:x + interval` (D, ratchet) |
| `ci_kit/sql/pit_reads.py` | `ci-pit-reads`: SQL wall-clock reads (`now()`, `current_date`, ...) -- plausible numbers from the WRONG day under a replay clock; a per-file count ratchet |
| `ci_kit/sql/decimal_reads.py` | `ci-decimal-reads`: a NUMERIC column projected without a cast decodes as `Decimal`; ground truth = a `--cols` file of `schema.table.column` (missing or empty is exit 2) |
| `ci_kit/sql/phantom_columns.py` | `ci-phantom-columns`: a single-table statement naming a column its table lacks, against a `--schema` json built from a migrated database. Precision over recall |
| `ci_kit/sql/column_comments.py` | `ci-column-comments`: a migration column comment that declares a domain (magnitude, 0..1, enum, `>= 0`) with no CHECK enforcing it |
| `ci_kit/review/` | **the `review` extra** (`ci-kit[review]`: pydantic-ai-slim + mcp; v1.15.0, moved from trade-data): the pre-merge model review gate's code. `tier` decides who is reviewed and why (every code PR; a guard path, 3+ non-merge commits or the label are REASONS; a docs/lockfile-only diff -- `--no-renames`, `--not-prose` prefixes never prose -- skips the model call; the `REVIEW_GATE_AUTH` credential toggle; a fork head or a mode with no secret is BLIND/UNARMED and red), `tools` the four read-only tools (`read_file`, `grep`, `glob_files`, `run` on an allowlist of git/ruff/pytest, no secret in any subprocess env), `agent` (api transport, pydantic-ai) and `claude` + `mcp_server` (subscription transport, Claude Code CLI with no built-in tool and only those four), `verdict` the policy (SHIP / SHIP WITH NOTED RISK pass, DO NOT SHIP or a CONFIRMED blocker blocks, no parseable verdict is BLIND = red), `carry` the verdict carried over an unchanged own diff. v1.15.1: the review job sets `PYTHONSAFEPATH=1` (cleared for the caller's install and the tool runs), so a PR-added `ci_kit/` cannot shadow the gate that grades it (`tests/test_review_safepath_must_fire.py`). `tier`/`verdict`/`carry` are stdlib-only |
| `action.yml` | composite action: run one of the above against the caller's checkout, installed from the action's own download (no second ref, no credential) |
| `.github/workflows/doc-refs.yml` | reusable: the doc gate + its selftest, full history (`fetch-depth: 0` is load-bearing; a shallow clone is exit 2, not a blind pass) |
| `.github/workflows/dup-code.yml` | reusable: the duplicate-code gate + its selftest (no history needed; the caller commits `ci/jscpd-baseline.json`) |
| `.github/workflows/secret-argv.yml` | reusable: the secret-argv lint + its selftest |
| `.github/workflows/hidden-unicode.yml` | reusable: the hidden-Unicode gate + its selftest; `args` for `--binary-suffix` |
| `.github/workflows/declared-literal.yml` | reusable: the declared-literal gate + its selftest; `args` (required) is the caller's policy, normally `@ci/literals.args` |
| `.github/workflows/gitleaks.yml` | reusable: secret scan, `mode: tree / history / range` (`action` = `range`, the old name); every mode is `ci-gitleaks` (v1.17.0: gitleaks pinned by version + sha256 through release_bin, no docker, no gitleaks-action) and runs on the caller's home pool when its `CI_RUNNER` variable is set; `history` is HEAD's ancestry, never `--all` (a fixture on another open branch must not fail THIS PR), and zero commits scanned is exit 2; every `.gitleaksignore` gitleaks loads (`./`, `<source>/`, and `ignore-path` -- a file or a folder) is judged, and a dead entry (an absolute `/repo/` path, a malformed or trailing-comment fingerprint) is exit 2, so `ignore-path: ''` still checks the repo's own baseline |
| `.github/workflows/pr-autoupdate.yml` | reusable: the armed-PR updater; output `relock`. `train-workflow` / `train-min-batch` (v1.34.0) wake the caller's merge train (`ci_kit/train_kick.py`); `cancel-doomed-runs` (v1.34.0, default on) cancels the `pull_request` runs that cannot merge (`ci_kit/doomed_runs.py`). The header's caller template decides the event-only skips in the update job's `if:` -- NO separate gate job: on a single-use pool a gate costs a whole runner, as much as the pass it debounces (trade-data 11-14: 289 gates, 142 go). Runs on the caller's SECURE pool (`[self-hosted, home-secure]`, home-host VM 111, no docker socket) when its `CI_SECURE_RUNNER` variable is set (an on/off switch; labels are fixed here), reading the token from its `ci-secure` environment; hosted otherwise. Inputs `scarce-pools`, `preempt-after-minutes`, `head-stall-minutes` turn on the PR scheduler (`ci_kit/pr_sched.py`, v1.29.0); `queue-hold` turns on the queue hold (`ci_kit/pr_hold.py`, v1.30.0); `base-retry-cap` (v1.36.0, default 0 = off) bounds the base-driven retry (`ci_kit/pr_baseretry.py`); `heal` (+ `heal-claude-per-pass`, `heal-claude-per-pr`, `heal-per-pr`, `heal-stale-minutes`) turns on the heal ladder (`ci_kit/pr_ladder.py`, v1.32.0), output `heal` = the rows for the caller's `pr-heal.yml` matrix; with it on, drop the caller's own `relock` job (tier 1 relocks, and two pushers race) `head-fail-fast` (v1.45.0, default on) is the switch for the head's at-once update; list `synchronize` beside `auto_merge_enabled` in the caller's `pull_request_target` types, or a new head on an armed PR starts no pass; `head-verdict-wait-seconds` (240, 0 = off, clamped to 300: the job is `timeout-minutes: 10`) bounds the tail's wait for a head whose review is past the young bound. The wait HOLDS the updater's `secure-runner`, and the review gate's `verdict` job it waits for runs on the same pool: a pool that gives the repo ONE runner blocks its own verdict for the whole wait, so set it to 0 there (nothing refuses that configuration) |
| `.github/workflows/pr-heal.yml` | reusable (v1.33.0): the heal ladder's tiers 1-4 for ONE PR, called per row of pr-autoupdate's `heal` output (caller template in the header). Jobs: `plan` (secure pool, token-free: re-checks the head and arming, gathers the failed logs and the review findings), `regen` (the CI pool or hosted: PR code, no secret but an optional `git-ssh-key`), `claim` (secure pool, the token: tier 3's once-per-head attempt marker, written before Claude runs), `fix` (the SEALED pool or hosted: tier 3, the review gate's credential toggle and `--restricted` CLI, every token stripped from its env; bounded at `pr_fix.FIX_JOB_MIN` = 50 min = the CLI's 40 + 2 + 8 setup, and every pr-autoupdate pass names it while it holds the sealed runner: `TIER 3 IN FLIGHT on the sealed runner (run R, ...)`, v1.35.0), `push` (secure pool, the token, no PR code: `ci_kit/pr_apply.py`), `report` (secure pool: the `healed` record and the page). The SEALED runner must carry `bubblewrap` and `socat` and let its job create user namespaces (v1.46.0): the fix step sets `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`, under which the CLI runs every Bash command in a bwrap sandbox or not at all; a runner that cannot is `result` = `cannot-start`, decided before a model session is spent |
| `.github/workflows/relock.yml` | reusable (v1.38.0): `ci_kit/pr_relock.py` on a `pull_request` caller (see "Relocking a dependency bot's PR"). Jobs: `trigger` (RED for any other event or an empty `branch-prefix`, never a quiet skip), `lock` (HOSTED, PR code: the lock tool, no token, only an optional read-only `git-ssh-key` agent), `push` (hosted or the fixed secure labels, the token from `pr-token` scope `push`, no PR code). Output `result` |
| `.github/workflows/pr-autoupdate-self.yml` | ci-kit's OWN updater (2026-11-14): calls `pr-autoupdate.yml` from this checkout on push to main + `ci` completing, serial (`max-in-flight: 1`, required `test`, `gitleaks (secret scan) / scan`). Before it ci-kit's armed PRs were updated by nothing |
| `.github/workflows/merge-train.yml` | reusable: `ci-merge-train` on the caller's SECURE pool (a PAT: GITHUB_TOKEN pushes fire no CI); caller template in the header (hourly backstop cron + dispatch -- pr-autoupdate's `train-workflow` dispatches the rest -- + `pull_request_target` labeled/closed + `workflow_run` of CI/review). No checkout. pr-autoupdate skips riders and holds non-conflict updates while a train is open |
| `.github/workflows/review-gate.yml` | reusable: the pre-merge model review gate. ALWAYS runs, a step decides (a skipped required check reads as passed); fails CLOSED; the policy, prompt, tools and verdict run from ci-kit at `kit-ref` (default `v1`), fetched into `$RUNNER_TEMP` with its own locked venv, so a caller PR can edit only its stanza, not the gate. Inputs: `guard-paths` (required), `label`, `charter`, `install`, `python-version`, `model`, `guard-model`, `not-prose`, `auth-mode`, `claude-code-version`, `kit-ref`, `git-ssh-repos`. A caller whose `install` fetches a PRIVATE GitHub repo by https URL passes `git-ssh-repos: owner/name` and the optional secret `git-ssh-key` (a read-only deploy key): `ci_kit/git_ssh.sh` loads it in a step of its own, Install uses the agent, which is stopped before Review (v1.24.0); two or more private repos pass one deploy key per repo in `git-ssh-key`, in `git-ssh-repos` order (v1.28.0); a caller passing neither runs exactly as before. `review` (the credential) runs on the caller's SEALED pool (`[self-hosted, home-sealed]`: one job per boot, VM rolled back to a clean snapshot) when its `CI_SEALED_RUNNER` variable is set, hosted otherwise (v1.22.1; node via setup-node since v1.22.2, the sealed image has none, since v1.22.3 before the caller's `install` too, and a failed setup still reaches Review; never the secure or socket pool); the verdict record (since v1.35.0 the `record verdict` STEP of `verdict`) runs on the caller's secure pool when `CI_SECURE_RUNNER` is set (v1.17.1; never the socket pool: later runs trust its artifact). A same-repo PR opened by Renovate (`renovate[bot]`, every non-merge commit its own) whose diff is version bumps only (`ci_kit/review/bump.py`: every changed manifest line differs from its pair only in a pinned version, ref, tag or digest, each version strictly up with its suffix kept, no `!=`/`<`/`<=` moved) skips the model call even on a guard path; any other author is reviewed, and the label still reviews (v1.23.0). A commit by another author whose own diff touches lockfiles only (trade-data `relock.yml`) is accepted inside such a PR unless a lockfile it touches is under a `not-prose` prefix or on the caller's `guard-paths`; a refusal logs `-- failed lock: <which>`, naming commits by SHA only (v1.27.1). Its header is the caller template: one `review` job plus a `review gate` alls-green aggregate, which is the required check. THREE JOBS since v1.35.0 (five v1.29.0-v1.34.x): `review` (job id `decide`, the SECURE pool: train, tier, CARRY, admission -- a base-only update of a passed PR never waits for the sealed runner), `model review` (the sealed pool, a full review only) and `verdict` (secure pool: the aggregate -- a skipped `model review` passes only when `decide` said none was needed -- then the `record verdict` and `review deferred` STEPS, which were jobs of their own and cost two runner boots per run). A CARRIED pass is finished in `decide` itself (`python -m ci_kit.review.carry finish`: the sticky comment and the record) and `verdict` is then SKIPPED -- only when `decide` succeeded and says `finished`; any failure there runs `verdict` as before. Check names are `reviewer / review`, `reviewer / model review`, `reviewer / verdict` and the caller's required `review gate`, unchanged; a parked run is the `verdict` job failing on its `review deferred` step, which `redclass`/`pr_sched` read as PARKED |
| `.github/workflows/ci-green.yml` | reusable: `ci-green` on `workflow_run` of the caller's CI (template in the file's header: `permissions: {}`, job `contents: write`, no-cancel concurrency). Advances only on a `success` run of a `push`/`workflow_dispatch` on the DEFAULT branch from the same repo; checks out that run's `head_sha` at full depth. Pushes with the caller's PAT (secret `token`: contents + workflows RW -- GITHUB_TOKEN is refused any push whose range touches `.github/workflows/**`), so it runs hosted or on the SECURE pool (`CI_SECURE_RUNNER`, `ci-secure` environment), never the socket pool |
| `.github/workflows/major-tag.yml` | moves each floating `vX` to its highest `vX.Y.Z` on every release tag; also reusable (`workflow_call`) -- the caller declares the triggers and `contents: write` |
| `.pre-commit-hooks.yaml` | hooks `doc-refs`, `secret-argv`, `hidden-unicode`, `dup-code` |
| `.github/workflows/version-tag.yml` | reusable: `ci-version-tag` for a version-first repo (ci-kit's own caller: `tag-version.yml`, dispatching `major-tag.yml` on each new tag); the caller declares push-to-main + a daily cron + workflow_dispatch and `contents: write, actions: write` (template in the file's header); inputs `release-workflow`, `github-release`, `pypi`, `since`, `dry-run`, `runner` |
| `renovate/default.json5` | the estate's ONE Renovate policy (moved here from the private edge-ci 2026-11-03: Renovate gives a PUBLIC repo a token scoped to itself, so it cannot read a preset in a private repo, and llm-ports' Renovate stopped on it). A public repo's `renovate.json` extends `github>mjdevaccount/ci-kit//renovate/default.json5`; a private repo may keep extending edge-ci's file, which is one line pointing here -- never a second copy. Window all Saturday + Sunday until 18:00 ET, 3-day release age (first-party `mjdevaccount` kits exempt from both, but since v1.35.0 in ONE daily window, 04:00-07:59 ET, so a day's releases make one PR per kit per repo; lock-file maintenance once a week, Saturday 00:00-05:59 ET, its branch untouched outside it), non-major + CI-action majors + compose minors auto-merge once EVERY check is green (`platformAutomerge` off), CI-only tooling (third-party `uses:` pins and `# renovate:` pins in workflows / `ci_kit/`, majors included) skips the age wait and the weekend for the daily window (ci-kit#139 -> #144; `tests/test_renovate_ci_tooling.py`; never a workflow's docker `services:` image), other majors for review, stateful images held, security at any time; in every SERIAL-QUEUE repo (one `matchRepositories` rule) Renovate neither merges (`automergeType: pr-comment`) nor rebases on behind (`rebaseWhen: conflicted`) -- the queue alone makes a PR current and lands it; `customManagers` bump first-party kit pins outside `uses:` (the shapes `ci-pin-drift` reads) and `# renovate: datasource=... depName=...` annotated pins (`tests/test_renovate_annotation_must_fire.py`). Read from `main`, so a merge here changes every repo; CI runs `renovate-config-validator --strict` on it |

## Using it

Workflows (the check is named `<caller job> / <called job>`, e.g. `doc-refs / lint`):

```yaml
jobs:
  doc-refs:
    uses: mjdevaccount/ci-kit/.github/workflows/doc-refs.yml@v1
    with:
      args: --exempt-prefix u/ --code-doc-dir docs --code-doc-dir runbooks   # optional
  secret-argv:
    uses: mjdevaccount/ci-kit/.github/workflows/secret-argv.yml@v1
  hidden-unicode:
    uses: mjdevaccount/ci-kit/.github/workflows/hidden-unicode.yml@v1
    with:
      args: --binary-suffix .age   # optional; a binary format the kit's list lacks
  literals:
    uses: mjdevaccount/ci-kit/.github/workflows/declared-literal.yml@v1
    with:
      args: "@ci/literals.args"   # required: the policy is yours (see below)
  dup-code:
    uses: mjdevaccount/ci-kit/.github/workflows/dup-code.yml@v1
    with:
      args: --ignore windmill/**/flow.yaml   # optional; plain flags, no quotes
  gitleaks:
    name: gitleaks (secret scan)
    permissions: { contents: read }
    uses: mjdevaccount/ci-kit/.github/workflows/gitleaks.yml@v1
    with: { mode: tree }
```

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

The secret **names are yours**: the workflow declares `token` and `ntfy-url` and you map onto them.

### Keeping held PRs current (`update-held`)

The update pass merges the base into ARMED PRs only. A PR you hold for review (open, auto-merge
not enabled, often labelled `autoland-off`) is never updated, so under "require branches to be up
to date" it falls BEHIND and stays there. `update-held: true` (default `false`: nothing changes
for a caller that does not set it) keeps those current too, under a cost guard, because every
update re-runs the PR's CI. The input is available from the release after v1.40.0 (this change
moves `version` to 1.41.0; a `@v1` caller has it once that tag is cut):

- only on a `schedule` or `workflow_dispatch` pass -- never on push, `workflow_run` or arming --
  so add a cron to the caller's `on:` (e.g. `schedule: [{cron: "17 * * * *"}]`);
- LAST in that pass, after the armed pass has written its outputs (`relock`, `heal`) and its
  summary, and only when no armed PR is still on its way to current: none the queue picked to
  update this pass, none BEHIND waiting only on its own running checks, none whose merge state
  is not computed yet. An armed PR held for a FAILING check does not block it (nothing updates
  that PR until its author pushes). No merge train may be open. The skip line names each
  blocking PR and its reason;
- only in the free `max-in-flight` slots -- a held PR whose own checks are running holds one too,
  so armed plus held never exceed it; armed PRs never wait on held ones -- lowest PR number
  first; with no queue, every eligible one. The skip line names every slot holder.

Worst case, plainly: on each tick (one scheduled or dispatched pass) every eligible held PR can
be re-merged with the base and its CI re-run, at most once per PR per tick. The update gives it
a new head, so the same PR is not eligible again until that head is 10 minutes old AND its CI
has finished, i.e. one update per PR per max(10 min, CI time), rounded up to your next tick. A
day costs at most (held PRs) x (ticks per day) CI runs, and fewer in practice: a PR already
current with the base is never touched. With `max-in-flight: 0` (no queue) ALL eligible held
PRs are updated in the same tick; with a queue, at most the free slots are.

Eligible: same-repo, not a draft, BEHIND, cleanly mergeable, a head commit at least 10 minutes
old (younger, or of unreadable age: someone may still be pushing), and no check of ANY name
running or failing on its head (`required-checks` narrows the armed queue, not this). Never
touched: forks, drafts, conflicting PRs, any bot's PR (a Bot account or a `[bot]` login,
Renovate included: a bot rebases its own branch) and merge-train branches. A held PR is never
labelled, paged or relocked -- a conflict or an error on its update is one summary line,
`#N: held, not updated: <why>`. `autoland-off` opts a PR out of auto-LANDING only; it is still
kept current. A failure of the held pass never turns the run red: it is one
`update-held: FAILED` line and a warning annotation. The summary names every held PR it skipped
and why, and ends `update-held: N held PR(s) updated of M eligible, K skipped, E error(s)`.

### The push token: GitHub App or PAT (v1.37.0)

A push made with `GITHUB_TOKEN` fires no workflow, so the token jobs (pr-autoupdate, merge-train,
ci-green, pr-heal's claim/push/report) push, comment and merge with the caller's token. That was
always the PAT in `token`. A GitHub App installation token works too, and does not expire with a
person's PAT: the App's id is the caller's repository VARIABLE `EDGE_BOT_APP_ID`, its key the
caller's SECRET `EDGE_BOT_PRIVATE_KEY`, which each reusable declares as an optional
`workflow_call` secret (map it like `token`; under `secrets: inherit` it passes as is, the names match).
Which one ACTS is `token-source` (a reusable input), else the caller's `PR_TOKEN_SOURCE` variable,
else `pat` (`ci_kit/pr_token.py`):

| mode | acts | App absent | App mint fails |
|---|---|---|---|
| `pat` (default) | the PAT; the App, if its key is passed, is minted with `metadata: read` ONLY and NOT used (a canary) | PAT | `::warning::`, PAT acts |
| `app` | the App | PAT, `reason=var-absent` / `key-absent` | **red** -- a broken key never hides behind the PAT |
| `app-required` | the App; `token` is never read | **red** | **red** |

THE CANARY is on by default, not opt-in: it is the only thing that proves the key AND the
App's installation in a repo BEFORE that repo flips (opt-in would mean it never runs), and a
`pr-token: ... app=failed` warning across repos is the early signal. It is write-free: the mint
asks for `metadata: read` and nothing else -- never NO permission input, which
`create-github-app-token` answers with the installation's WHOLE permission set. Cost: one API
call per job.

Staged rollout -- each stage is config in the CALLER repo, no ci-kit change:

1. **Flip one repo.** Its caller stanzas add `EDGE_BOT_PRIVATE_KEY: ${{ secrets.EDGE_BOT_PRIVATE_KEY }}`
   under `secrets:` (pr-autoupdate, merge-train, ci-green, pr-heal), then set the repo variable
   `PR_TOKEN_SOURCE=app` (`gh variable set PR_TOKEN_SOURCE -R owner/repo --body app`). Until the
   variable is set the runs read `source=pat ... app=minted`: the key is proven before the flip.
   Rollback: delete the variable. Keep the `EDGE_BOT_PRIVATE_KEY` mapping when you do: the PAT
   run learns the App's login from the canary, so a caller that drops the key on rollback stops
   trusting the App's base-retry markers and trains (its record line then reads `app=absent`).
2. **Flip the default.** The same two edits in every caller repo (the variable per repo -- a
   personal account has no organisation variables). A repo without them stays on the PAT.
3. **Make the PAT fail loud.** `PR_TOKEN_SOURCE=app-required` per repo: an App that is absent or
   cannot mint is a red run, and `token` is no longer read; then the PAT secret can be retired.
   Markers and trains written under the PAT are still honoured only while `token` is passed
   (its login is read from it); after that they are history.

IDENTITY: the App acts as `<app-slug>[bot]` (REST spelling; GraphQL says `<app-slug>` with
`__typename` Bot). An installation token cannot call `GET /user`, so the composite resolves the
login (the mint's `app-slug`; `GET /user` with the PAT for the PAT) and passes it as
`CI_KIT_TOKEN_LOGIN`, with the other configured identity in `CI_KIT_TRUSTED_LOGINS`: base-retry
markers and open merge trains written by either side of a flip are honoured, nobody else's.
An unreadable acting identity is a red run where a tool trusts markers with it (scopes `update`
and `train`: base-retry, the merge train); in ci-green and pr-heal nothing reads it, so it is a
`::warning::`, `login=unknown`, and the token is still handed out. Train commits keep each rider as author; the
committer is the acting identity (`<id>+<slug>[bot]@users.noreply.github.com` for the App). Heal
commits stay `github-actions[bot]` whichever token pushes them.

THE RECORD: every token job prints ONE line, as a `::notice` (an `::error` when the decide step
could not run at all) and in the step summary:
`pr-token: source=app|pat|none mode=<mode> reason=<configured|mode-pat|var-absent|key-absent|mode-invalid|scope-invalid|mint-failed|identity-unreadable|decide-crashed> app=<minted|failed|absent> repo=<owner/repo> workflow=<file> job=<id> login=<login|unknown>`.
Its grammar is a CONTRACT: a detector parses it. Fields are space-separated `key=value` in this
order and no value holds `=` or a space; a new reason or field is a ci-kit release, listed here
(`tests/test_pr_token_must_fire.py` holds this list to what the code can emit).
Read it from outside with `gh run view <run> --log | grep 'pr-token: '` or the job's check-run
annotations (`gh api repos/<owner>/<repo>/check-runs/<job id>/annotations`); the step summary is
for humans (no REST endpoint serves it).

### The pr-token composite's input names (v1.40.0)

The composite (`.github/actions/pr-token`) takes the App's id as `client-id` (its client id, or app
id -- `actions/create-github-app-token`'s own name). `app-id` is a DEPRECATED alias: a caller that
passes `app-id` + `private-key` exactly as before gets the same App id and key, the same decision and
the same `pr-token:` record line, plus one `::notice` titled `ci-kit deprecated name` (never the
record's `pr-token` title). Two more optional inputs, `deprecated-client-id` and
`deprecated-private-key`, are read only when `client-id`/`app-id` resp. `private-key` are empty,
each use a notice; generic first. The aliases are removed in v2.
`ci_kit.pr_admit --pool` also accepts a runs-on JSON label list, read as its LAST label, or a JSON
string; a value that names no label (malformed JSON, `[]`, a non-string item, quoted text) is refused
with an `::error` and exit 2, and the review then runs (v1.42.0).

### Generic names in the reusables (v1.42.0)

The token reusables (pr-autoupdate, merge-train, ci-green, pr-heal, relock) take the App as the
`app-client-id` INPUT (its client id, or app id) and the `app-private-key` SECRET -- the caller
maps whatever it calls them. The older spellings above, the repository variable `EDGE_BOT_APP_ID`
and the secret `EDGE_BOT_PRIVATE_KEY`, are DEPRECATED aliases: a caller that sets only them gets the
same values and the same decision, plus one `::notice` per alias used; when both are set the
generic name wins. They are removed in v2. Each reusable hands both spellings to the pr-token
composite, which alone resolves them (`deprecated-client-id` / `deprecated-private-key`).

Pools are inputs too, each a JSON label list read with `fromJSON`: `secure-runner` (the jobs that
hold a write token; pr-autoupdate, merge-train, ci-green, pr-heal, relock, review-gate) and
`sealed-runner` (the job that holds the model credential AND runs PR code; pr-heal, review-gate;
its LAST label names the pool to the scheduler). Precedence: the input; else the caller repo's
`CI_SECURE_RUNNER` / `CI_SEALED_RUNNER` variable, an ON/OFF switch for the kit's default labels
(`["self-hosted","home-secure"]` / `["self-hosted","home-sealed"]` -- a variable never carries
labels, so a typo there cannot move a token job onto another pool); else `ubuntu-latest`. The
secure pool runs in the caller's `ci-secure` environment wherever the workflow used one. A caller
that sets no input runs on exactly the pools it ran on before.

`sealed-runner` needs `kit-ref` at v1.40.0 or later: an older pinned kit's `pr_admit` reads the
JSON list as ONE label, which names no pool, so the review never parks -- silently.

Rollback floor: once v1.42.0 is out, moving the floating `v1` tag back goes no lower than v1.40.0.
The reusables now send the pr-token composite names (`client-id`, `deprecated-client-id`,
`deprecated-private-key`) that actions older than v1.40.0 do not declare.

### Relocking a dependency bot's PR (v1.38.0)

A bot's regex manager bumps a pin in the manifest and leaves the lockfile stale, so the PR is red
on `uv lock --check` until someone relocks it. `relock.yml` does: `lock` runs the lock tool HOSTED
with no token, `push` writes only the lockfiles YOUR `lockfiles` input names, commits them as
`github-actions[bot]` (Renovate: `gitIgnoredAuthors`, which `renovate/default.json5` sets) and
pushes fast-forward with the token. A bot PR whose lock already matches passes without reading the
token; one that needs a relock with no token is red, by name. A person's branch, a fork, or a caller
on any event but `pull_request` is never relocked (the last is a red `trigger` job).

A plain repo (`master` as default branch changes nothing here: the PR's own base is used):

```yaml
name: relock
on:
  pull_request:
    paths: [pyproject.toml, uv.lock, .github/workflows/relock.yml]
permissions:
  contents: read
concurrency:
  group: relock-${{ github.event.pull_request.number }}
  cancel-in-progress: true
jobs:
  relock:
    uses: mjdevaccount/ci-kit/.github/workflows/relock.yml@v1
    # with: { lockfiles: uv.lock, branch-prefix: renovate/ }      # the defaults
    secrets:
      token: ${{ secrets.YOUR_PUSH_PAT }}                         # contents RW; or the App below
      EDGE_BOT_PRIVATE_KEY: ${{ secrets.EDGE_BOT_PRIVATE_KEY }}   # optional (token-source)
```

A repo whose lockfile names PRIVATE GitHub repos by https URL (same `on:` and `concurrency:`): one
READ-ONLY deploy key per repo, in `git-ssh-repos` order (`ci_kit/git_ssh.sh`):

```yaml
jobs:
  relock:
    uses: mjdevaccount/ci-kit/.github/workflows/relock.yml@v1
    with:
      git-ssh-repos: owner/private-a owner/private-b
    secrets:
      token: ${{ secrets.YOUR_PUSH_PAT }}
      EDGE_BOT_PRIVATE_KEY: ${{ secrets.EDGE_BOT_PRIVATE_KEY }}   # optional
      git-ssh-key: |
        ${{ secrets.PRIVATE_A_DEPLOY_KEY }}
        ${{ secrets.PRIVATE_B_DEPLOY_KEY }}
```

This is not pr-autoupdate's `relock` output (a lockfile-only CONFLICT after a main merge, which the
heal ladder's tier 1 replaces): that one acts on armed PRs that conflict, this on a bot PR whose
own manifest moved, the moment it opens. Both push fast-forward only, so a race loses as `moved`.

The library, from a lockfile:

```toml
[project]
dependencies = ["ci-kit @ git+https://github.com/mjdevaccount/ci-kit@v1.0.0"]
```

The SQL gates, as a dev dependency (their baselines live in your repo):

```toml
[dependency-groups]
dev = ["ci-kit[sql] @ git+https://github.com/mjdevaccount/ci-kit@v1.3.0"]
```

```bash
uv run ci-sql-casts --scan-dir app --baseline ci/sql_casts_baseline.txt --d-baseline ci/sql_bind_arith_baseline.txt
uv run ci-pit-reads --scan-dir app --baseline ci/pit_reads_baseline.txt
uv run ci-decimal-reads --scan-dir app --cols ci/numeric_columns.txt --baseline ci/decimal_reads_baseline.txt
uv run ci-column-comments --migrations migrations --legacy-max 7
```

```python
from ci_kit import astread, discover, site_lint
from ci_kit.ratchet import RatchetCount
```

Locally, or from a Makefile:

```bash
uvx --from git+https://github.com/mjdevaccount/ci-kit@v1.0.0 ci-doc-lint --root ../x
uvx --from git+https://github.com/mjdevaccount/ci-kit@v1.0.0 ci-pin-drift --file uv.lock --remote https://github.com/mjdevaccount/ci-kit.git
uvx --from git+https://github.com/mjdevaccount/ci-kit@v1.10.0 ci-dup-code --write-baseline   # once: freezes today's clones; later runs refuse to grow it
```

pre-commit:

```yaml
- repo: https://github.com/mjdevaccount/ci-kit
  rev: v1.0.0
  hooks: [{ id: doc-refs }, { id: secret-argv }, { id: dup-code }]
```

## Self-hosted runner capacity (`ci-runners`)

`ci_kit.runners` sizes self-hosted GitHub Actions runners that run as ephemeral containers inside
runner VMs on one host (Proxmox or similar), shared by per-repo POOLS. The kit owns the
mechanism; a domain supplies two files:

- **the capacity file** (`runners.yml`): `host` (threads, RAM, `cpu_overcommit`, `reserves_mb`),
  `guests` (vmid, cores, RAM; `state: running|stopped|on_demand`, `planned`, `budgeted: false`,
  `shares: <reserve>`), and one block per runner tier (`vm`, `max_total`, overcommits,
  `vm_reserve_mb`, `mem_book_ratio` + `live_floor_mb`, `backfill`, `redecide`, `defaults`, `pools`).
  Each pool has `min_idle`, optional `max_runners` (absent = derived from admission), a MEASURED
  `peak_mb`, optional `young_mb` / `starve_s`, and, for a second pool of one repo by job class,
  `repo` + its own `labels` + its own `variable`.
- **the kit** (`runners-kit.yml`): what the domain decides once. These are its extension points:

| kit key | what it extends |
|---|---|
| `blocks.<key>` | a runner block the capacity file may hold: `instance`, `prefix` (unique, or two supervisors reap each other's runners), `isolation` (passed to the supervisor), `label` (the trust tier: a pool carries `label` or `label-<class>` and no other block's), `variable` (the `runs-on` kill switch), `output`, `required`, `one_job_per_boot` (max_total 1 on an `on_demand` VM) |
| `share_duties.<reserve>` | a guest that `shares` a host reserve runs inside it, so the reserve's other owner must yield: `--check` requires `doc` to carry `text` |
| `out_dir`, `generated`, `write_command` | where renders go (relative to `--root`), the banner they carry, the command a stale finding names |
| `alloc_jq` | a path: `--write` copies ci-kit's `alloc.jq` there, `--check` flags a copy that drifted |
| `source_hash` | `raw` (default) or `canonical`, below |
| `Kit(rules=...)` (Python) | callables `(cfg, kit) -> [finding]` run after every built-in check passes |

```bash
ci-runners --check --config ci/runners.yml --kit ci/runners-kit.yml   # CI: schema + budgets + fresh renders
ci-runners --write --config ci/runners.yml --kit ci/runners-kit.yml   # regenerate the renders
ci-runners --write ... --out-dir "$RUNNER_TEMP/r"                     # render elsewhere, e.g. to diff against another renderer
ci-runners --alloc < tick.json                                        # one admission verdict (alloc.jq's twin)
ci-runners --selftest
```

Exit 0 clean, 1 findings or a stale / orphaned render, 2 could not look (unreadable or unparsable
file, malformed kit, no pyyaml). **Admission** (`alloc.jq`, run by the supervisor with no Python):
a start needs a free slot AND its pool's `peak_mb` free inside `mem_budget_mb`, capped each tick by
the VM's live MemAvailable less `live_floor_mb`; a pool that does not fit is held, and after
`starve_s` it reserves its room and the run drains for it, with bounded backfill.

**`source_sha256` and the reinstall it causes.** A supervisor reinstalls whenever its render's
`source_sha256` changes. In `raw` mode (the default, and what existing renders carry) the hash
covers the capacity file's TEXT, so a comment-only edit reinstalls every supervisor. `canonical`
hashes the parsed data instead, so comments and formatting reinstall nothing. It is opt-in and
OFF by default because switching modes changes every render's hash once: turning it on is ONE
deliberate reinstall of every supervisor, done when the operator chooses, not as a side effect of
upgrading ci-kit.

The golden test (`tests/test_runners_must_fire.py`) holds the render BYTE-IDENTICAL to the
hand-rolled renderer this replaced, on a fixture rendered by that renderer.

## Nothing is assumed about your repo

- `ci-doc-lint` exempts no prefix by default; `--exempt-prefix` names a namespace of yours whose
  spans look like paths but are not (a job scheduler's `u/user/flow`). It reads source files for
  pointers into `docs/` only; `--code-doc-dir` adds another tree.
- `ci-pin-drift` has no default remote: `--remote` is required, and the kit's name in a pin is
  the remote's last path component (`--name` to override).
- `ci-manual-lint` checks `CLAUDE.md` (`--manual` for another) and nothing else unless asked:
  sibling repos (`--sibling`), a migrations dir, a line budget, placeholder packages, reference-doc
  dirs, a stamp and a total index are all flags. Commit them in an args file and run
  `ci-manual-lint @scripts/manual_lint.args` so CI and a workstation sweep read the same rules.
- `ci-declared-literal` holds no pattern at all. A regex cannot ride in the word-split `args`, so
  the policy is an args file, UTF-8, one argument per line (blank and `#` lines are notes):
  `--rule` / `nets` / `--pattern=\b192\.0\.2\.\d{1,3}\b` / `--example=GW=192.0.2.1` /
  `--declared` / `.env.example` / `--message` / `<your remediation>`, then `--exempt-suffix` /
  `.md` if prose may name the value (matched as written: `.md` does not exempt `NOTES.MD`). Several `--rule` blocks may share one file, each with its own
  declared set. Every rule must PROVE it can fire: each `--example` must be flagged and each
  `--pattern` must match an example, and a declared file that is present must hold a match --
  otherwise exit 2. Comment openers depend on the file kind, each a subset of the predecessor's
  six; `--comment-prefix .j2={#` adds one (`*` = every file but PowerShell, whose comments are
  parsed; a PowerShell key is exit 2). A note line right after a value-taking flag is exit 2, so a
  value starting `#` uses `--message=#...`. `@file` resolves against the working directory (the
  checkout root in the workflow), not `--root`. There is no `--help`: exit 0 from a gate run comes only
  from a scan that read files (`--selftest` alone writes no receipt), and the workflow asserts the scan receipt (`CI_KIT_DECLARED_LITERAL_RECEIPT`).
  A file with NUL bytes is also read as UTF-16-LE and -BE, whatever its name.
- `ci-guard-selfproof` reads `scripts/` for `lint_*` by default; `--scripts`, `--prefix`,
  `--baseline` and `--js` (prove `.mjs`/`.js` guards through `node <guard> --selftest`, which
  must exit 0 AND print a last line `<name> selftest: OK`) are yours. **Run it where your guards'
  dependencies are installed** (`uv run`, or `pip install` ci-kit into the CI python): importing
  a guard imports what it imports. The action's isolated uvx environment holds ci-kit alone, so
  it fits only a repo whose guards are stdlib-only.
- `ci-dup-code` reads the whole tracked tree at the kit's thresholds and ignores; a repo adds
  `--ignore GLOB` (repeatable), `--format LIST`, `--min-tokens` / `--min-lines`. The baseline is
  per repo (`--baseline`, default `ci/jscpd-baseline.json`) and the first `--write-baseline` is the
  only one that may be larger than the last.
- `args` (the action and every reusable workflow) is word-split by the shell and nothing else:
  plain flags, **no quotes** (they would stay literal) and no expansion of them (`set -f`, since
  v1.10.1), so `--ignore windmill/**/flow.yaml` reaches the tool as that glob.
- `pr-autoupdate.yml` declares generic secret names; `--label` renames the conflict label.
- The tree a gate reads is `--root`, else `CI_KIT_ROOT`, else the git toplevel holding the
  working directory. Never the tool's own location.
- `runs-on` for the no-secret workflows is the caller's `CI_RUNNER` repository variable (a JSON
  label list) when set, else `ubuntu-latest`. Secret-holding jobs run hosted unless the caller
  turns on their fixed-label switch: secure (`CI_SECURE_RUNNER`) for pr-autoupdate, ci-green and
  the review `verdict` job; sealed (`CI_SEALED_RUNNER`) for review. Never the shared `CI_RUNNER` pool.
  The six lint workflows (`doc-refs`, `dup-code`, `secret-argv`, `hidden-unicode`, `declared-literal`, `gitleaks`) read
  `CI_RUNNER_LIGHT` FIRST (v1.18.0): a self-hosted pool admits runners at their pool's peak
  memory, and a lint booked at the repo's heaviest job's peak held the room a real job needed
  (home-host 2026-11-08: ~75% of queued jobs were these lints). Set it only once that light
  pool is live -- a label nothing serves queues forever.

## Proof discipline

Every gate ships with a must-fire suite (`tests/`): driven against a known-BAD input (exit 1), a
known-GOOD one (exit 0) and a BLIND one it cannot read (exit 2), on throwaway **real** git repos,
not mocks. A guard only ever seen passing is indistinguishable from one that cannot fail. Each
selftest names the mutation it dies to. CI runs the suite, every selftest through the action
exactly as callers get it, the doc gate and secret-argv gate on this repo, `actionlint`, and the
kit's own gitleaks workflow.

## Releasing

Change, PR, CI green, merge. **Library releases are immutable `vX.Y.Z` tags, never moved**: a
consumer's lockfile records the tag and its commit, so moving one breaks `--frozen` installs; a
fix is the next patch tag, and `version` in `pyproject.toml` moves with it. The reusable
workflows reference the action as `mjdevaccount/ci-kit@v1`, a moving major. **Nobody moves it by
hand**: `.github/workflows/major-tag.yml` points each `vX` at the highest `vX.Y.Z` on every release
tag push, on `workflow_dispatch`, and daily, and verifies the move with `ls-remote` (a backport or
pre-release tag never moves it). **Nobody cuts the `vX.Y.Z` tag by hand either** (2026-11-09):
`.github/workflows/tag-version.yml` runs `ci-version-tag` on every `pyproject.toml` change to main
(and daily), tags the bump commit, and dispatches `major-tag.yml` on the new tag, so the release
PR that moves `version` IS the release; the major follows in the same run. A breaking change is a new major (`v2`) with those references updated in the
same commit.
