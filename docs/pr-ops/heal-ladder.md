# The heal ladder and the relock

## The heal ladder (`heal: true`)

`ci_kit/pr_ladder.py` (v1.33.0), turned on by pr-autoupdate's `heal: true` (+ `heal-claude-per-pass`,
`heal-claude-per-pr`, `heal-per-pr`, `heal-stale-minutes`). An armed PR that goes red or conflicts
heals itself, cheapest tier first:

| tier | what | where |
|---|---|---|
| 0 | a clean merge (the updater) / a machinery rerun (`pr_heal`) | pr-autoupdate |
| 1 | a lockfile-only conflict relocked (uv, npm, pnpm, NuGet; built in) | `pr-heal.yml` `regen` |
| 2 | a conflict or red confined to GENERATED files, regenerated from the caller's `regen` map; a release-binary `Pin` whose version Renovate moved gets its sha256 table recomputed (`ci_kit/pin_rehash.py`) | `pr-heal.yml` `regen` |
| 3 | Claude Code in Actions (`ci_kit/pr_fix.py`) on the SEALED pool, for a judgment red or a real conflict | `pr-heal.yml` `claim` + `fix` |
| 4 | ONE ntfy page per head with the PR, the failing check and what was tried | `pr-heal.yml` `report` |

Tier 3 runs at most once per head, under the `heal-claude-per-pass` / `-per-pr` budgets, and never
on a Renovate major, an `autoland-off`, unarmed, draft, fork or database-group PR. It never edits a
test (it declines; the pusher refuses). A conflicted Renovate PR (armed = its body says
`Automerge: Enabled`) gets its rebase box ticked instead: Renovate rebuilds its own branch.

With the ladder on, a judgment red gets the ladder instead of `pr_heal`'s one comment, and a
conflict the ladder instead of the page; machinery is still `pr_heal`'s. Drop the caller's own
`relock` job when you turn it on (tier 1 relocks, and two pushers race).

**The record.** Every dispatch writes `<!-- smart-merge:heal head= mode= claude= at= -->` BEFORE
it runs (the once-per-head budget), the heal job `<!-- smart-merge:healed head= tier= result= -->`,
a page `<!-- smart-merge:paged head= why= -->`, and tier 3
`<!-- smart-merge:attempt head= tier=claude run= -->` from a token-only `claim` job BEFORE Claude
runs (a second run or a re-run at the head is refused). A dispatch with no record after
`heal-stale-minutes` is paged by the next pass (and turns it red).

**A tier 3 that cannot start** (v1.46.0) is the runner's fault, not the PR's.
`pr_fix.sandbox_check` asks the runner for the sandbox the CLI's env scrub demands (bubblewrap +
socat on PATH, and a bwrap that can create a user and pid namespace) BEFORE the CLI runs.
`pr_fix.judge` reads the CLI's own `could not start` and a run whose every Bash call the sandbox
refused (a call the permission rules denied is left out of "every"; a red command that printed its
own `bwrap: ` line is not a refusal); a CLI that is not installed (`exit=not-run`) is the same
kind. The record says `result=cannot-start`, with a page titled
`<repo>: heal tier 3 CANNOT START (the runner, every PR)` instead of
`#N: heal ladder could not fix it` (on one consumer, 17 attempts on 15 PRs over 11 days had each
been recorded as that PR's `failed`). A cannot-start head keeps its once-per-head dispatch and does
not count toward `heal-claude-per-pr`: the next head, on a runner that works, still gets tier 3.

### `pr-heal.yml`: six jobs, one trust boundary each

The reusable runs the ladder's tiers 1-4 for ONE PR, called per row of pr-autoupdate's `heal`
output; the caller template is the file's header ([inputs](../reusable-workflows.md#pr-healyml)).

| job | pool | holds |
|---|---|---|
| `plan` | secure, token-free | re-checks the head and arming, gathers the failed logs and the review findings |
| `regen` | the CI pool (`CI_RUNNER`) or hosted | PR code, no secret but an optional `git-ssh-key` agent |
| `claim` | secure | the token: tier 3's once-per-head attempt marker, written before Claude runs |
| `fix` | the SEALED pool or hosted | tier 3: the review gate's credential toggle and `--restricted` CLI, every token stripped from its env |
| `push` | secure | the token, no PR code: `ci_kit/pr_apply.py` |
| `report` | secure | the `healed` record and the page |

`fix` is bounded at `pr_fix.FIX_JOB_MIN` = 50 min (the CLI's 40 + 2 + 8 setup), and every
pr-autoupdate pass names it while it holds the sealed runner:
`TIER 3 IN FLIGHT on the sealed runner (run R, ...)` (v1.35.0).

The SEALED runner must carry `bubblewrap` and `socat` and let its job create user namespaces
(v1.46.0): the fix step sets `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`, under which the CLI runs every
Bash command in a bwrap sandbox or not at all. A runner that cannot is `result` = `cannot-start`,
decided before a model session is spent.

### Tiers 1-2 and the one pusher (`pr_regen`, `pr_apply`)

`ci_kit/pr_regen.py` merges the base with `--no-commit` (a `.gitattributes` `merge=union` file is
resolved by git itself; use that only for truly append-only files, never a shrink-only baseline:
union re-adds a deleted line), takes main's side of each covered file, runs the generators and
saves the files they changed. A write outside the map, a marker, a grown `shrink: true` baseline
or a failed generator hands the PR to tier 3.

`ci_kit/pr_apply.py` runs on a fresh SECURE-pool runner with the push token and no PR code:

- it re-checks the head and redoes the merge (git's conflicted set must equal the tier's);
- it re-checks SCOPE (regen: generated / lockfile / sha256-only-pin files; claude: the PR's own +
  conflicted + generated files, no test, nothing under `.github/`, `max-fix-lines`);
- it commits a merge or one fix commit as `github-actions[bot]` (Renovate's `gitIgnoredAuthors`)
  and pushes fast-forward only;
- since v1.38.0 it never writes THROUGH a symlink (`unsafe_destination`): a destination tracked as
  a link, a linked parent directory, or a path resolving outside the repository or into `.git/`
  refuses the whole change, red (`destination symlink: ...`).

## Relocking a dependency bot's PR

`ci_kit/pr_relock.py` (v1.38.0), run by `.github/workflows/relock.yml`
([inputs](../reusable-workflows.md#relockyml)).

A bot's regex manager bumps a pin in the manifest and leaves the lockfile stale, so the PR is red
on `uv lock --check` until someone relocks it. `relock.yml` does: `lock` runs the lock tool HOSTED
with no token, `push` writes only the lockfiles YOUR `lockfiles` input names, commits them as
`github-actions[bot]` (Renovate: `gitIgnoredAuthors`, which `renovate/default.json5` sets) and
pushes fast-forward with the token. A bot PR whose lock already matches passes without reading the
token; one that needs a relock with no token is red, by name. A person's branch, a fork, or a
caller on any event but `pull_request` is never relocked (the last is a red `trigger` job).

It reuses `pr_regen` + `pr_apply` (the heal ladder's one way to regenerate a lockfile with no
secret and push it with a token), plus what a relock alone needs:

- NO merge of the base;
- the lockfile set is the caller's `lockfiles` input (tracked, a `LOCKFILES` kind);
- the event gate (`pull_request`, same repo, `branch-prefix`) in both jobs;
- a STRICT artifact check on the push side (exactly `manifest.json` + `files/<p>` for p in the push
  job's own copy of the input, regular files, no delete, no merge claim);
- NO LOOP (a relock needed on top of its own relock commit is refused).

It commits as `github-actions[bot]`, subject `chore(lock): relock <paths>`. Jobs: `trigger` (RED
for any other event or an empty `branch-prefix`, never a quiet skip), `lock` (HOSTED, PR code: the
lock tool, no token, only an optional read-only `git-ssh-key` agent), `push` (hosted or the fixed
secure labels, the token from `pr-token` scope `push`, no PR code). Output `result`.

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
      app-private-key: ${{ secrets.YOUR_APP_PRIVATE_KEY }}        # optional (token-source)
```

A repo whose lockfile names PRIVATE GitHub repos by https URL (same `on:` and `concurrency:`): one
READ-ONLY deploy key per repo, in `git-ssh-repos` order (`ci_kit/git_ssh.sh`,
[gates/supply-chain.md](../gates/supply-chain.md#git_sshsh-a-private-git-dependency-in-ci)):

```yaml
jobs:
  relock:
    uses: mjdevaccount/ci-kit/.github/workflows/relock.yml@v1
    with:
      git-ssh-repos: owner/private-a owner/private-b
    secrets:
      token: ${{ secrets.YOUR_PUSH_PAT }}
      app-private-key: ${{ secrets.YOUR_APP_PRIVATE_KEY }}        # optional
      git-ssh-key: |
        ${{ secrets.PRIVATE_A_DEPLOY_KEY }}
        ${{ secrets.PRIVATE_B_DEPLOY_KEY }}
```

The App's key goes in `app-private-key` (v1.42.0's generic name; the older `EDGE_BOT_PRIVATE_KEY`
still works as a deprecated alias, [push-token.md](push-token.md#generic-names-in-the-reusables-v1420)).

This is not pr-autoupdate's `relock` output (a lockfile-only CONFLICT after a main merge, which the
heal ladder's tier 1 replaces): that one acts on armed PRs that conflict, this on a bot PR whose own
manifest moved, the moment it opens. Both push fast-forward only, so a race loses as `moved`.
