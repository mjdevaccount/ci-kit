# Reusable workflows

Every file below declares `on: workflow_call`. Call one with
`uses: mjdevaccount/ci-kit/.github/workflows/<file>@v1`. The check a reusable reports is named
`<caller job> / <called job>` (e.g. `doc-refs / lint`). Input and secret descriptions in the tables
are the first sentence of each one's `description:` in the workflow file, which is the source of
truth; a workflow's header comment holds its full caller template wherever the triggers matter.

**Secret names are yours**: each workflow declares its own secret names and you map whatever your
repo calls them onto those.

**`args`** (the action and every reusable workflow that takes it) is word-split by the shell and
nothing else: plain flags, **no quotes** (they would stay literal) and no glob expansion of them
(`set -f`, since v1.10.1), so `--ignore vendor/**/flow.yaml` reaches the tool as that glob.

| workflow | runs | page |
|---|---|---|
| [`doc-refs.yml`](#doc-refsyml) | `ci-doc-lint` + its selftest, full history | [gates/docs.md](gates/docs.md) |
| [`dup-code.yml`](#dup-codeyml) | `ci-dup-code` + its selftest | [gates/ratchets.md](gates/ratchets.md) |
| [`secret-argv.yml`](#secret-argvyml) | `ci-secret-argv-lint` + its selftest | [gates/secrets.md](gates/secrets.md) |
| [`hidden-unicode.yml`](#hidden-unicodeyml) | `ci-hidden-unicode` + its selftest | [gates/secrets.md](gates/secrets.md) |
| [`declared-literal.yml`](#declared-literalyml) | `ci-declared-literal` + its selftest | [gates/secrets.md](gates/secrets.md) |
| [`gitleaks.yml`](#gitleaksyml) | `ci-gitleaks` secret scan | [gates/secrets.md](gates/secrets.md) |
| [`version-tag.yml`](#version-tagyml) | `ci-version-tag` | [versioning.md](versioning.md) |
| [`major-tag.yml`](#major-tagyml) | moves the floating `vX` tag | [versioning.md](versioning.md) |
| [`ci-green.yml`](#ci-greenyml) | `ci-green` | [gates/supply-chain.md](gates/supply-chain.md) |
| [`pr-autoupdate.yml`](#pr-autoupdateyml) | `ci-pr-autoupdate` | [pr-ops/autoupdate.md](pr-ops/autoupdate.md) |
| [`pr-heal.yml`](#pr-healyml) | the heal ladder for one PR | [pr-ops/heal-ladder.md](pr-ops/heal-ladder.md) |
| [`relock.yml`](#relockyml) | the dependency-bot relock | [pr-ops/heal-ladder.md](pr-ops/heal-ladder.md#relocking-a-dependency-bots-pr) |
| [`merge-train.yml`](#merge-trainyml) | `ci-merge-train` | [pr-ops/merge-train.md](pr-ops/merge-train.md) |
| [`review-gate.yml`](#review-gateyml) | the pre-merge model review gate | [review-gate.md](review-gate.md) |
| [`app-audit.yml`](#app-audityml) | `ci-app-audit`: declared GitHub Apps against live | [github-apps.md](github-apps.md) |

Not reusable (ci-kit's own): `ci.yml` (this repo's CI), `pr-autoupdate-self.yml` (ci-kit's own
updater, [pr-ops/autoupdate.md](pr-ops/autoupdate.md#ci-kits-own-updater)) and `tag-version.yml`
(ci-kit's own caller of `version-tag.yml`, [versioning.md](versioning.md)).

## runs-on

- **The lint workflows** (`doc-refs`, `dup-code`, `secret-argv`, `hidden-unicode`,
  `declared-literal`) and `version-tag` take a `runner` input (a JSON label list), which wins;
  else the caller's `CI_RUNNER_LIGHT` repository variable, else `CI_RUNNER`, else `ubuntu-latest`.
  `gitleaks` has no `runner` input and reads `CI_RUNNER_LIGHT`, else `CI_RUNNER`, else
  `ubuntu-latest`. `CI_RUNNER_LIGHT` is read FIRST (v1.18.0): a self-hosted pool admits runners at
  their pool's peak memory, and a lint booked at the repo's heaviest job's peak held the room a
  real job needed (on one self-hosted pool, ~75% of queued jobs were these lints). Set it only once
  that light pool is live: a label nothing serves queues forever.
- **Secret-holding jobs** run hosted unless the caller turns on their pool: `secure-runner` /
  `sealed-runner` inputs, else the `CI_SECURE_RUNNER` / `CI_SEALED_RUNNER` on/off switches for the
  fixed labels `["self-hosted","home-secure"]` / `["self-hosted","home-sealed"]`. Secure: the token
  jobs of pr-autoupdate, merge-train, ci-green, pr-heal, relock, app-audit (it holds an App key) and
  the review gate's `decide` and `verdict`; sealed: the review gate's `model review` and pr-heal's `fix`. Never the shared
  `CI_RUNNER` pool. Precedence and rationale: [pr-ops/push-token.md](pr-ops/push-token.md#pools).
- pr-heal's `regen` job runs PR code with no secret, so it runs on `CI_RUNNER` or hosted; relock's
  `trigger` and `lock` jobs and `major-tag` always run hosted.

## doc-refs.yml

The doc gate + its selftest, at full history: `fetch-depth: 0` is load-bearing, and a shallow clone
is exit 2, not a blind pass.

```yaml
jobs:
  doc-refs:
    uses: mjdevaccount/ci-kit/.github/workflows/doc-refs.yml@v1
    with:
      args: --exempt-prefix u/ --code-doc-dir docs --code-doc-dir runbooks   # optional
```

| input | type | default | what |
|---|---|---|---|
| `runner` | string | `''` | runs-on as a JSON label list, e.g. '["self-hosted","home"]' ('' = the caller repo's CI_RUNNER_LIGHT, else CI_RUNNER, else ubuntu-latest; docs/reusable-workflows.md "runs-on"). |
| `args` | string | `''` | extra ci-doc-lint flags (--exempt-prefix, --code-doc-dir); none by default |

## dup-code.yml

The duplicate-code gate + its selftest. No history needed; the caller commits
`ci/jscpd-baseline.json`.

```yaml
jobs:
  dup-code:
    uses: mjdevaccount/ci-kit/.github/workflows/dup-code.yml@v1
    with:
      args: --ignore vendor/**/flow.yaml   # optional; plain flags, no quotes
```

| input | type | default | what |
|---|---|---|---|
| `runner` | string | `''` | JSON runs-on labels; '' = the caller's lane (doc-refs.yml's runner input says which). |
| `args` | string | `''` | extra ci-dup-code flags (--ignore GLOB, --min-tokens N, --min-lines N, --format LIST); none by default |

## secret-argv.yml

The secret-argv lint + its selftest.

```yaml
jobs:
  secret-argv:
    uses: mjdevaccount/ci-kit/.github/workflows/secret-argv.yml@v1
```

| input | type | default | what |
|---|---|---|---|
| `runner` | string | `''` | JSON runs-on labels; '' = the caller's lane (doc-refs.yml's runner input says which). |

## hidden-unicode.yml

The hidden-Unicode gate + its selftest; `args` for `--binary-suffix`.

```yaml
jobs:
  hidden-unicode:
    uses: mjdevaccount/ci-kit/.github/workflows/hidden-unicode.yml@v1
    with:
      args: --binary-suffix .age   # optional; a binary format the kit's list lacks
```

| input | type | default | what |
|---|---|---|---|
| `runner` | string | `''` | JSON runs-on labels; '' = the caller's lane (doc-refs.yml's runner input says which). |
| `args` | string | `''` | extra ci-hidden-unicode flags (--binary-suffix .EXT, repeatable); none by default |

## declared-literal.yml

The declared-literal gate + its selftest; `args` (required) is the caller's policy, normally
`@ci/literals.args` ([gates/secrets.md](gates/secrets.md)). The workflow asserts the scan receipt
(`CI_KIT_DECLARED_LITERAL_RECEIPT`), so exit 0 comes only from a scan that read files.

```yaml
jobs:
  literals:
    uses: mjdevaccount/ci-kit/.github/workflows/declared-literal.yml@v1
    with:
      args: "@ci/literals.args"   # required: the policy is yours
```

| input | type | default | what |
|---|---|---|---|
| `runner` | string | `''` | JSON runs-on labels; '' = the caller's lane (doc-refs.yml's runner input says which). |
| `args` | string | **required** | the caller's policy, normally `@<path to args file>`; plain flags, no quotes |

## gitleaks.yml

Secret scan, `mode: tree / history / range` (`action` = `range`, the old name). Every mode is
`ci-gitleaks` (v1.17.0: gitleaks pinned by version + sha256 through `release_bin`, no docker, no
gitleaks-action). `history` is HEAD's ancestry, never `--all` (a fixture on another open branch
must not fail THIS PR), and zero commits scanned is exit 2. Every `.gitleaksignore` gitleaks loads
(`./`, `<source>/`, and `ignore-path`, a file or a folder) is judged, and a dead entry (an absolute
`/repo/` path, a malformed or trailing-comment fingerprint) is exit 2, so `ignore-path: ''` still
checks the repo's own baseline.

```yaml
jobs:
  gitleaks:
    name: gitleaks (secret scan)
    permissions: { contents: read }
    uses: mjdevaccount/ci-kit/.github/workflows/gitleaks.yml@v1
    with: { mode: tree }
```

| input | type | default | what |
|---|---|---|---|
| `mode` | string | `tree` | tree \| history \| range (action = range, the old name) |
| `config` | string | `''` | gitleaks config path relative to the repo root ('' = the built-in ruleset) |
| `ignore-path` | string | `''` | an extra .gitleaksignore (file, or a folder holding one) relative to the repo root. '' does NOT mean no baseline: gitleaks always loads ./.gitleaksignore and <source>/.gitleaksignore when present, and ci-gitleaks judges every file it loads -- a dead entry ... |
| `image` | string | `''` | IGNORED since v1.16.0 (no docker; the gitleaks pin is ci_kit/gitleaks.py). |

## version-tag.yml

`ci-version-tag` for a version-first repo ([versioning.md](versioning.md)). The caller declares
push-to-main + a daily cron + `workflow_dispatch` and `contents: write, actions: write`.

```yaml
on:
  push: { branches: [main] }
  schedule: [{ cron: '41 7 * * *' }]
  workflow_dispatch: { inputs: { dry_run: { type: boolean, default: false } } }
permissions: {}
jobs:
  tag:
    permissions: { contents: write, actions: write }
    uses: mjdevaccount/ci-kit/.github/workflows/version-tag.yml@v1
    with: { release-workflow: release.yml, github-release: true, pypi: <project>,
            dry-run: ${{ inputs.dry_run || false }} }
```

| input | type | default | what |
|---|---|---|---|
| `runner` | string | `''` | runs-on as a JSON label list ('' = the caller repo's CI_RUNNER_LIGHT, else CI_RUNNER, else ubuntu-latest). |
| `release-workflow` | string | `''` | workflow file dispatched on a new or unreleased tag ('' = tag only) |
| `github-release` | boolean | `false` | a tag counts as released only with a GitHub release |
| `pypi` | string | `''` | a tag counts as released only with this project's files on PyPI ('' = no check) |
| `since` | string | `''` | ignore versions below this one (history from before the repo tagged) |
| `dry-run` | boolean | `false` | print the plan, write nothing |

## major-tag.yml

Moves each floating `vX` to its highest `vX.Y.Z` on every release tag ([versioning.md](versioning.md)).
Also reusable: checkout and the token are the caller's, so the caller declares the triggers (a
`vX.Y.Z` tag push, `workflow_dispatch`, a daily cron) and `contents: write`. No inputs, no secrets.

```yaml
on:
  push: { tags: ['v[0-9]+.[0-9]+.[0-9]+'] }
  schedule: [{ cron: '23 6 * * *' }]
  workflow_dispatch:
permissions: { contents: write }
jobs:
  major:
    uses: mjdevaccount/ci-kit/.github/workflows/major-tag.yml@v1
```

## ci-green.yml

`ci-green` on `workflow_run` of the caller's CI ([gates/supply-chain.md](gates/supply-chain.md)).
Advances only on a `success` run of a `push` / `workflow_dispatch` on the DEFAULT branch from the
same repo; checks out that run's `head_sha` at full depth. Pushes with the caller's PAT (secret
`token`: contents + workflows RW; GITHUB_TOKEN is refused any push whose range touches
`.github/workflows/**`), so it runs hosted or on the SECURE pool (`ci-secure` environment), never
the socket pool. A PAT push fires `push` workflows on ci-green: keep every push trigger filtered
to main.

```yaml
name: ci-green
on:
  workflow_run:
    workflows: [CI]          # YOUR CI workflow's `name:`
    types: [completed]
    branches: [main]
permissions: {}
concurrency: { group: ci-green, cancel-in-progress: false }
jobs:
  advance:
    permissions: { contents: read }
    uses: mjdevaccount/ci-kit/.github/workflows/ci-green.yml@v1
    secrets:
      token: ${{ secrets.YOUR_PUSH_PAT }}
      app-private-key: ${{ secrets.YOUR_APP_PRIVATE_KEY }}   # optional (token-source)
```

| input | type | default | what |
|---|---|---|---|
| `branch` | string | `ci-green` | the branch to fast-forward (never the default branch, main or master) |
| `dry-run` | boolean | `false` | print the plan, push nothing |
| `token-source` | string | `''` | pat \| app \| app-required ('' = the caller repo's PR_TOKEN_SOURCE variable, else pat) -- ci_kit/pr_token.py (v1.37.0) |
| `app-client-id` | string | `''` | the App's client id, with the app-private-key secret ('' = deprecated EDGE_BOT_APP_ID) -- docs/pr-ops/push-token.md |
| `secure-runner` | string | `''` | runs-on of the `advance` job (it holds the write token), a JSON label list ('' = the CI_SECURE_RUNNER switch, else hosted) -- docs/pr-ops/push-token.md |

| secret | what |
|---|---|
| `token` | fine-grained PAT with contents + workflows read/write on the caller repo (not required since v1.37.0; empty while the PAT acts is still red) |
| `app-private-key` | the App's private key, with the app-client-id input -- docs/pr-ops/push-token.md |
| `EDGE_BOT_PRIVATE_KEY` | DEPRECATED alias of app-private-key (removed in v2) |

## pr-autoupdate.yml

The armed-PR updater ([pr-ops/autoupdate.md](pr-ops/autoupdate.md)); output `relock` and `heal`.
The full caller template (push, `pull_request_target` `auto_merge_enabled` + `synchronize`,
`workflow_run`, `workflow_dispatch`, and the job's `if:`) is the file's header.

```yaml
on:
  push: { branches: [main] }
  pull_request_target: { types: [auto_merge_enabled, synchronize] }
  workflow_dispatch:
concurrency: { group: pr-autoupdate, cancel-in-progress: false }
permissions: { contents: read, pull-requests: read, checks: read, statuses: read }
jobs:
  update:
    uses: mjdevaccount/ci-kit/.github/workflows/pr-autoupdate.yml@v1
    with: { max-in-flight: 1, required-checks: "test" }
    secrets:
      token: ${{ secrets.YOUR_PR_UPDATE_TOKEN }}
      ntfy-url: ${{ secrets.YOUR_NTFY_URL }}   # optional
```

| input | type | default | what |
|---|---|---|---|
| `review-workflow` | string | `''` | workflow file whose queued/running run on a PR's branch defers its update ('' = none) |
| `tracker` | number | `0` | issue number whose row for a PR a conflict page quotes (0 = none) |
| `relock` | boolean | `false` | the caller relocks lockfile-only conflicts from the `relock` output |
| `label` | string | `conflict` | the conflict label (the page dedup) |
| `max-in-flight` | number | `0` | armed PRs tested against current main at once, lowest PR number first (0 = update every armed PR). Under strict protection only one PR merges per main sha. |
| `required-checks` | string | `''` | comma-separated check names the queue reads (your branch protection's required contexts; '' = every check) |
| `rerun-limit` | number | `2` | smart auto-merge (ci_kit/pr_heal.py): a red on an armed PR that is MACHINERY (a lost runner, a cancelled run, a setup step's network error) is rerun at most this many times per head; a JUDGMENT red gets one PR comment and is never rerun. 0 = off. |
| `scarce-pools` | string | `''` | the PR scheduler (ci_kit/pr_sched.py): a JSON object of the caller's scarce runner pool labels -> capacity, e.g. {"home-sealed": 1, "home-heavy": 1}. '' = off (parked work is still re-run). |
| `preempt-after-minutes` | number | `3` | a non-head job on a scarce pool is parked for a waiting head only if it started less than this ago |
| `head-stall-minutes` | number | `15` | the queue head waiting this long on a pool a non-head job holds turns the run red (queue-head-starved); twice this, the next parked non-head is PARKED STARVED (named every pass and paged once per head, the run stays green, v1.35.0) |
| `train-workflow` | string | `''` | the caller's merge-train workflow file (e.g. merge-train.yml): each pass dispatches it when the train would find work (ci_kit/train_kick.py), so its cron can be an hourly backstop. '' = off. |
| `train-min-batch` | number | `2` | the train's own min-batch (dispatch only with at least this many candidates) |
| `train-stale-minutes` | number | `90` | the merge train's own `stale-minutes` (v1.35.0): with `queue-hold`, an open merge train holds the queue head pending (so main cannot move under the train) for at most this long; a red, BEHIND or older train releases it, and the pass summary says so ... |
| `cancel-doomed-runs` | boolean | `true` | cancel the queued/running `pull_request` runs at a head that cannot merge and whose result nothing reads -- the head the queue just replaced; an armed DIRTY head on a strict base; an armed BEHIND head on a strict base in a repo with NO merge train ... |
| `queue-hold` | boolean | `false` | the QUEUE HOLD (ci_kit/pr_hold.py, v1.30.0): post commit status `queue/position` -- success on the queue head (and a merge train), pending on every other open PR -- so an out-of-turn auto-merge cannot land once branch protection requires `queue/position`. |
| `heal` | boolean | `false` | the HEAL LADDER (ci_kit/pr_ladder.py, v1.33.0): conflicts and judgment reds on armed PRs go to output `heal` (one row per dispatch, at most one per head) for the caller's pr-heal.yml call, instead of a comment / a page. |
| `heal-claude-per-pass` | number | `1` | tier-3 (Claude) dispatches per pass; a PR over it waits for the next pass (0 = tier 3 off) |
| `heal-claude-per-pr` | number | `2` | tier-3 dispatches per PR, across all its heads |
| `heal-per-pr` | number | `6` | heal dispatches per PR, across all its heads (a push-red-push loop ends in a page) |
| `heal-stale-minutes` | number | `135` | a dispatch with no heal record after this long is paged by the next pass (default = every pr-heal job timeout, 95, plus 40 for the sealed-runner wait; ci_kit/pr_ladder.py STALE_MIN) |
| `base-retry-cap` | number | `1` | base-driven retries (since the PR's last own commit) of an armed PR held red + BEHIND on a check MAIN went red -> green on (ci_kit/pr_baseretry.py; once per head, after every green PR; at the cap a marker comment + a red pass; 0 = off; |
| `base-retry-max-per-pass` | number | `3` | base-retry candidates whose main runs are read per pass, lowest PR number first (the rest are named in the summary and wait for the next pass); 1..100 |
| `token-source` | string | `''` | WHICH TOKEN ACTS (v1.37.0, ci_kit/pr_token.py): pat \| app \| app-required ('' = the caller repo's PR_TOKEN_SOURCE variable, else pat). app mints a GitHub App token (variable EDGE_BOT_APP_ID, secret EDGE_BOT_PRIVATE_KEY) and falls back to `token` only when ... |
| `base-retry-bot-authors` | string | `renovate` | comma-separated EXACT author logins (as GraphQL reports them, e.g. Renovate's app is `renovate`; the author must also be a Bot account) retried without red-at-base evidence ([autoupdate.md](pr-ops/autoupdate.md#base-driven-retry)) |
| `app-client-id` | string | `''` | the App's client id, with the app-private-key secret ('' = deprecated EDGE_BOT_APP_ID) -- docs/pr-ops/push-token.md |
| `secure-runner` | string | `''` | the secure pool's runs-on, a JSON label list ('' = the CI_SECURE_RUNNER switch, else hosted) -- docs/pr-ops/push-token.md |
| `head-fail-fast` | boolean | `true` | HEAD FAIL-FAST (v1.45.0, ci_kit/pr_sched.py review_young): with scarce-pools, a queue head BEHIND the base is updated at once while its review has spent under preempt-after-minutes of model time, so its one full CI run is at a head that can merge; false = ... |
| `head-verdict-wait-seconds` | number | `240` | with head-fail-fast: seconds the pass's tail waits for a BEHIND queue head past the young bound to record its verdict, then updates it in the same job (ci_kit/pr_autoupdate.py _await_head); 0 = off. |
| `update-held` | boolean | `false` | also merge the base into HELD PRs (open, not armed, BEHIND, mergeable, no check running or failing, not a bot's) on schedule / workflow_dispatch passes only, last, once no armed PR is on its way to current, in the queue's free slots (ci_kit/pr_held.py) |

| secret | what |
|---|---|
| `token` | fine-grained PAT with contents, pull requests and workflows RW, actions read (RW with scarce-pools -- the scheduler cancels and re-runs --, with train-workflow, which dispatches, and with cancel-doomed-runs), administration read (cancel-doomed-runs reads ... |
| `app-private-key` | the App's private key, with the app-client-id input -- docs/pr-ops/push-token.md |
| `EDGE_BOT_PRIVATE_KEY` | DEPRECATED alias of app-private-key (removed in v2) |
| `ntfy-url` | an ntfy topic URL to page on a NEW conflict ('' = no page) |

Outputs: `relock`, `heal`.

## pr-heal.yml

The heal ladder's tiers 1-4 for ONE PR, called per row of pr-autoupdate's `heal` output
([pr-ops/heal-ladder.md](pr-ops/heal-ladder.md)).

```yaml
  heal:
    needs: update
    if: always() && needs.update.outputs.heal != '' && needs.update.outputs.heal != '[]'
    strategy:
      fail-fast: false
      max-parallel: 2
      matrix:
        include: ${{ fromJSON(needs.update.outputs.heal) }}
    permissions: { contents: read, pull-requests: read, actions: read }
    uses: mjdevaccount/ci-kit/.github/workflows/pr-heal.yml@v1
    with:
      pr: ${{ matrix.pr }}
      head: ${{ matrix.head }}
      mode: ${{ matrix.mode }}
      claude: ${{ matrix.claude }}
      checks: ${{ matrix.checks }}
      runs: ${{ matrix.runs }}
      install: uv sync --all-groups --frozen
    secrets:
      token: ${{ secrets.YOUR_PR_UPDATE_TOKEN }}
      ntfy-url: ${{ secrets.YOUR_NTFY_URL }}                            # tier 4
      claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}   # tier 3 (or anthropic-api-key)
```

| input | type | default | what |
|---|---|---|---|
| `pr` | number | **required** | the PR number |
| `head` | string | **required** | the PR head sha the ladder dispatched for (a moved head is a new budget: skipped) |
| `mode` | string | **required** | conflict \| red |
| `claude` | boolean | `false` | tier 3 allowed for this dispatch (the pass's budget); the plan re-checks it |
| `checks` | string | `''` | the red required checks (red mode), for the record, the page and tier 3 |
| `runs` | string | `''` | the red workflow run ids (red mode): tier 3 reads their failed jobs' logs |
| `regen` | string | `''` | the caller's regen map, JSON (ci_kit/pr_regen.py): [{"paths": [...], "run": "...", "on": ["conflict", "red"], "touches": [...], "shrink": false}]. |
| `install` | string | `''` | installs the caller's project before the generators and tier 3 ('' = none) |
| `python-version` | string | `''` | setup-uv python-version ('' = uv's choice) |
| `dotnet-version` | string | `''` | setup-dotnet version for a NuGet relock or a .NET project ('' = only when a lockfile needs it, 10.0.x) |
| `model` | string | `claude-opus-5-5` | tier 3's model |
| `fix-commands` | string | `''` | command prefixes tier 3 may run with Bash, one per line ('' = ci_kit/pr_fix.DEFAULT_COMMANDS) |
| `max-fix-lines` | number | `300` | tier 3's changed-line budget outside conflict resolutions; over it, refused and paged |
| `auth-mode` | string | `''` | subscription \| api \| auto ('' = the caller repo's REVIEW_GATE_AUTH variable, else auto) |
| `claude-code-version` | string | `2.1.332` | tier 3's pinned Claude Code CLI |
| `kit-ref` | string | `v1` | the ci-kit ref whose ci_kit.pr_* modules run every job (a tag, branch or sha) |
| `git-ssh-repos` | string | `''` | PRIVATE repos the install fetches by https (ci_kit/git_ssh.sh), with the git-ssh-key secret |
| `token-source` | string | `''` | pat \| app \| app-required ('' = the caller repo's PR_TOKEN_SOURCE variable, else pat) -- ci_kit/pr_token.py (v1.37.0) |
| `app-client-id` | string | `''` | the App's client id, with the app-private-key secret ('' = deprecated EDGE_BOT_APP_ID) -- docs/pr-ops/push-token.md |
| `secure-runner` | string | `''` | the secure pool's runs-on, a JSON label list ('' = the CI_SECURE_RUNNER switch, else hosted) -- docs/pr-ops/push-token.md |
| `sealed-runner` | string | `''` | the sealed pool's runs-on, a JSON label list ('' = the CI_SEALED_RUNNER switch, else hosted) -- docs/pr-ops/push-token.md |

| secret | what |
|---|---|
| `token` | the push token (fine-grained PAT, contents + pull requests RW on the repo); comments too. |
| `app-private-key` | the App's private key, with the app-client-id input -- docs/pr-ops/push-token.md |
| `EDGE_BOT_PRIVATE_KEY` | DEPRECATED alias of app-private-key (removed in v2) |
| `ntfy-url` | tier 4's page topic ('' = not pushed, named in the record) |
| `claude-code-oauth-token` | tier 3, the subscription credential |
| `anthropic-api-key` | tier 3, the api credential |
| `git-ssh-key` | read-only deploy key(s) for git-ssh-repos, one per repo in order |

Outputs: `result`.

## relock.yml

`ci_kit/pr_relock.py` on a `pull_request` caller
([pr-ops/heal-ladder.md](pr-ops/heal-ladder.md#relocking-a-dependency-bots-pr), which has the full
caller and the private-dependency variant).

```yaml
on:
  pull_request:
    paths: [pyproject.toml, uv.lock, .github/workflows/relock.yml]
permissions: { contents: read }
jobs:
  relock:
    uses: mjdevaccount/ci-kit/.github/workflows/relock.yml@v1
    secrets:
      token: ${{ secrets.YOUR_PUSH_PAT }}
```

| input | type | default | what |
|---|---|---|---|
| `lockfiles` | string | `uv.lock` | the lockfiles to relock, repo-relative, space- or newline-separated; each a tracked file of a kind ci_kit/pr_regen.LOCKFILES knows (uv.lock, package-lock.json, npm-shrinkwrap.json, pnpm-lock.yaml, packages.lock.json), relocked in its own directory. |
| `branch-prefix` | string | `renovate/` | the dependency bot's branch prefix; only a same-repo PR whose head ref starts with it is relocked, matched case-INSENSITIVELY (GitHub's startsWith; ci_kit/pr_relock.refusal agrees). '' or any whitespace = red |
| `python-version` | string | `''` | setup-uv python-version ('' = uv's choice) |
| `dotnet-version` | string | `''` | setup-dotnet version for a NuGet relock ('' = 10.0.x, only when a packages.lock.json is listed) |
| `git-ssh-repos` | string | `''` | PRIVATE GitHub repos (`owner/name`, space- or newline-separated) the lockfile names by https URL; with the `git-ssh-key` secret the lock tool reaches them over ssh (ci_kit/git_ssh.sh: the keys live only in their own step, the tool gets the agents, which ... |
| `kit-ref` | string | `v1` | the ci-kit ref whose ci_kit modules run in both jobs (a tag, branch or sha) |
| `token-source` | string | `''` | pat \| app \| app-required ('' = the caller repo's PR_TOKEN_SOURCE variable, else pat) -- ci_kit/pr_token.py |
| `app-client-id` | string | `''` | the App's client id, with the app-private-key secret ('' = deprecated EDGE_BOT_APP_ID) -- docs/pr-ops/push-token.md |
| `secure-runner` | string | `''` | the secure pool's runs-on, a JSON label list ('' = the CI_SECURE_RUNNER switch, else hosted) -- docs/pr-ops/push-token.md |

| secret | what |
|---|---|
| `token` | the push token (fine-grained PAT, contents RW on the caller repo); empty while the PAT acts is red, only when a relock is needed |
| `app-private-key` | the App's private key, with the app-client-id input -- docs/pr-ops/push-token.md |
| `EDGE_BOT_PRIVATE_KEY` | DEPRECATED alias of app-private-key (removed in v2) |
| `git-ssh-key` | READ-ONLY deploy key(s) for `git-ssh-repos`, never a key that can write; one key per repo, in `git-ssh-repos` order (a `\|` block of `secrets.*` lines) |

Outputs: `result`.

## merge-train.yml

`ci-merge-train` on the caller's SECURE pool ([pr-ops/merge-train.md](pr-ops/merge-train.md)). The
full caller template (hourly backstop cron, dispatch, `pull_request_target` labeled/closed,
`workflow_run` of CI/review, and the job's `if:`) is the file's header.

```yaml
on:
  schedule: [{ cron: "7 * * * *" }]
  workflow_dispatch:
concurrency: { group: merge-train, cancel-in-progress: false }
permissions: { contents: read, pull-requests: read, checks: read, statuses: read, actions: read }
jobs:
  train:
    uses: mjdevaccount/ci-kit/.github/workflows/merge-train.yml@v1
    with:
      required-checks: "test,review gate"
    secrets:
      token: ${{ secrets.YOUR_PR_UPDATE_TOKEN }}   # a PAT: GITHUB_TOKEN pushes fire no CI
```

| input | type | default | what |
|---|---|---|---|
| `base` | string | `main` | the branch trains land on |
| `required-checks` | string | `''` | comma-separated required check names (branch protection's contexts; '' = every check) |
| `max-batch` | number | `8` | at most this many PRs per train, lowest PR number first |
| `min-batch` | number | `2` | a train runs only with at least this many ready PRs (>= 2; one PR is auto-merge's) |
| `stale-minutes` | number | `90` | a train whose checks are still pending after this long is closed and recomposed; also the bound on how long an open train holds the queue head (v1.35.0): set pr-autoupdate's `train-stale-minutes` to the same value |
| `review-workflow` | string | `review-gate.yml` | the caller's review-gate workflow file; a PR rides only if it passed at its head ('' = no pre-check) |
| `dry-run` | boolean | `false` | decide and report, push and change nothing |
| `token-source` | string | `''` | pat \| app \| app-required ('' = the caller repo's PR_TOKEN_SOURCE variable, else pat) -- ci_kit/pr_token.py (v1.37.0) |
| `app-client-id` | string | `''` | the App's client id, with the app-private-key secret ('' = deprecated EDGE_BOT_APP_ID) -- docs/pr-ops/push-token.md |
| `secure-runner` | string | `''` | runs-on of the `train` job (it holds the write token), a JSON label list ('' = the CI_SECURE_RUNNER switch, else hosted) -- docs/pr-ops/push-token.md |

| secret | what |
|---|---|
| `token` | fine-grained PAT with contents, pull requests, workflows and actions RW, on the caller repo (not required since v1.37.0; empty while the PAT acts is still red) |
| `app-private-key` | the App's private key, with the app-client-id input -- docs/pr-ops/push-token.md |
| `EDGE_BOT_PRIVATE_KEY` | DEPRECATED alias of app-private-key (removed in v2) |

## review-gate.yml

The pre-merge model review gate ([review-gate.md](review-gate.md), which has the full caller with
its `review gate` aggregate).

```yaml
jobs:
  review:
    name: reviewer
    uses: mjdevaccount/ci-kit/.github/workflows/review-gate.yml@v1
    with:
      guard-paths: |
        - 'migrations/**'
    secrets:
      claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

| input | type | default | what |
|---|---|---|---|
| `guard-paths` | string | **required** | The caller's guard paths as dorny/paths-filter globs, one YAML list item per line (`- 'migrations/**'`). |
| `label` | string | `review:required` | the PR label that adds a reason; must be on the PR AT OPEN (no `labeled` trigger) |
| `charter` | string | `If the repository has a REVIEW.md, read it first and follow it.` | the repo's review charter, appended to the prompt (which files to read, what to hunt) |
| `install` | string | `uv sync --all-groups --frozen` | the command that installs the caller's project, so the reviewer can run its tests |
| `python-version` | string | `''` | setup-uv python-version for the caller's project ('' = uv's choice) |
| `model` | string | `claude-sonnet-5-5` | the reviewer's model |
| `guard-model` | string | `claude-opus-5-5` | the reviewer's model when a guard path is in the diff |
| `not-prose` | string | `.claude/ prompts/` | space-separated path prefixes never read as prose by the docs-only skip |
| `auth-mode` | string | `''` | subscription \| api \| auto ('' = the caller repo's REVIEW_GATE_AUTH variable, else auto) |
| `claude-code-version` | string | `2.1.332` | the subscription transport's pinned Claude Code CLI |
| `kit-ref` | string | `v1` | the ci-kit ref whose ci_kit.review grades the PR (a tag, branch or sha) |
| `git-ssh-repos` | string | `''` | PRIVATE GitHub repos (`owner/name`, space- or newline-separated) the caller's `install` fetches by https URL; with the `git-ssh-key` secret they are routed to ssh for the Install step only (ci_kit/git_ssh.sh). |
| `train-workflow` | string | `merge-train.yml` | the caller's merge-train workflow file: a `train/` PR passes only if its composition record names a run of THIS workflow that opened it (ci_kit/merge_train/github.composed) |
| `queue-priority` | boolean | `false` | the PR scheduler's admission (v1.29.0, ci_kit/pr_sched.py): a full review of a PR numbered after the queue head is PARKED while the head needs the sealed pool, and re-run by pr-autoupdate (pass its `scarce-pools`). |
| `secure-runner` | string | `''` | the secure pool's runs-on, a JSON label list ('' = the CI_SECURE_RUNNER switch, else hosted) -- docs/pr-ops/push-token.md |
| `sealed-runner` | string | `''` | the sealed pool's runs-on, a JSON label list ('' = the CI_SEALED_RUNNER switch, else hosted) -- docs/pr-ops/push-token.md |

| secret | what |
|---|---|
| `git-ssh-key` | optional; a read-only (deploy) key for `git-ssh-repos`, held only by the step that loads it into an ssh-agent; |
| `claude-code-oauth-token` | the `subscription` credential (`claude setup-token`) |
| `anthropic-api-key` | the `api` credential |
| `ntfy-url` | optional; a block or BLIND is pushed here (priority 3) |

Outputs: `result`.

## app-audit.yml

`ci-app-audit` over the caller's App manifest, then its selftest
([github-apps.md](github-apps.md)). Run it on a schedule: an App's grants and installations change
on GitHub's settings pages, which no commit or run records, so the red run is the whole point.
The key reaches the tool through the environment only.

```yaml
on:
  schedule: [{cron: "17 6 * * *"}]
  workflow_dispatch:
jobs:
  apps:
    uses: mjdevaccount/ci-kit/.github/workflows/app-audit.yml@v1
    with:
      manifest: ci/github-apps.yml
      keyed-app: example-bot
      app-level: other-app
    secrets:
      app-private-key: ${{ secrets.EXAMPLE_BOT_PRIVATE_KEY }}
```

| input | type | default | what |
|---|---|---|---|
| `manifest` | string | `ci/github-apps.yml` | the App manifest (YAML or JSON) in the caller's checkout |
| `keyed-app` | string | `''` | the name (slug) of the App whose private key is the `app-private-key` secret ('' = none) |
| `app-level` | string | `''` | space-separated App names audited WITHOUT a key (permissions and events only; installations not read) |
| `only` | string | `''` | space-separated App names to audit ('' = every App in the manifest) |
| `secure-runner` | string | `''` | the secure pool's runs-on, a JSON label list ('' = the CI_SECURE_RUNNER switch, else hosted) -- docs/pr-ops/push-token.md |

| secret | what |
|---|---|
| `app-private-key` | the keyed App's private key (PEM) |
| `app-level-token` | a token that can read the app-level Apps (GET /apps/<slug>); default the job's own token |
