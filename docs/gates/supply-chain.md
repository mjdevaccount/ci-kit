# Pins, releases and runners

Gates and helpers that keep what a repo runs current and fetchable: kit pins that lag, a deploy
branch that only holds green commits, third-party binaries pinned by digest, private git
dependencies in CI, runner queue time, and the shared Renovate preset.

| command / file | module | what |
|---|---|---|
| `ci-pin-drift` | `ci_kit/pin_drift.py` | every exact pin of a kit is its newest release tag |
| `ci-green` | `ci_kit/ci_green.py` | fast-forward a `ci-green` branch to the commit CI just passed on the default branch |
| `ci-tf-seed` | `ci_kit/tf_seed.py` | fill terraform's provider cache from the lock file, off GitHub's download path |
| `ci-queue-wait` | `ci_kit/queue_wait.py` | p90 time a workflow's runs waited for a runner |
| library | `ci_kit/release_bin.py` | a third-party CLI pinned by version + sha256 per platform |
| shell | `ci_kit/git_ssh.sh`, `ci_kit/ssh_retry.sh` | a private GitHub git dependency in a runner install |
| preset | `renovate/default.json5` | one shared Renovate policy |

## `ci-pin-drift`

Every `<kit>@vX.Y.Z` / `?tag=vX.Y.Z` / `<kit>/releases/download/vX.Y.Z/` / PEP 508
`<kit>[extras]==X.Y.Z` pin (an index pin, PEP 503 name, read as tag `vX.Y.Z`; v1.12.0) in the
given files is the newest release tag on `--remote`, else exit 1. A kit fix that never reaches a
consumer must not fail silently.

- Moving refs (`@v1`) and SHA pins are deliberately NOT pins here: `v1` cannot lag by
  construction, and a SHA says nothing comparable. Neither is a range or a pre-release.
- `--remote` is required (no default), and the kit's name in a pin is the remote's last path
  component (`--name` to override). The newest release is the highest `vX.Y.Z` tag on the remote
  (`git ls-remote --tags`), read with the same credential that installs the package.
- `--grace-hours` (default 24 since v1.21.0, 48 since v1.35.0): a lag whose EVERY missing release
  is dated and younger than that (by DATE, not version) is printed PENDING and exits 0: the bump
  is Renovate's, not yet landed, so a daily cron no longer goes red the day after every kit
  release. An unreadable or future release date, a private kit (no credential in the date fetch)
  and a phantom pin get no grace; `0` = strict.
- A remote read (`ls-remote`, the date fetch) is tried three times at 40 s each (v1.47.0): a
  stalled connection is a printed failed try and, when a later try answers, a `::warning::`, not a
  red "could not look". Each date-fetch try uses its own bare repo.
- A file that pins nothing is exit 2 (ci-kit's own CI runs that blind direction).

```bash
uvx --from git+https://github.com/mjdevaccount/ci-kit@v1.51.0 ci-pin-drift --file uv.lock --remote https://github.com/mjdevaccount/ci-kit.git
```

## `ci-green`

Fast-forward a `ci-green` branch to the commit CI just PASSED on the default branch (v1.22.0), so a
host that BUILDS from its checkout follows `origin/ci-green` instead of `origin/main` and never
deploys a merge whose push CI failed. The host needs no GitHub credential to read it.

| state | action | exit |
|---|---|---|
| sha not on the default branch's history | nothing | 1 |
| branch absent | create it at the sha | 0 `created` |
| branch == sha | nothing | 0 `current` |
| branch is an ancestor | push the sha, NON-force | 0 `advanced` |
| sha is an ancestor (an older run finished last) | nothing | 0 `older` |
| diverged (someone pushed it by hand, or main was rewritten) | nothing, never forced | 1 |

It refuses the default branch, `main` and `master` as the target; `--dry-run` prints the plan;
exit 2 when it could not look. Reusable workflow: `ci-green.yml`
([reusable-workflows.md](../reusable-workflows.md#ci-greenyml)).

## `ci-tf-seed`

Fill terraform's provider cache from `.terraform.lock.hcl` without GitHub's release-download path.
A cold `terraform init` fetches each provider's SHA256SUMS, `.sig` and zip from wherever the
registry points; for a provider hosted on GitHub releases that endpoint 504'd for over an hour
(2026-11-08) and init failed. A WARM init does not touch it. So this fills the cache first,
through `release_bin.fetch` (retry plus the REST asset fallback).

- For each `provider` block: if `$TF_PLUGIN_CACHE_DIR/<host>/<ns>/<type>/<version>/<os>_<arch>/`
  exists, nothing. Otherwise ask the registry's download endpoint for the zip URL and its sha256,
  fetch, refuse a sha256 mismatch, unpack beside the target and rename in.
- With no `TF_PLUGIN_CACHE_DIR` it uses `$RUNNER_TEMP/tf-plugin-cache` and exports it through
  `$GITHUB_ENV`.
- Trust: the registry's sha256 judges the download; terraform (>= 1.4) then uses a cached provider
  only when its hash is in the committed lock file. The registry signature is not checked here.
- Exit 0 seeded or already cached, 1 a sha256 mismatch, 2 did not finish (no lock file, a fetch
  that failed after the retries, a registry answer it cannot read).

## `ci-queue-wait`

The p90 time one workflow's runs waited for a runner over the last N hours; exit 1 over
`--max-p90` (or the p95 limit).

- `--event pull_request --branch=` (empty = every branch) measures PR runs; the run list is paged
  up to `--max-runs`, newest first, and a capped sample says so.
- A job cancelled while still queued is scored as a wait of AT LEAST its
  `completed_at - created_at` (a lower bound), not as 0 and not dropped.
- `--unit run` (earliest picked-up job vs the run attempt's start) or `--unit job` (every job's own
  wait).
- Too few waits in `--hours` doubles the window up to `--widen-to-hours` (default 168) and gates
  the widened sample. NO completed run at the cap is `IDLE`, exit 0, only while the filter's
  newest completed run is under `--stale-days` (30) old. A filter that never matched or went
  stale, runs with nothing to measure, a thin sample within the limits and any exception stay
  exit 2; a thin sample over a limit exits 1 (v1.44.0: a quiet Sunday had read red).

## `release_bin`: third-party CLIs pinned by digest

A third-party CLI pinned by **version + sha256 per platform**, fetched once into
`$CI_KIT_CACHE_DIR` (default `~/.cache/ci-kit`, `<name>-<version>-<asset>/`); `$CI_KIT_<NAME>`
(e.g. `CI_KIT_OASDIFF`) points a gate at a binary you already have. jscpd, oasdiff and gitleaks
each state only a `Pin`.

- A 5xx or network error is retried 3x with backoff (a 4xx is not), and each failed attempt also
  tries the REST asset API path: the two GitHub download paths fail apart. The composite action
  passes the job token as `CI_KIT_GITHUB_TOKEN` for that fallback.
- The binary is written to a temp file and `os.replace`d in, so a cache shared by concurrent
  runners is safe.
- Any failure to obtain it is exit 2: a gate whose tool could not be fetched looked at nothing.
- Each version line carries `# renovate: datasource=github-releases depName=...`, which the
  Renovate preset reads. Renovate moves the VERSION, so its PR fails the selftest on the sha256
  until the hashes move in the same PR: a new binary never runs on an unreviewed digest. The heal
  ladder's tier 2 recomputes the table (`ci_kit/pin_rehash.py`,
  [pr-ops/heal-ladder.md](../pr-ops/heal-ladder.md)).

## `git_ssh.sh`: a private git dependency in CI

A PRIVATE GitHub git dependency in a runner install (the review gate, v1.24.0), in three phases =
three steps:

1. **`load`**, the ONLY step holding `CI_KIT_GIT_SSH_KEY` (no PR code runs there), starts an
   `ssh-agent`, adds the key from stdin, and writes GitHub's published host keys to a file of its
   own.
2. **`. git_ssh.sh route`**, in the install step (no key in its env: a build backend could read it
   from the parent shell's `/proc/<pid>/environ` even after `unset`), rewrites only
   `CI_KIT_GIT_SSH_REPOS` (`owner/name`) from https to ssh through env-scoped `GIT_CONFIG_COUNT`
   with `StrictHostKeyChecking=yes`.
3. **`kill`** (`always()`; a zombie counts as dead) stops the agent before the step that runs the
   PR's tests.

The agent starts without the key in its environment, but the key is in its MEMORY while Install
runs: code with root (a hosted runner's passwordless sudo) can dump it, so the key must be
read-only and scoped to one repo. `webfactory/ssh-agent` was not used because its agent lives to
the job's end.

- **Several private repos, one deploy key each (v1.28.0).** A deploy key reads ONE repo and GitHub
  takes the first key an agent offers, so `CI_KIT_GIT_SSH_KEY` may hold N concatenated keys for
  the N repos of `CI_KIT_GIT_SSH_REPOS`, paired by position (a `|` block of N secrets). Each key
  gets its own agent and a host alias `ci-kit.<owner>.<name>` (HostName github.com, HostKeyAlias
  github.com, IdentityAgent = its agent) in a config `load` writes (`ssh-config=`, which `route`
  reads from `CI_KIT_GIT_SSH_CONFIG`), plus `agents=` (JSON repo to socket, for a BuildKit `ssh:`
  id per repo); `pid=` lists every agent and `kill` stops them all. One key with several repos is
  the old shape, unchanged. Any other count, the same key twice, or a route for a repo `load`
  never named is red.
- Repos named with an EMPTY key (a fork or Dependabot PR, an unmapped secret), a key with no repo,
  a malformed repo, or a key `ssh-add` refuses is an `::error::` and exit 1 before the install
  (`tests/test_git_ssh_must_fire.py`).
- **Stalled and failed connections.** The routed `GIT_SSH_COMMAND` ends a stalled connection
  (v1.47.0: `ConnectTimeout=20`, `ServerAliveInterval=15` x 3). Since v1.50.1 it runs through
  `ci_kit/ssh_retry.sh`, which re-runs ONLY a connection that never came up: an ssh exit 255 whose
  stderr holds one of OpenSSH's connect-phase lines, anchored (`ssh: connect to host ... port N:
  ...`, `ssh: Could not resolve hostname`, `Connection timed out during banner exchange`,
  `kex_exchange_identification:`), before any of git's stdin was read. Each retry and give-up is
  also appended to `$GITHUB_STEP_SUMMARY` (uv drops git's stderr when the fetch then succeeds),
  and a signal to the wrapper reaches ssh with its stderr still printed. `CI_KIT_SSH_TRIES`
  (default 3) bounds the attempts and the pause is n x `CI_KIT_SSH_PAUSE` s (default 2), so a host
  that is really down costs a connection up to 66 s, not 20. Auth, host-key and mid-session
  failures, and the remote command's own exit, are returned on the first try
  (`tests/test_ssh_retry_must_fire.py`). Added after a review-gate run went BLIND on one banner
  timeout (2026-11-28).

## `renovate/default.json5`: the shared Renovate preset

ONE Renovate policy for every consumer. A public repo's `renovate.json` is one line:

```json
{ "extends": ["github>mjdevaccount/ci-kit//renovate/default.json5"] }
```

It lives in a public repo because Renovate gives a public repo a token scoped to itself, which
cannot read a preset in a private repo. A private repo may extend a private preset that is
itself one line pointing here, never a second copy. It is read from `main`, so a merge here
changes every consumer; ci-kit's CI runs `renovate-config-validator --strict` on it.

- **Window:** all Saturday plus Sunday until 18:00 ET; a 3-day release age.
- **First-party kits** (`mjdevaccount`) are exempt from both, but since v1.35.0 land in ONE daily
  window, 04:00-07:59 ET, so a day's releases make one PR per kit per repo.
- **Lock-file maintenance** once a week, Saturday 00:00-05:59 ET; its branch is untouched outside it.
- **Auto-merge:** non-major updates, CI-action majors and compose minors auto-merge once EVERY
  check is green (`platformAutomerge` off, so Renovate merges only after every check run, required
  or not). Other majors wait for review; stateful images are held; security updates go at any time.
- **CI-only tooling** (third-party `uses:` pins and `# renovate:` pins in workflows and `ci_kit/`,
  majors included) skips the age wait and the weekend for the daily window (ci-kit#139 -> #144;
  `tests/test_renovate_ci_tooling.py`). Never a workflow's docker `services:` image.
- **Serial-queue repos** (one `matchRepositories` rule): Renovate neither merges
  (`automergeType: pr-comment`) nor rebases on behind (`rebaseWhen: conflicted`); the PR queue
  alone makes a PR current and lands it ([pr-ops/autoupdate.md](../pr-ops/autoupdate.md)).
- **`customManagers`** bump first-party kit pins outside `uses:` (the shapes `ci-pin-drift` reads)
  and `# renovate: datasource=... depName=...` annotated pins
  (`tests/test_renovate_annotation_must_fire.py`).
