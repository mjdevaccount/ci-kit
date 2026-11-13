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
| `ci_kit/pr_autoupdate.py` | `ci-pr-autoupdate`: merge main into every open PR with auto-merge **armed**, so "require branches up to date" is affordable; a conflict gets one label and one page, never more. `--max-in-flight N` makes it a **serial queue** (`ci_kit/pr_queue.py`): at most N armed PRs tested against current main at once, lowest PR number first, skipping a PR whose own checks are red or still running. Under strict protection only one PR merges per main sha, so updating all of them threw away 70% of PR CI runs (trade-data, 2026-10-24). A PR skipped for a running review hands its slot to the next in line, and a `conflict` label comes off any PR GitHub reports mergeable again (v1.26.1) **Smart auto-merge (v1.27.0, `--rerun-limit`, default 2, 0 = off):** an armed PR red at its head is classified by its JOBS (`ci_kit/redclass.py`): MACHINERY (a lost runner, a log GitHub no longer has, a cancelled run, a setup step's network error, an alls-green aggregator red only because of those) is rerun (`rerun-failed-jobs`, needs the token's Actions: Read and write) at most N times per head, or simply updated when BEHIND; JUDGMENT (any of the PR's own steps failed) gets ONE PR comment naming the class and nothing else. Every action is a PR-comment marker (`<!-- smart-merge:rerun\|exhausted\|judgment head=... -->`, the budget and the record); a refused rerun or a spent budget turns the run red. `ci_kit/pr_heal.py` |
| `ci_kit/pr_list.py` | library: the updater's open-PR list as ONE narrow GraphQL query (check-run name/status/conclusion, status context/state, nothing more), returned in `gh pr list --json` shape. `gh pr list --json statusCheckRollup` also selects each check's workflow run, which a token without `actions: read` is refused (v1.7.0 still was, live 2026-10-24) |
| `ci_kit/merge_train/` | `ci-merge-train` (v1.25.0; run cancel v1.26.0): batch the armed, reviewed, green-but-behind PRs into ONE `train/<run>` PR -- one squash commit per PR (title `(#N)`, body, co-authors, `Merge-Train-PR`/`-Head` trailers), lowest PR number first, 2..8 -- CI runs once; a green train is NOT auto-merged -- the pass re-checks every rider at land time (open, armed, not a draft, no `autoland-off`, head unchanged, review still passing on this base) and only then lands it by REBASE (per-PR history kept), each constituent closed `landed-via-train` with its main sha; red ejects the newer half (n>=4, else all) back to individual auto-merge, unless the red is machinery (a check that never started, a BLIND gate): then a bounded retry, never an ejection; stale/stuck recomposes (stale is never held against a rider; a rider released from 2 STUCK trains at one head is held until its head changes). A rider boards only if its review passed against THIS base (its passing run was created after the PR's last base change, from the PR timeline's `filteredCount` + `BaseRefChangedEvent`; a timeline GitHub will not answer skips that rider, BLIND, never the pass) and its diff lands with the context the review read; a rider skipped for moved context is labelled `train-needs-update`, which pr-autoupdate updates even while a train is open. The pass acts only on a train it composed (a composition record in the PR body, checked against its own run). The review gate verifies a train instead of re-reviewing it (`ci_kit/review/train.py`, same checks plus the record). Every action is a PR comment (riders carry `rode`/`released`/`landed` markers) + the run summary; a stuck train, a retry, a hold, a green train GitHub would not merge turn the run red. Every train close (stale, stuck, red, land-time recheck, superseded) goes through ONE `ops.close_train`, which also cancels the train branch's unfinished workflow runs (v1.26.0; a closed train's CI held trade-data's one heavy runner ~25 min, 2026-11-12) -- never the train workflow's own runs, never an open PR's; a cancel GitHub refuses turns the run red. Free: GitHub's merge queue needs an org, Mergify/Trunk/Aviator are paid |
| `ci_kit/queue_wait.py` | `ci-queue-wait`: p90 time one workflow's runs waited for a runner over the last N hours, exit 1 over `--max-p90`, 2 when fewer than `--min-runs` were measured. The number the treadmill above hid |
| `ci_kit/pin_drift.py` | `ci-pin-drift`: every `<kit>@vX.Y.Z` / `?tag=vX.Y.Z` / `<kit>/releases/download/vX.Y.Z/` / PEP 508 `<kit>[extras]==X.Y.Z` (an index pin, PEP 503 name, read as tag `vX.Y.Z`; v1.12.0) pin in the given files is the newest release tag on `--remote`, else exit 1. A kit fix that never reaches a consumer must not fail silently. `--grace-hours` (default 24, v1.21.0): a lag whose EVERY missing release is dated and younger than that (by DATE, not version) is printed PENDING and exits 0 -- Renovate's bump, not yet landed -- so a daily cron no longer goes red the day after every kit release; an unreadable or future release date, a private kit (no credential in the date fetch) and a phantom pin get no grace; `0` = strict |
| `ci_kit/manual_lint.py` | `ci-manual-lint`: every path an agent manual (`CLAUDE.md`) names **resolves today** in `git ls-files`. The question `ci-doc-lint` deliberately cannot answer -- a path that was never real -- asked only of the file every session loads. Quiet by SHAPE (routes, flags, prose with slashes, image tags, formulas), not by allowlist; a sibling-repo pointer is checked when that checkout is present and silent when not. Optional: stale migration ranges, line budget, empty placeholder packages, reference-doc dirs + freshness stamp, a total index |
| `ci_kit/guard_selfproof.py` | `ci-guard-selfproof`: every guard script proves it can FAIL -- `selftest() -> list[str]` (or `--selftest` for a JS guard) rejecting a known-bad input and accepting a known-good one. A guard without one is a shrink-only baseline entry; a NEW one fails. A selftest returning an int, raising, or failing to import is a failure by name |
| `ci_kit/env_reads.py` | `ci-env-reads`: a shell script pulls a key out of a `.env` FILE with a hand pipeline (`sed -n 's/^K=//p' FILE`, `grep '^K=' FILE \| cut -d= -f2-`, `awk -F= '/^K=/' FILE`) instead of the repo's reader. Hand copies disagreed in RESULTS (first vs last duplicate, quotes kept, a value cut at `#`, `$$` left doubled where compose yields `$`). Reading from a pipe (`docker inspect ... \| sed`) is quiet on purpose. Per-file count ratchet (`--baseline`, default `ci/env_reads_baseline.txt`), `--exempt` the reader itself, waiver `# env-read-ok: <why>` |
| `ci_kit/dup_code.py` | `ci-dup-code`: **the same code written twice** fails the PR. [jscpd](https://github.com/kucherenko/jscpd) 5.4.2 (pinned release binary, sha256-verified, cached under `CI_KIT_CACHE_DIR`; no node) over code formats (python, bash, powershell, csharp, js/ts, sql, yaml, ...; json/md/toml are noise, not code) at **70 tokens / 7 lines** -- measured 2026-10-31 on three live repos: below that the hits are 5-line test prologues, above it they are copied helpers and lab-script twins, the complaint itself. jscpd's own fingerprint baseline (`ci/jscpd-baseline.json`, `--write-baseline`): a line shift or a rename is not new, an edit INSIDE a frozen clone splits it into new clones (consolidate, not re-freeze), and `--write-baseline` **refuses to grow**. A clone counts only between two `git ls-files` files (CI == laptop). The verdict is read from the JSON report, never from jscpd's exit code (0 on a missing path and an empty scan): no report or zero files analyzed is **exit 2**. Chosen over pylint `duplicate-code`: one language, no stable clone identity to baseline. Kit ignores: worktrees, venvs, `**/migrations/**`, fixtures/testdata/golden/snapshots, lockfiles, minified; add `--ignore GLOB` / `--format` |
| `ci_kit/api_diff.py` | `ci-api-diff`: a committed OpenAPI contract may **grow, never break**. [oasdiff](https://github.com/oasdiff/oasdiff) 1.35.0 (pinned release binary like jscpd) classifies the spec at `git merge-base <--base-ref> HEAD` against the working-tree file (`--path`, default `docs/api/openapi.json`) as ERR / WARN / INFO; any ERR (a route removed, a response field dropped, a request parameter made required) is **exit 1**. The verdict is parsed from `oasdiff changelog --format json`, never its exit code; no merge base, an unparseable spec or no binary is **exit 2**; a spec NEW at the merge base cannot break (0, said so). A deliberate break is a line in oasdiff's own `--err-ignore FILE`, committed in the breaking PR -- there is no skip flag. `--base A --revision B` compares two files. Library: `changes()` (classified), `structural()` (what moved by operation/component, incl. description-only edits the changelog does not classify) |
| `ci_kit/ci_gate.py` | `ci-gate-lint` (v1.13.0, the `workflow` extra: PyYAML): **a job that can SKIP must funnel into one always-running aggregate.** GitHub counts a skipped job as a passing required check and lets auto-merge through: trade-infra#806 (2026-11-06) merged with four required jobs skipped after its paths-filter `detect` job died on runner DNS. The fix is one `if: always()` job running [re-actors/alls-green](https://github.com/re-actors/alls-green) over `${{ toJSON(needs) }}`, needing every job, as the ONLY required check; this gate keeps that shape from rotting: a job with `needs:` and no aggregate, more than one, an aggregate that is not `always()` or judges something other than `toJSON(needs)`, a job outside its `needs` (the aggregate does not judge it: protection must name it, or it joins `needs`), and an aggregate on a self-hosted or expression runner with no `setup-python` before it (alls-green runs `python -m`; a python3-only image exits 127), and an `allowed-skips` entry (JSON list or comma-separated, as alls-green reads it) that is an upstream (`detect`), has no `if:`, or names no job. `--workflow` (repeatable, default `.github/workflows/ci.yml`); a missing/unparseable/jobless workflow is exit 2. Branch protection naming the aggregate lives outside the checkout -- an estate audit reads that |
| `ci_kit/version_tag.py` | `ci-version-tag` (v1.20.0): in a **version-first** repo (the release PR moves `version` in pyproject.toml) every version bump on the default branch's first-parent history gets its `vX.Y.Z` tag at the bump commit, and every tag its release. An untagged bump is tagged through the API, verified, and `--release-workflow` is dispatched on it (a GITHUB_TOKEN tag fires no push workflow; `workflow_dispatch` is the event that token may start, so the release workflow must read `github.ref_type == 'tag'`, never `event_name == 'push'`). A tag missing its GitHub release (`--github-release`) or its PyPI files (`--pypi NAME`): no run = dispatch, a run in flight = pending, a run that FINISHED and left it missing = exit 1 (a failed publish is rerun by a person, never re-dispatched blind). `.devN`/`+local` and versions below `--since` are skipped; a tag on another commit is printed, never moved. Writes only on the default branch (elsewhere = dry run). Built after llm-ports 1.19.0 merged and was never tagged, with nothing red |
| `ci_kit/ci_green.py` | `ci-green` (v1.22.0): fast-forward a `ci-green` branch to the commit CI just PASSED on the default branch, so a box that BUILDS from its checkout follows `origin/ci-green` instead of `origin/main` and never deploys a merge whose push CI failed (edge-host deploy-reconcile, 2026-11-09). Absent = create, ancestor = advance NON-force, newer already there = no-op (`older`), diverged or not in the default branch's history = exit 1 and never forced; refuses the default branch, `main`, `master`; `--dry-run`; exit 2 when it could not look |
| `ci_kit/release_bin.py` | library: a third-party CLI pinned by **version + sha256 per platform**, fetched once into `$CI_KIT_CACHE_DIR` (`<name>-<version>-<asset>/`), `$CI_KIT_<NAME>` overrides; a 5xx or network error is retried 3x with backoff (a 4xx is not), the binary is written atomically (a shared cache is safe under concurrent jobs), and any failure to obtain it is still exit 2. jscpd and oasdiff state only a `Pin`. Each version line carries `# renovate: datasource=github-releases depName=...` (`renovate/default.json5` reads it): Renovate moves the VERSION, so its PR fails the selftest on the sha256 until the hashes move in the same PR -- a new binary never runs on an unreviewed digest |
| `ci_kit/git_ssh.sh` | a PRIVATE GitHub git dependency in a runner install (the review gate, v1.24.0), three phases = three steps: `load` (the ONLY step holding `CI_KIT_GIT_SSH_KEY`; no PR code runs there) starts an `ssh-agent`, adds the key from stdin, writes GitHub's published host keys to a file of its own; `. git_ssh.sh route` in the install step (no key in its env: a build backend could read it from the parent shell's `/proc/<pid>/environ` even after `unset`) rewrites only `CI_KIT_GIT_SSH_REPOS` (`owner/name`) https -> ssh through env-scoped `GIT_CONFIG_COUNT` with `StrictHostKeyChecking=yes`; `kill` (`always()`; a zombie counts as dead) stops the agent before the step that runs the PR's tests. The agent starts without the key in its environment, but the key is in its MEMORY while Install runs: code with root (a hosted runner's passwordless sudo) can dump it, so the key must be read-only and scoped to one repo. webfactory/ssh-agent was not used: its agent lives to the job's end. Repos named with an EMPTY key (a fork/Dependabot PR, an unmapped secret), a key with no repo, a malformed repo or a key `ssh-add` refuses = `::error::` and exit 1 before the install (`tests/test_git_ssh_must_fire.py`) |
| `ci_kit/ratchet.py` | library: `RatchetSet` / `RatchetCount`, the shrink-only baseline every ratchet lint shares |
| `ci_kit/discover.py` | library: `tracked()` = `git ls-files`, never a filesystem walk (a walk sees your untracked files, CI does not; a checkout under `.claude/worktrees/` once blinded three gates); `require_scanned()` = zero files is exit 2; `count_by_dir()` + `require_scanned_each()` (v1.9.0) = a floor PER ruled dir, so one tree going dark cannot hide under another's hundreds; two NAMED file lists (v1.14.0): `tracked_each()` = absolute paths, every dir contributes or exit 2 naming `gate[dir]` (the sql gates' `iter_files`), and `tracked_or_empty()` = absolute paths, [] when no scan dir exists so the gate exits 2 in its own words (a git failure is still 2) |
| `ci_kit/site_lint.py` | library: the ratchet gate's scan / fixture selftest / CLI -- a per-file count (`RatchetCount`) or a set of keys (`RatchetSet`), with the gate's own words through optional hooks (`header`, `describe`, `ok`, `written`, and `hard` for findings no baseline grants) -- so a second ratchet lint is a predicate, not a copy. `check` is the same CLI for a **pass/fail** gate with no baseline (`--selftest`, the gate's own mode flags, findings -> 1, could not look -> 2, an optional scan floor; voice via `header`/`describe`/`advice`/`ok`, or `report` for output that fits no template). v1.14.0: `waived_lines(text, waiver)` = an inline waiver must carry a reason (>= 8 chars, else it is BARE and reported, not honoured); `repo_on_path(root, marker)` = put the checkout on `sys.path` only when it holds the marker file, so a must-fire fixture repo keeps the installed package |
| `ci_kit/astread.py` | library: read + `ast.parse` a source file, or **exit 2** naming the file and the gate (`blind()`); BOM-tolerant (`utf-8-sig`), never `errors="replace"`. A parse failure answered with `continue` is byte-identical to "read and clean" |
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
| `.github/workflows/gitleaks.yml` | reusable: secret scan, `mode: tree / history / range` (`action` = `range`, the old name); every mode is `ci-gitleaks` (v1.17.0: gitleaks pinned by version + sha256 through release_bin, no docker, no gitleaks-action) and runs on the caller's home pool when its `CI_RUNNER` variable is set; `history` is HEAD's ancestry, never `--all` (a fixture on another open branch must not fail THIS PR), and zero commits scanned is exit 2; every `.gitleaksignore` gitleaks loads (`./`, `<source>/`, and `ignore-path` -- a file or a folder) is judged, and a dead entry (an absolute `/repo/` path, a malformed or trailing-comment fingerprint) is exit 2, so `ignore-path: ''` still checks the repo's own baseline |
| `.github/workflows/pr-autoupdate.yml` | reusable: the armed-PR updater; output `relock`. Runs on the caller's SECURE pool (`[self-hosted, home-secure]`, home-host VM 111, no docker socket) when its `CI_SECURE_RUNNER` variable is set (an on/off switch; labels are fixed here), reading the token from its `ci-secure` environment; hosted otherwise |
| `.github/workflows/merge-train.yml` | reusable: `ci-merge-train` on the caller's SECURE pool (a PAT: GITHUB_TOKEN pushes fire no CI); caller template in the header (cron + dispatch + `pull_request_target` labeled/closed + `workflow_run` of CI/review). No checkout. pr-autoupdate skips riders and holds non-conflict updates while a train is open |
| `.github/workflows/review-gate.yml` | reusable: the pre-merge model review gate. ALWAYS runs, a step decides (a skipped required check reads as passed); fails CLOSED; the policy, prompt, tools and verdict run from ci-kit at `kit-ref` (default `v1`), fetched into `$RUNNER_TEMP` with its own locked venv, so a caller PR can edit only its stanza, not the gate. Inputs: `guard-paths` (required), `label`, `charter`, `install`, `python-version`, `model`, `guard-model`, `not-prose`, `auth-mode`, `claude-code-version`, `kit-ref`, `git-ssh-repos`. A caller whose `install` fetches a PRIVATE GitHub repo by https URL passes `git-ssh-repos: owner/name` and the optional secret `git-ssh-key` (a read-only deploy key): `ci_kit/git_ssh.sh` loads it in a step of its own, Install uses the agent, which is stopped before Review (v1.24.0); a caller passing neither runs exactly as before. `review` (the credential) runs on the caller's SEALED pool (`[self-hosted, home-sealed]`: one job per boot, VM rolled back to a clean snapshot) when its `CI_SEALED_RUNNER` variable is set, hosted otherwise (v1.22.1; node via setup-node since v1.22.2, the sealed image has none, since v1.22.3 before the caller's `install` too, and a failed setup still reaches Review; never the secure or socket pool); `record verdict` runs on the caller's secure pool when `CI_SECURE_RUNNER` is set (v1.17.1; never the socket pool: later runs trust its artifact). A same-repo PR opened by Renovate (`renovate[bot]`, every non-merge commit its own) whose diff is version bumps only (`ci_kit/review/bump.py`: every changed manifest line differs from its pair only in a pinned version, ref, tag or digest, each version strictly up with its suffix kept, no `!=`/`<`/`<=` moved) skips the model call even on a guard path; any other author is reviewed, and the label still reviews (v1.23.0). A commit by another author whose own diff touches lockfiles only (trade-data `relock.yml`) is accepted inside such a PR unless a lockfile it touches is under a `not-prose` prefix or on the caller's `guard-paths`; a refusal logs `-- failed lock: <which>`, naming commits by SHA only (v1.27.1). Its header is the caller template: one `review` job plus a `review gate` alls-green aggregate, which is the required check |
| `.github/workflows/ci-green.yml` | reusable: `ci-green` on `workflow_run` of the caller's CI (template in the file's header: `permissions: {}`, job `contents: write`, no-cancel concurrency). Advances only on a `success` run of a `push`/`workflow_dispatch` on the DEFAULT branch from the same repo; checks out that run's `head_sha` at full depth. Pushes with the caller's PAT (secret `token`: contents + workflows RW -- GITHUB_TOKEN is refused any push whose range touches `.github/workflows/**`), so it runs hosted or on the SECURE pool (`CI_SECURE_RUNNER`, `ci-secure` environment), never the socket pool |
| `.github/workflows/major-tag.yml` | moves each floating `vX` to its highest `vX.Y.Z` on every release tag; also reusable (`workflow_call`) -- the caller declares the triggers and `contents: write` |
| `.pre-commit-hooks.yaml` | hooks `doc-refs`, `secret-argv`, `dup-code` |
| `.github/workflows/version-tag.yml` | reusable: `ci-version-tag` for a version-first repo (ci-kit's own caller: `tag-version.yml`, dispatching `major-tag.yml` on each new tag); the caller declares push-to-main + a daily cron + workflow_dispatch and `contents: write, actions: write` (template in the file's header); inputs `release-workflow`, `github-release`, `pypi`, `since`, `dry-run`, `runner` |
| `renovate/default.json5` | the estate's ONE Renovate policy (moved here from the private edge-ci 2026-11-03: Renovate gives a PUBLIC repo a token scoped to itself, so it cannot read a preset in a private repo, and llm-ports' Renovate stopped on it). A public repo's `renovate.json` extends `github>mjdevaccount/ci-kit//renovate/default.json5`; a private repo may keep extending edge-ci's file, which is one line pointing here -- never a second copy. Window all Saturday + Sunday until 18:00 ET, 3-day release age (first-party `mjdevaccount` kits exempt from both: any day, no wait), non-major + CI-action majors + compose minors auto-merge once EVERY check is green (`platformAutomerge` off), other majors for review, stateful images held, security at any time; `customManagers` bump first-party kit pins outside `uses:` (the shapes `ci-pin-drift` reads) and `# renovate: datasource=... depName=...` annotated pins (`tests/test_renovate_annotation_must_fire.py`). Read from `main`, so a merge here changes every repo; CI runs `renovate-config-validator --strict` on it |

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
  record verdict; sealed (`CI_SEALED_RUNNER`) for review. Never the shared `CI_RUNNER` pool.
  The four lint workflows (`doc-refs`, `dup-code`, `secret-argv`, `gitleaks`) read
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
