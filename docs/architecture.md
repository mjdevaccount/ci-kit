# Architecture

ci-kit is four things that share one contract: a Python package (`ci_kit`, zero runtime
dependencies, Python 3.11+), a composite action (`action.yml`) that runs any of its console
scripts against the caller's checkout, a set of reusable GitHub workflows
([reusable-workflows.md](reusable-workflows.md)), and a pre-commit hooks manifest
(`.pre-commit-hooks.yaml`).

## The exit contract: 0 / 1 / 2

**Every tool exits 0 clean, 1 findings, 2 did-not-finish-looking.**

| exit | meaning |
|---|---|
| 0 | the tool read what it was pointed at and found nothing |
| 1 | the tool read it and found something (a finding, a regression, a stale render) |
| 2 | the tool could not finish looking: an unreadable or unparsable file, zero files scanned, a missing binary, a remote it could not reach, a crash mid-scan |

2 is never folded into 0 or 1. A crashed detector that returns 0 reads as healthy to every
caller, and a gate that scanned nothing has verified nothing. A "could not read this" that exits
1 is also wrong: it puts a blind spot in the findings channel, where it reads as a violation.

Concretely, that contract is enforced by shared library code rather than by each gate's care
([library.md](library.md)):

- `ci_kit/astread.py` reads and parses a source file, or exits 2 naming the file and the gate.
  It is BOM-tolerant (`utf-8-sig`) and never uses `errors="replace"`: a parse failure answered
  with `continue` is byte-identical to "read and clean". The kit measured that defect in the
  service repo the module came from (2026-09-18): seventeen gate scripts called `ast.parse`,
  and thirteen of their parse handlers answered a parse failure with a clean file's value.
  `ci-blind-parse` ([gates/structure.md](gates/structure.md)) keeps the shape from coming back.
- `ci_kit/discover.py` lists files with `git ls-files`, never a filesystem walk, and
  `require_scanned()` turns zero files into exit 2. A walk sees your untracked files and CI does
  not; a checkout under `.claude/worktrees/` once blinded three gates whose exclusion matched
  every absolute path.
- `ci_kit/site_lint.py` `check` is the CLI a pass/fail gate shares: findings exit 1, could not
  look exits 2, a crash mid-scan is exit 2 instead of a traceback.

The tree a gate reads is `--root`, else `CI_KIT_ROOT`, else the git toplevel holding the working
directory. Never the tool's own location.

## A guard must be able to fire

A passing check and a check that cannot fail are byte-identical from the outside. Review does not
catch the difference, and CI actively hides it. So every gate in the kit is proven to fire
before it is trusted:

- **A must-fire suite** (`tests/`). Each gate is driven against a known-BAD input (exit 1), a
  known-GOOD one (exit 0) and a BLIND one it cannot read (exit 2), on throwaway **real** git
  repositories, not mocks. Each selftest names the mutation it dies to.
- **A selftest per gate** (`--selftest`). The gate runs itself against a fixture it must flag
  and one it must pass. ci-kit's own CI runs every selftest through the composite action, exactly
  as callers get it (`.github/workflows/ci.yml`), so a broken `action.yml` fails here and not in
  every caller at once.
- **The blind direction in CI.** Some gates are also run where they must refuse: `ci-pin-drift`
  on a repo that pins nothing, `ci-manual-lint` and `ci-guard-selfproof` with nothing to look at,
  `ci-declared-literal` with no policy. Each must exit 2, and the step checks the tool's own
  refusal line, because a bare 2 could also be the installer failing.
- **Gates on the kit itself.** CI runs `ci-doc-lint`, `ci-secret-argv-lint`, `ci-hidden-unicode`,
  `ci-dup-code`, `ci-blind-parse`, `ci-gate-lint`, the `ci-runners` golden check and
  `ci-gitleaks` (range mode, plus a planted key that must fire) on this repository, plus
  `actionlint` over the reusable workflows and the kit's own gitleaks workflow, called the way
  a consumer calls it.
- **Guards for guards.** `ci-guard-selfproof` requires every guard script in a consumer repo to
  carry a `selftest()`, and `ci-guards-have-tests` requires a test to name it outside prose
  ([gates/ratchets.md](gates/ratchets.md)). A rule that names an example it must flag
  (`ci-declared-literal`'s `--example`) carries its own must-fire proof.

The two failure shapes this exists to prevent are a guard that exists somewhere it cannot fire,
and a guard nothing invokes.

## Shrink-only baselines

A gate that fails on day one is bypassed on day one. So most code-shape gates are **ratchets**:
today's findings are frozen in a baseline committed to the consumer repo, and only growth fails.

- `ci_kit/ratchet.py` defines the mechanism once, in two modes. `RatchetSet` is a frozen set of
  keys: a key not in the baseline is a regression. `RatchetCount` is a frozen count per key: a
  new key, or a grown count, is a regression. Shrinking is never a failure; `--write-baseline`
  captures the tighter number.
- `--write-baseline` keeps the file's own `#` header (`kept_header()`, v1.49.0).
- Some baselines **refuse to grow** even on `--write-baseline`: `ci-dup-code`'s first freeze is
  the only one that may be larger than the last, and `ci-doc-lint`'s baseline is shrink-only.
- A shrink-only baseline must never be resolved with a `.gitattributes` `merge=union` driver:
  union re-adds a deleted line. A heal-ladder regeneration that grows a `shrink: true` baseline
  is handed on to the next tier, never pushed ([pr-ops/heal-ladder.md](pr-ops/heal-ladder.md)).

Waivers, where a gate has them, are inline and must carry a reason (`# standin-ok: <why>`,
`# blind-ok: <why>`, ...). `site_lint.waived_lines` reports a bare waiver shorter than 8
characters as a finding instead of honouring it.

## Extras are the dependency policy

The base package takes **no** dependency. A gate that genuinely needs a library gets it behind an
optional extra, so a consumer installs only what the gates it runs need:

| extra | adds | for |
|---|---|---|
| `sql` | `sqlglot>=30.13` | the parsed-SQL gates, `ci_kit.sql` ([gates/sql.md](gates/sql.md)) |
| `workflow` | `pyyaml>=6.0.2` | `ci-gate-lint` and `ci-image-pr-filter`, which read workflow YAML |
| `cog` | `cogapp>=3.4` | `ci-cog`, which runs cog in-process |
| `runners` | `pyyaml>=6.0.2` | `ci-runners`, which reads the capacity file and its kit ([runners.md](runners.md)) |
| `review` | `pydantic-ai-slim[anthropic]>=2.57,<3`, `mcp>=2.3,<3` | the pre-merge model review gate ([review-gate.md](review-gate.md)) |

The composite action installs the right extra per tool (`action.yml`'s `case`), so a workflow
caller never names one. The review gate's `tier`, `verdict` and `carry` modules stay stdlib-only.

Third-party CLIs that have no PyPI build (jscpd, oasdiff, gitleaks) are not dependencies at all:
they are pinned by version **and** sha256 per platform through `ci_kit/release_bin.py` and fetched
once into a cache ([gates/supply-chain.md](gates/supply-chain.md)). No node, no docker.

## Nothing is assumed about your repo

Every policy is the caller's: patterns, exemptions, directories, baselines, label names, secret
names and runner pools are flags or inputs. Each gate page lists its defaults. Two rules hold
everywhere:

- `args` (the action and every reusable workflow) is word-split by the shell and nothing else:
  plain flags, **no quotes** (they would stay literal) and no expansion of them (`set -f`, since
  v1.10.1), so `--ignore vendor/**/flow.yaml` reaches the tool as that glob.
- An args file (`@path/to/file.args`) holds a policy too long or too regex-heavy for `args`.
  Commit it, so CI and a workstation run read the same rules.
