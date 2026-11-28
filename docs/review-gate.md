# The pre-merge model review gate (`review` extra)

`ci_kit/review/` (v1.15.0; **the `review` extra**, `ci-kit[review]`: pydantic-ai-slim + mcp), run by
the reusable `.github/workflows/review-gate.yml` ([inputs](reusable-workflows.md#review-gateyml)).
A model reviewer whose verdict is a CI check auto-merge waits for.

**Why.** When PRs open ready with auto-merge armed at open, CI is the only merge gate. "Review a
guard change before merging" that lives in a person's or an agent's memory loses every race: in the
repo this gate came from, a PR auto-merged 30 seconds after opening and the reviewer's DO NOT SHIP
arrived after it. 41 of 134 closed reviewer dispatches there had found a blocking defect. A hosted
code-review product that never blocks a merge does not close that gap; a required check does.

## The modules

| module | console script | role |
|---|---|---|
| `tier.py` | `ci-review-tier` | who is reviewed and why |
| `tools.py` | | the four read-only tools: `read_file`, `grep`, `glob_files`, `run` (an allowlist of git/ruff/pytest; no secret in any subprocess env) |
| `agent.py` | `ci-review-agent` | the api transport (pydantic-ai on the plain Messages API) |
| `claude.py` + `mcp_server.py` | `ci-review-claude` | the subscription transport: the Claude Code CLI with no built-in tool and only those four, served over MCP |
| `verdict.py` | `ci-review-verdict` | the policy |
| `carry.py` | `ci-review-carry` | the verdict carried over an unchanged own diff |
| `train.py` | `ci-review-train` | a merge train verified instead of re-reviewed |
| `bump.py` | | the dependency-bot version-bump skip |

`tier`, `verdict` and `carry` are stdlib-only. The `ci-review-*` scripts are the workflow's steps;
they are not runnable through `action.yml` (an unknown tool there is exit 2).

## Policy

- **Tier.** Every code PR is reviewed. A guard path (`guard-paths`), 3+ non-merge commits, or the
  label (`label`, default `review:required`, which must be on the PR AT OPEN: there is no
  `labeled` trigger) are REASONS the reviewer reads, and a guard path switches to `guard-model`. A
  docs/lockfile-only diff (read with `--no-renames`; the `not-prose` prefixes are never prose)
  skips the model call.
- **Verdict.** SHIP and SHIP WITH NOTED RISK pass; DO NOT SHIP, or a CONFIRMED blocker, blocks;
  no parseable verdict is BLIND = red.
- **Fails closed.** In the tier with no credential (UNARMED), a fork head, a reviewer that crashed,
  timed out or wrote no parseable verdict is BLIND = RED, with the cause in the PR comment.
- **Always runs, never skips.** A required check that is path-filtered at the trigger never
  reports on an out-of-tier PR, and a job skipped by a job-level `if:` counts as PASSED. So the job
  always starts and a STEP decides.
- **Dependency-bot bumps** (v1.23.0, `ci_kit/review/bump.py`): a same-repo PR opened by Renovate
  (`renovate[bot]`, every non-merge commit its own) whose diff is version bumps only (every changed
  manifest line differs from its pair only in a pinned version, ref, tag or digest, each version
  strictly up with its suffix kept, no `!=`/`<`/`<=` moved) skips the model call even on a guard
  path; any other author is reviewed, and the label still reviews. A commit by another author
  whose own diff touches lockfiles only (a relock, [pr-ops/heal-ladder.md](pr-ops/heal-ladder.md))
  is accepted inside such a PR unless a lockfile it touches is under a `not-prose` prefix or on the
  caller's `guard-paths`; a refusal logs `-- failed lock: <which>`, naming commits by SHA only
  (v1.27.1).
- **Carried verdicts.** The armed-PR updater merges main into every armed PR, and each move
  re-runs this gate. When the PR's OWN diff (merge-base..head) hashes the same as one a previous
  run PASSED, it passes with "verdict carried from <sha>" and no model run. The record is an
  ARTIFACT written by the `record verdict` step of the `verdict` job, which never checks the PR
  out, or, for a carried pass (v1.35.0), by `decide`, which checks it out but executes none of it
  (a comment is editable by any token holder).
- **A merge train is not re-reviewed, and not waved through** (v1.25.0). A PR whose head branch is
  `train/*` skips the tier and the model: `ci_kit.review.train` proves every commit on it is ONE
  constituent's squash (its tree recomputed byte-for-byte from the PR head named in the commit's
  trailers) AND that the constituent's own review-gate run PASSED at exactly that head, and that
  the composition record names a run of `train-workflow` that opened it. Anything else (a stray
  byte, an unbound commit, an unreviewed or moved head, a merge commit, a fork) is block; a
  verifier that could not finish is BLIND. The `train/` prefix is the key in both directions: a
  hand-made `train/` branch is verified, never reviewed, and so cannot pass unless it IS a train.

## The credential toggle

Two transports, one toggle: the caller repo's VARIABLE `REVIEW_GATE_AUTH` (or input `auth-mode`) =
`subscription` (secret `claude-code-oauth-token`, the Claude Code CLI) | `api` (secret
`anthropic-api-key`, pydantic-ai) | `auto` / unset (subscription first). Only the chosen credential
reaches the Review step: with both set, `claude -p` would prefer the API key and bill the account
the subscription mode exists to spare. A fork head or a mode with no secret is BLIND/UNARMED and
red.

## Why a reusable workflow closes most of the self-grading hole

`pull_request` runs the PR HEAD's copy of a workflow, so a PR editing an in-repo gate was graded
by its own edit (it could drop the tier, the prompt or the Gate step). Here the policy, the prompt,
the tools and the verdict all run from ci-kit at `kit-ref` (default `v1`), fetched into
`$RUNNER_TEMP` with its own locked venv (the `review` extra synced there; the reviewer never runs
in the caller's venv). A caller PR can only edit its STANZA (the ref, the inputs, the aggregate);
that residual wants a human or agent reviewer on any diff to the caller file. The PR comment names
the ref, version and sha that graded the PR. `kit-ref` exists for a bootstrap or a test of an
unreleased change; a caller pinned to `@v1` leaves it alone.

Still open: the review job runs the PR's own pytest as the runner user, which can read the
credential and rewrite the installed gate under `$RUNNER_TEMP`. Fork heads get no secrets.

`PYTHONSAFEPATH=1` (v1.15.1): the review job sets it (cleared for the caller's install and the
tool runs), so a PR-added `ci_kit/` cannot shadow the gate that grades it
(`tests/test_review_safepath_must_fire.py`).

## Three jobs, one verdict

Since v1.35.0 (five jobs v1.29.0-v1.34.x):

| job (check name, caller job `reviewer`) | pool | runs |
|---|---|---|
| `decide` (`reviewer / review`) | SECURE | everything that executes no PR code: train, tier, CARRY, admission, siblings; finishes a carry, a BLIND or a train itself |
| `model review` (`reviewer / model review`) | SEALED | a full review only |
| `verdict` (`reviewer / verdict`) | SECURE | the in-workflow aggregate (a skipped `model review` passes only when `decide` said none was needed), then the `record verdict` and `review deferred` STEPS |

`record verdict` and `review deferred` were jobs of their own until v1.35.0 and cost two runner
boots per run for seconds of shell. A base-only update of a passed PR never waits for the sealed
runner. A CARRIED pass is finished in `decide` itself (`python -m ci_kit.review.carry finish`: the
sticky comment and the record) and `verdict` is then SKIPPED, only when `decide` succeeded and says
`finished`; any failure there runs `verdict` as before. A parked run is the `verdict` job failing
on its `review deferred` step, which `redclass` / `pr_sched` read as PARKED.

The required check is the caller's own `review gate`: one `always()` alls-green aggregate over the
`review` job (`ci-gate-lint`'s shape, [gates/structure.md](gates/structure.md)). It keeps a
pre-existing check name, and `record verdict` (continue-on-error) can never turn it red.

**Pools.** `model review` (the credential) runs on the caller's SEALED pool
(`[self-hosted, home-sealed]`: one job per boot, VM rolled back to a clean snapshot) when
`sealed-runner` is set or its `CI_SEALED_RUNNER` variable is on, hosted otherwise (v1.22.1; node
via setup-node since v1.22.2, the sealed image has none; since v1.22.3 before the caller's
`install` too, and a failed setup still reaches Review). Never the secure or socket pool. The
verdict record runs on the caller's secure pool when `CI_SECURE_RUNNER` is set (v1.17.1), never
the socket pool: later runs trust its artifact ([pr-ops/push-token.md](pr-ops/push-token.md#pools)).

**The PR queue** (`queue-priority`, v1.29.0). The sealed pool is ONE runner. A PR numbered after
the queue head (the lowest-numbered armed PR, `ci_kit/pr_queue.head`) does not start a full review
while the head needs that pool; its run ends red at `review deferred`, labelled `queue:parked`, and
the caller's pr-autoupdate re-runs it ([pr-ops/autoupdate.md](pr-ops/autoupdate.md#the-pr-scheduler)).
ORDER AND TIMING ONLY: every PR is still reviewed in full, or carried, at its merging head.

## A private git dependency

A caller whose `install` fetches a PRIVATE GitHub repo by https URL passes
`git-ssh-repos: owner/name` and the optional secret `git-ssh-key` (a read-only deploy key):
`ci_kit/git_ssh.sh` loads it in a step of its own, where no PR code runs; Install gets only the
agent's socket, an env-scoped rewrite of just those repos and GitHub's pinned host keys; the agent
is killed before Review (v1.24.0). Two or more private repos pass one deploy key per repo in
`git-ssh-key`, in `git-ssh-repos` order (v1.28.0; a deploy key reads ONE repo), each with its own
agent and host alias. Named repos + an empty key = loud red. A caller passing neither runs exactly
as before ([gates/supply-chain.md](gates/supply-chain.md#git_sshsh-a-private-git-dependency-in-ci)).

## Caller

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]   # NO `labeled`
permissions: { contents: read, pull-requests: write, actions: read }
concurrency: { group: review-gate-${{ github.event.pull_request.number }}, cancel-in-progress: true }
jobs:
  review:
    name: reviewer
    uses: mjdevaccount/ci-kit/.github/workflows/review-gate.yml@v1
    with:
      guard-paths: |            # YOUR guard paths, dorny/paths-filter globs, one `- '...'` per line
        - 'migrations/**'
        - '.github/workflows/**'
      charter: Your charter is REVIEW.md. Read it first and follow it.
    secrets:                    # the names are yours; map onto these
      claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
      anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
      ntfy-url: ${{ secrets.NTFY_URL }}                 # optional
  gate:                         # THE required check: one always() aggregate
    name: review gate
    needs: [review]
    if: always()
    runs-on: ubuntu-latest
    steps:
      - uses: re-actors/alls-green@b5b5b37504aa4183270bd3d855c52a67f212be35 # v1.3.0
        with: { jobs: '${{ toJSON(needs) }}' }
```

The same template, with the optional `git-ssh-repos` lines, is the header of
`.github/workflows/review-gate.yml`.
