<div align="center">

# ci-kit

**Reusable CI gates and PR automation for GitHub, each proven to fail before it is trusted.**

[![CI](https://github.com/mjdevaccount/ci-kit/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/mjdevaccount/ci-kit/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/mjdevaccount/ci-kit?sort=semver)](https://github.com/mjdevaccount/ci-kit/releases)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[Quickstart](#quickstart) · [What's inside](#whats-inside) · [Design](#design-principles) · [Gate catalog](#gate-catalog) · [Docs](docs/README.md)

</div>

---

A gate that only ever passes looks exactly like a gate that cannot fail. ci-kit is a set of CI
checks and PR-automation tools built around that one problem: **every gate ships with a must-fire
suite** that runs it on real git repositories against a known-bad input, a known-good input, and
one it cannot read, and the suite is part of the kit's own CI.

It is one Python package (`ci_kit`, zero runtime dependencies, Python 3.11+), exposed four ways: a
composite GitHub Action, reusable workflows, console scripts you can run with `uvx`, and
pre-commit hooks. Nothing in it knows about your repository; every path, threshold and baseline is
an argument you pass.

## What's inside

| Capability | What it gives you |
|---|---|
| **[Gates](docs/README.md#gates)** | 30+ checks across docs, secrets, code shape, structure, SQL and supply chain: doc references that went dead, secrets on a command line, invisible Unicode, new code clones, modules no test reaches, SQL outside the repository layer, OpenAPI breaking changes, and more. |
| **[Shrink-only ratchets](docs/architecture.md#shrink-only-baselines)** | Adopt a gate on a messy codebase today. The current debt is frozen in a baseline that can only shrink, so nothing new gets in while the old debt is paid down. |
| **[PR operations](docs/pr-ops/README.md)** | A serial PR queue, a merge train that tests N armed PRs once and lands them as N commits with bisect-on-red, and a heal ladder that fixes a red or conflicted PR cheapest-first, from a rerun up to a sandboxed agent. It works with strict branch protection, on a personal account, without a paid merge-queue service. |
| **[Pre-merge review gate](docs/review-gate.md)** | A model code review that runs as a **required check**, so auto-merge waits for the verdict instead of racing it. Two transports (an API key, or a Claude Code subscription over MCP), a tiering policy, and a verdict carried over an unchanged diff. |
| **[GitHub Apps](docs/github-apps.md)** | Declare each GitHub App your automation acts as (permissions, events, installed repositories) in a reviewed manifest, and turn any change made on GitHub's settings pages into a red scheduled run. |
| **[Runner capacity](docs/runners.md)** | Self-hosted runner sizing as a library: schema-checked capacity files, host, guest and runner memory budgets against measured peaks, one JSON render per runner block, and the admission rule a supervisor runs. |
| **[Library](docs/library.md)** | The parsers, discovery, site-lint and ratchet primitives the gates are built on, for writing your own gate with the same exit contract. |

Also included: a shared **[Renovate preset](renovate/default.json5)** (one line in a consumer's
`renovate.json`), and **[16 reusable workflows](docs/reusable-workflows.md)**, each documented
with every input and a caller stanza.

## Quickstart

**Run a gate locally**, no install:

```bash
uvx --from git+https://github.com/mjdevaccount/ci-kit@v1.51.0 ci-doc-lint
```

**Call a reusable workflow** from your CI:

```yaml
jobs:
  doc-refs:
    uses: mjdevaccount/ci-kit/.github/workflows/doc-refs.yml@v1
  gitleaks:
    name: gitleaks (secret scan)
    permissions: { contents: read }
    uses: mjdevaccount/ci-kit/.github/workflows/gitleaks.yml@v1
    with: { mode: tree }
```

**Run any console script as a step** with the composite action:

```yaml
- uses: actions/checkout@v4
- uses: mjdevaccount/ci-kit@v1
  with:
    tool: ci-line-cap
    args: --dir src --cap 400 --baseline ci/line_cap_baseline.txt
```

**Add pre-commit hooks** (`doc-refs`, `secret-argv`, `hidden-unicode`, `dup-code`):

```yaml
repos:
  - repo: https://github.com/mjdevaccount/ci-kit
    rev: v1.51.0
    hooks:
      - id: hidden-unicode
```

Pin workflows and the action to `@v1`, the moving major, and the library to an immutable
`vX.Y.Z` tag ([versioning](docs/versioning.md)). Every way to run a gate is compared in
[architecture](docs/architecture.md#four-ways-to-run-a-gate).

## Design principles

- **Three exits, never two.** Every tool exits `0` clean, `1` findings, or `2` did not finish
  looking. A crashed detector that exits 0 reads as healthy to every caller, and a gate that
  scanned nothing has verified nothing, so `2` is never folded into the others
  ([the exit contract](docs/architecture.md#the-exit-contract-0--1--2)).
- **Proven to fire.** Each gate has a `selftest` and a must-fire suite: bad input → `1`, good
  input → `0`, unreadable input → `2`, on real git repositories rather than mocks
  ([a guard must be able to fire](docs/architecture.md#a-guard-must-be-able-to-fire)).
- **Adoptable on day one.** Ratchet gates freeze today's count per file in a baseline that may
  shrink and never grow.
- **No assumptions about your repo.** Paths, thresholds, seams and baselines are arguments. Domain
  specifics live in the caller's args files, not in the kit
  ([nothing is assumed](docs/architecture.md#nothing-is-assumed-about-your-repo)).
- **Dependencies are opt-in.** The base package has none; heavier gates sit behind extras (`sql`,
  `workflow`, `cog`, `runners`, `review`)
  ([extras](docs/architecture.md#extras-are-the-dependency-policy)).
- **Pinned supply chain.** Release binaries such as gitleaks and jscpd are pinned by sha256, and the action installs
  exactly the source its ref points at.

## Gate catalog

Every console script, grouped by concern. Each links to its page in [docs/](docs/README.md).

<details>
<summary><b>Documentation</b> (3)</summary>

| script | what it checks or does | docs |
|---|---|---|
| `ci-doc-lint` | a path a doc names existed and is gone (count fell from positive to zero) | [gates/docs](docs/gates/docs.md) |
| `ci-manual-lint` | every path an agent manual (`CLAUDE.md`) names resolves today | [gates/docs](docs/gates/docs.md) |
| `ci-cog` | every cog-generated fact matches what its generator renders now (`cog` extra) | [gates/docs](docs/gates/docs.md) |

</details>

<details>
<summary><b>Secrets and hidden content</b> (4)</summary>

| script | what it checks or does | docs |
|---|---|---|
| `ci-secret-argv-lint` | no secret expanded on the host into a `docker exec` / `curl` / `ssh` argv | [gates/secrets](docs/gates/secrets.md) |
| `ci-gitleaks` | secret scan (tree, history or range), gitleaks pinned by sha256 | [gates/secrets](docs/gates/secrets.md) |
| `ci-hidden-unicode` | no invisible, bidi or control character in a tracked text file | [gates/secrets](docs/gates/secrets.md) |
| `ci-declared-literal` | a literal that must live in one declared file appears nowhere else | [gates/secrets](docs/gates/secrets.md) |

</details>

<details>
<summary><b>Code-shape ratchets</b> (10)</summary>

| script | what it checks or does | docs |
|---|---|---|
| `ci-line-cap` | no new module over the line cap; one already over may not grow | [gates/ratchets](docs/gates/ratchets.md) |
| `ci-function-clones` | no new function that is a structural twin of one in another file | [gates/ratchets](docs/gates/ratchets.md) |
| `ci-dup-code` | no new token-level code clone (jscpd) | [gates/ratchets](docs/gates/ratchets.md) |
| `ci-test-standins` | no new signature-blind test stand-in | [gates/ratchets](docs/gates/ratchets.md) |
| `ci-untested-modules` | no new source module that no test references | [gates/ratchets](docs/gates/ratchets.md) |
| `ci-guards-have-tests` | every guard script is named by a test, outside prose | [gates/ratchets](docs/gates/ratchets.md) |
| `ci-http-clients` | no new hand-built HTTP client outside the declared seam | [gates/ratchets](docs/gates/ratchets.md) |
| `ci-sql-placement` | no new SQL outside the repository layer (`sql` extra) | [gates/ratchets](docs/gates/ratchets.md#ci-sql-placement) |
| `ci-env-reads` | no shell script pulls a key out of a `.env` file with a hand pipeline | [gates/ratchets](docs/gates/ratchets.md) |
| `ci-guard-selfproof` | every guard script proves it can fail (`selftest()`) | [gates/ratchets](docs/gates/ratchets.md) |

</details>

<details>
<summary><b>Structure and contracts</b> (6)</summary>

| script | what it checks or does | docs |
|---|---|---|
| `ci-blind-parse` | no parse-failure handler answers with a clean file's value | [gates/structure](docs/gates/structure.md) |
| `ci-quarantine` | every quarantined test carries a reason, an issue and an unexpired date | [gates/structure](docs/gates/structure.md) |
| `ci-gate-lint` | a job that can skip funnels into one always-run aggregate (`workflow` extra) | [gates/structure](docs/gates/structure.md) |
| `ci-api-diff` | a committed OpenAPI contract may grow, never break (oasdiff) | [gates/structure](docs/gates/structure.md) |
| `ci-image-pr-filter` | every Dockerfile COPY is listed in the image PR build's paths-filter (`workflow` extra) | [gates/structure](docs/gates/structure.md) |
| `ci-migration-open-prs` | a migration number is not already taken on the base or by an older open PR | [gates/structure](docs/gates/structure.md) |

</details>

<details>
<summary><b>SQL (`sql` extra)</b> (5)</summary>

| script | what it checks or does | docs |
|---|---|---|
| `ci-sql-casts` | asyncpg bind/cast footguns in embedded SQL (`sql` extra) | [gates/sql](docs/gates/sql.md) |
| `ci-pit-reads` | SQL wall-clock reads (`now()`, `current_date`) (`sql` extra) | [gates/sql](docs/gates/sql.md) |
| `ci-decimal-reads` | a NUMERIC column projected without a cast (`sql` extra) | [gates/sql](docs/gates/sql.md) |
| `ci-phantom-columns` | a statement naming a column its table lacks (`sql` extra) | [gates/sql](docs/gates/sql.md) |
| `ci-column-comments` | a column comment that declares a domain has a CHECK enforcing it (`sql` extra) | [gates/sql](docs/gates/sql.md) |

</details>

<details>
<summary><b>Supply chain and CI health</b> (4)</summary>

| script | what it checks or does | docs |
|---|---|---|
| `ci-pin-drift` | every exact pin of a kit is its newest release tag | [gates/supply-chain](docs/gates/supply-chain.md) |
| `ci-green` | fast-forward a `ci-green` branch to the commit CI just passed | [gates/supply-chain](docs/gates/supply-chain.md) |
| `ci-tf-seed` | fill terraform's provider cache from the lock file, off GitHub's download path | [gates/supply-chain](docs/gates/supply-chain.md) |
| `ci-queue-wait` | p90 time a workflow's runs waited for a runner, over a limit | [gates/supply-chain](docs/gates/supply-chain.md) |

</details>

<details>
<summary><b>Releases</b> (1)</summary>

| script | what it checks or does | docs |
|---|---|---|
| `ci-version-tag` | every version bump gets its tag, every tag its release | [versioning](docs/versioning.md) |

</details>

<details>
<summary><b>PR operations</b> (2)</summary>

| script | what it checks or does | docs |
|---|---|---|
| `ci-pr-autoupdate` | merge main into armed PRs: serial queue, scheduler, queue hold, retries | [pr-ops/autoupdate](docs/pr-ops/autoupdate.md) |
| `ci-merge-train` | test N armed PRs once on one tree, land them as N commits | [pr-ops/merge-train](docs/pr-ops/merge-train.md) |

</details>

<details>
<summary><b>Runner capacity</b> (1)</summary>

| script | what it checks or does | docs |
|---|---|---|
| `ci-runners` | self-hosted runner capacity: schema, budgets, renders, admission (`runners` extra) | [runners](docs/runners.md) |

</details>

<details>
<summary><b>GitHub Apps</b> (2)</summary>

| script | what it checks or does | docs |
|---|---|---|
| `ci-app-audit` | each declared GitHub App's permissions, events and installations match live (`apps` extra) | [github-apps](docs/github-apps.md) |
| `ci-pr-token-check` | the pr-token record lines in a log or annotation dump parse and are clean | [github-apps](docs/github-apps.md#reading-the-pr-token-record) |

</details>

<details>
<summary><b>Review gate (`review` extra)</b> (6)</summary>

| script | what it checks or does | docs |
|---|---|---|
| `ci-review-tier` | review gate: who is reviewed and why (`review` extra) | [review-gate](docs/review-gate.md) |
| `ci-review-agent` | review gate: the api transport (`review` extra) | [review-gate](docs/review-gate.md) |
| `ci-review-claude` | review gate: the subscription transport (`review` extra) | [review-gate](docs/review-gate.md) |
| `ci-review-verdict` | review gate: the verdict policy (`review` extra) | [review-gate](docs/review-gate.md) |
| `ci-review-carry` | review gate: a verdict carried over an unchanged own diff (`review` extra) | [review-gate](docs/review-gate.md) |
| `ci-review-train` | review gate: a merge train verified instead of re-reviewed (`review` extra) | [review-gate](docs/review-gate.md) |

</details>

## Documentation

| Start here | |
|---|---|
| [Architecture](docs/architecture.md) | How the kit is put together: the four ways to run a gate, the exit contract, ratchets, extras |
| [Gates](docs/README.md#gates) | One page per concern, every flag and example |
| [PR operations](docs/pr-ops/README.md) | [Autoupdate](docs/pr-ops/autoupdate.md), [merge train](docs/pr-ops/merge-train.md), [heal ladder](docs/pr-ops/heal-ladder.md), [push token](docs/pr-ops/push-token.md) |
| [Review gate](docs/review-gate.md) | Policy, transports, the caller stanza |
| [Runner capacity](docs/runners.md) | The capacity file, the kit file, the CLI, admission |
| [GitHub Apps](docs/github-apps.md) | An App's permissions and installations declared and audited against live; reading the pr-token record |
| [Reusable workflows](docs/reusable-workflows.md) | Every input of every workflow |
| [Versioning](docs/versioning.md) | Tags, the moving major, release policy |
| [Contributing](docs/contributing.md) | Adding a gate: selftest, must-fire suite, docs |

## License

[MIT](LICENSE)
