# ci-kit documentation

Start with [architecture.md](architecture.md): the exit contract, shrink-only baselines, why every
gate must prove it can fire, and the dependency policy. Everything else is a reference page.

## Gates

| page | gates |
|---|---|
| [gates/docs.md](gates/docs.md) | `ci-doc-lint`, `ci-manual-lint`, `ci-cog` |
| [gates/secrets.md](gates/secrets.md) | `ci-secret-argv-lint`, `ci-gitleaks`, `ci-hidden-unicode`, `ci-declared-literal` |
| [gates/ratchets.md](gates/ratchets.md) | `ci-line-cap`, `ci-function-clones`, `ci-dup-code`, `ci-test-standins`, `ci-untested-modules`, `ci-guards-have-tests`, `ci-http-clients`, `ci-sql-placement`, `ci-env-reads`, `ci-guard-selfproof` |
| [gates/structure.md](gates/structure.md) | `ci-blind-parse`, `ci-quarantine`, `ci-gate-lint`, `ci-api-diff`, `ci-image-pr-filter`, `ci-migration-open-prs` |
| [gates/sql.md](gates/sql.md) | `ci-sql-casts`, `ci-pit-reads`, `ci-decimal-reads`, `ci-phantom-columns`, `ci-column-comments` (the `sql` extra) |
| [gates/supply-chain.md](gates/supply-chain.md) | `ci-pin-drift`, `ci-green`, `ci-tf-seed`, `ci-queue-wait`, `release_bin`, `git_ssh.sh`, the shared Renovate preset |

## Kits

| page | what |
|---|---|
| [pr-ops/README.md](pr-ops/README.md) | keeping armed PRs current and landing them under strict protection |
| [pr-ops/autoupdate.md](pr-ops/autoupdate.md) | `ci-pr-autoupdate`: the serial queue, smart auto-merge, base-driven retry, the scheduler, the queue hold, doomed-run cancel, `update-held` |
| [pr-ops/heal-ladder.md](pr-ops/heal-ladder.md) | the heal ladder (`pr-heal.yml`) and the dependency-bot relock (`relock.yml`) |
| [pr-ops/merge-train.md](pr-ops/merge-train.md) | `ci-merge-train` |
| [pr-ops/push-token.md](pr-ops/push-token.md) | the push token (GitHub App or PAT), its record line, generic names, runner pools |
| [review-gate.md](review-gate.md) | the pre-merge model review gate (the `review` extra) |
| [runners.md](runners.md) | `ci-runners`: self-hosted runner capacity (the `runners` extra) |
| [github-apps.md](github-apps.md) | `ci-app-audit`: GitHub Apps declared and held to live (the `apps` extra); reading the pr-token record (`ci-pr-token-check`) |

## Using and changing the kit

| page | what |
|---|---|
| [reusable-workflows.md](reusable-workflows.md) | every `workflow_call` workflow: inputs, secrets, a caller stanza, `runs-on` |
| [library.md](library.md) | the shared modules a gate is built on |
| [versioning.md](versioning.md) | immutable `vX.Y.Z` tags, the moving `v1`, automatic tagging |
| [contributing.md](contributing.md) | setup, what a new gate needs, what CI runs |
