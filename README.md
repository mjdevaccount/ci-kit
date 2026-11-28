# ci-kit

Reusable CI gates, each proven to **fire** before it is trusted. A gate that only ever passes is
indistinguishable from one that cannot fail, so every gate here ships with a selftest and a
must-fire suite that drives it against a known-bad input, a known-good one and one it cannot read,
on real git repositories. ci-kit is a Python package (`ci_kit`, no runtime dependencies, Python
3.11+), a composite action that runs it, reusable GitHub workflows (doc rot, secrets, code-shape
ratchets, SQL, a pre-merge model review gate, a serial PR queue with a merge train and a heal
ladder), and a pre-commit hooks manifest. MIT licensed.

**Every tool exits 0 clean · 1 findings · 2 did-not-finish-looking.** 2 is never folded into 0 or
1: a crashed detector that returns 0 reads as healthy to every caller, and a gate that scanned
nothing has verified nothing ([docs/architecture.md](docs/architecture.md)).

## Quickstart

Install, run one gate:

```bash
uvx --from git+https://github.com/mjdevaccount/ci-kit@v1.51.0 ci-doc-lint
```

Call a reusable workflow (doc rot needs full history; the workflow checks out with `fetch-depth: 0`):

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

Pin workflows and the action to `@v1` (the moving major) and the library to an immutable
`vX.Y.Z` tag ([docs/versioning.md](docs/versioning.md)). The action, uvx, a lockfile and
pre-commit are all covered in [docs/architecture.md](docs/architecture.md#four-ways-to-run-a-gate).

## Console scripts

| script | what it checks or does | docs |
|---|---|---|
| `ci-doc-lint` | a path a doc names existed and is gone (count fell from positive to zero) | [gates/docs](docs/gates/docs.md) |
| `ci-manual-lint` | every path an agent manual (`CLAUDE.md`) names resolves today | [gates/docs](docs/gates/docs.md) |
| `ci-cog` | every cog-generated fact matches what its generator renders now (`cog` extra) | [gates/docs](docs/gates/docs.md) |
| `ci-secret-argv-lint` | no secret expanded on the host into a `docker exec` / `curl` / `ssh` argv | [gates/secrets](docs/gates/secrets.md) |
| `ci-gitleaks` | secret scan (tree, history or range), gitleaks pinned by sha256 | [gates/secrets](docs/gates/secrets.md) |
| `ci-hidden-unicode` | no invisible, bidi or control character in a tracked text file | [gates/secrets](docs/gates/secrets.md) |
| `ci-declared-literal` | a literal that must live in one declared file appears nowhere else | [gates/secrets](docs/gates/secrets.md) |
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
| `ci-blind-parse` | no parse-failure handler answers with a clean file's value | [gates/structure](docs/gates/structure.md) |
| `ci-quarantine` | every quarantined test carries a reason, an issue and an unexpired date | [gates/structure](docs/gates/structure.md) |
| `ci-gate-lint` | a job that can skip funnels into one always-run aggregate (`workflow` extra) | [gates/structure](docs/gates/structure.md) |
| `ci-api-diff` | a committed OpenAPI contract may grow, never break (oasdiff) | [gates/structure](docs/gates/structure.md) |
| `ci-image-pr-filter` | every Dockerfile COPY is listed in the image PR build's paths-filter (`workflow` extra) | [gates/structure](docs/gates/structure.md) |
| `ci-migration-open-prs` | a migration number is not already taken on the base or by an older open PR | [gates/structure](docs/gates/structure.md) |
| `ci-sql-casts` | asyncpg bind/cast footguns in embedded SQL (`sql` extra) | [gates/sql](docs/gates/sql.md) |
| `ci-pit-reads` | SQL wall-clock reads (`now()`, `current_date`) (`sql` extra) | [gates/sql](docs/gates/sql.md) |
| `ci-decimal-reads` | a NUMERIC column projected without a cast (`sql` extra) | [gates/sql](docs/gates/sql.md) |
| `ci-phantom-columns` | a statement naming a column its table lacks (`sql` extra) | [gates/sql](docs/gates/sql.md) |
| `ci-column-comments` | a column comment that declares a domain has a CHECK enforcing it (`sql` extra) | [gates/sql](docs/gates/sql.md) |
| `ci-pin-drift` | every exact pin of a kit is its newest release tag | [gates/supply-chain](docs/gates/supply-chain.md) |
| `ci-green` | fast-forward a `ci-green` branch to the commit CI just passed | [gates/supply-chain](docs/gates/supply-chain.md) |
| `ci-tf-seed` | fill terraform's provider cache from the lock file, off GitHub's download path | [gates/supply-chain](docs/gates/supply-chain.md) |
| `ci-queue-wait` | p90 time a workflow's runs waited for a runner, over a limit | [gates/supply-chain](docs/gates/supply-chain.md) |
| `ci-version-tag` | every version bump gets its tag, every tag its release | [versioning](docs/versioning.md) |
| `ci-pr-autoupdate` | merge main into armed PRs: serial queue, scheduler, queue hold, retries | [pr-ops/autoupdate](docs/pr-ops/autoupdate.md) |
| `ci-merge-train` | test N armed PRs once on one tree, land them as N commits | [pr-ops/merge-train](docs/pr-ops/merge-train.md) |
| `ci-runners` | self-hosted runner capacity: schema, budgets, renders, admission (`runners` extra) | [runners](docs/runners.md) |
| `ci-review-tier` | review gate: who is reviewed and why (`review` extra) | [review-gate](docs/review-gate.md) |
| `ci-review-agent` | review gate: the api transport (`review` extra) | [review-gate](docs/review-gate.md) |
| `ci-review-claude` | review gate: the subscription transport (`review` extra) | [review-gate](docs/review-gate.md) |
| `ci-review-verdict` | review gate: the verdict policy (`review` extra) | [review-gate](docs/review-gate.md) |
| `ci-review-carry` | review gate: a verdict carried over an unchanged own diff (`review` extra) | [review-gate](docs/review-gate.md) |
| `ci-review-train` | review gate: a merge train verified instead of re-reviewed (`review` extra) | [review-gate](docs/review-gate.md) |

Reusable workflows, with every input and a caller stanza:
[docs/reusable-workflows.md](docs/reusable-workflows.md). The shared Renovate preset
(`renovate/default.json5`) and the library modules gates are built on
([docs/library.md](docs/library.md)) are documented too.

## Documentation

[docs/README.md](docs/README.md) is the index. Start with [architecture](docs/architecture.md),
then the page for what you need: [gates](docs/README.md#gates), [PR operations](docs/pr-ops/README.md),
the [review gate](docs/review-gate.md), [runner capacity](docs/runners.md),
[reusable workflows](docs/reusable-workflows.md), [versioning](docs/versioning.md),
[contributing](docs/contributing.md).

## License

MIT ([LICENSE](LICENSE)).
