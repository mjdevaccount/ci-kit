# Contributing

## Setup

```bash
uv sync --frozen --all-extras     # the package, every extra, the dev group
uv run pytest -q                  # the must-fire suite
uv run ruff check .
```

CI (`.github/workflows/ci.yml`) runs the same plus everything below; a PR merges when it is green.

## What a new gate needs

1. **The exit contract.** 0 clean, 1 findings, 2 could not look
   ([architecture.md](architecture.md#the-exit-contract-0--1--2)). Read files with
   `ci_kit/astread.py` and list them with `ci_kit/discover.py` (`git ls-files`, never a walk), so a
   parse failure or an empty scan is exit 2 by construction. Build the CLI on
   `ci_kit/site_lint.py` (`check` for pass/fail, the ratchet CLI for a baseline) rather than a new
   argument parser ([library.md](library.md)).
2. **A `--selftest`** that runs the gate against a fixture it must flag and one it must pass, and
   names the mutation it dies to.
3. **A must-fire suite** in `tests/test_<gate>_must_fire.py`: a known-BAD input (exit 1), a
   known-GOOD one (exit 0) and a BLIND one it cannot read (exit 2), on throwaway **real** git
   repositories (the `new_repo` / `tracked_repo` fixtures), not mocks. A guard only ever seen
   passing is indistinguishable from one that cannot fail.
4. **A console script** in `pyproject.toml` `[project.scripts]`, and the tool name in
   `action.yml`'s description and its `case` (with the extra it needs, if any).
5. **A CI step** that runs its selftest through the action (`uses: ./`), exactly as callers get it,
   and, where it applies, the gate on ci-kit itself.
6. **No policy in the kit.** Patterns, exemptions, directories and names are the caller's flags;
   the kit ships no allowlist ([architecture.md](architecture.md#nothing-is-assumed-about-your-repo)).
7. **A dependency only behind an extra**, and only when a library does the job better than the
   stdlib ([architecture.md](architecture.md#extras-are-the-dependency-policy)). A third-party CLI
   is pinned through `release_bin` by version and sha256, never installed by a package manager.
8. **A doc**: a section on the gate's page under `docs/gates/` (or its kit page), and a row in the
   root `README.md` table.

## What CI runs

The `test` job: the version check ([versioning.md](versioning.md)), ruff, the must-fire suite,
every gate's selftest through the action, the gates on ci-kit itself (`ci-doc-lint`,
`ci-secret-argv-lint`, `ci-hidden-unicode`, `ci-declared-literal` with its scan receipt,
`ci-blind-parse`, `ci-dup-code` against `ci/jscpd-baseline.json`, `ci-gate-lint` on `ci.yml`,
the `ci-runners` golden check, `ci-gitleaks` over the event's range plus a planted key that must
fire), the blind directions (`ci-pin-drift`, `ci-manual-lint`, `ci-guard-selfproof` and
`ci-declared-literal` must each exit 2 with their own refusal line), a cold-then-warm `ci-tf-seed`
and `actionlint`. Separate jobs: a live merge-train timeline probe, `renovate-config-validator
--strict` on the shared preset and this repo's `renovate.json`, and the kit's own gitleaks
workflow, called the way a consumer calls it.

## Pull requests

- One change per PR, conventional-commit title (`feat:`, `fix:`, `docs:`, `chore:`).
- Bump `version` in `pyproject.toml` in the same PR when shipped behaviour changes: minor for a
  feature, patch for a fix ([versioning.md](versioning.md)). Merging the bump is the release; the
  tag and the `v1` move are automatic.
- Never move or delete a `vX.Y.Z` tag.
- Every tracked markdown file is gated by `ci-doc-lint`: a doc that names a file you delete fails
  until it stops naming it (or declares itself `<!-- doc-rot: historical -->`).
- Public repository: no credentials, hostnames or private names in code, tests or docs. The
  secret scan and `ci-declared-literal` run on every PR.
