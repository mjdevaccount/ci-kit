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
| `ci_kit/pr_autoupdate.py` | `ci-pr-autoupdate`: merge main into every open PR with auto-merge **armed**, so "require branches up to date" is affordable; a conflict gets one label and one page, never more |
| `ci_kit/pin_drift.py` | `ci-pin-drift`: every `<kit>@vX.Y.Z` / `?tag=vX.Y.Z` pin in the given files is the newest release tag on `--remote`, else exit 1. A kit fix that never reaches a consumer must not fail silently |
| `ci_kit/manual_lint.py` | `ci-manual-lint`: every path an agent manual (`CLAUDE.md`) names **resolves today** in `git ls-files`. The question `ci-doc-lint` deliberately cannot answer -- a path that was never real -- asked only of the file every session loads. Quiet by SHAPE (routes, flags, prose with slashes, image tags, formulas), not by allowlist; a sibling-repo pointer is checked when that checkout is present and silent when not. Optional: stale migration ranges, line budget, empty placeholder packages, reference-doc dirs + freshness stamp, a total index |
| `ci_kit/guard_selfproof.py` | `ci-guard-selfproof`: every guard script proves it can FAIL -- `selftest() -> list[str]` (or `--selftest` for a JS guard) rejecting a known-bad input and accepting a known-good one. A guard without one is a shrink-only baseline entry; a NEW one fails. A selftest returning an int, raising, or failing to import is a failure by name |
| `ci_kit/ratchet.py` | library: `RatchetSet` / `RatchetCount`, the shrink-only baseline every ratchet lint shares |
| `ci_kit/discover.py` | library: `tracked()` = `git ls-files`, never a filesystem walk (a walk sees your untracked files, CI does not; a checkout under `.claude/worktrees/` once blinded three gates); `require_scanned()` = zero files is exit 2 |
| `ci_kit/site_lint.py` | library: the ratchet gate's scan / fixture selftest / CLI -- a per-file count (`RatchetCount`) or a set of keys (`RatchetSet`), with the gate's own words through optional hooks (`header`, `describe`, `ok`, `written`, and `hard` for findings no baseline grants) -- so a second ratchet lint is a predicate, not a copy |
| `ci_kit/astread.py` | library: read + `ast.parse` a source file, or **exit 2** naming the file and the gate (`blind()`); BOM-tolerant (`utf-8-sig`), never `errors="replace"`. A parse failure answered with `continue` is byte-identical to "read and clean" |
| `action.yml` | composite action: run one of the above against the caller's checkout, installed from the action's own download (no second ref, no credential) |
| `.github/workflows/doc-refs.yml` | reusable: the doc gate + its selftest, full history (`fetch-depth: 0` is load-bearing; a shallow clone is exit 2, not a blind pass) |
| `.github/workflows/secret-argv.yml` | reusable: the secret-argv lint + its selftest |
| `.github/workflows/gitleaks.yml` | reusable: secret scan, `mode: tree / history / action` |
| `.github/workflows/pr-autoupdate.yml` | reusable: the armed-PR updater; output `relock` |
| `.pre-commit-hooks.yaml` | hooks `doc-refs`, `secret-argv` |

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
  gitleaks:
    name: gitleaks (secret scan)
    permissions: { contents: read, pull-requests: read }
    uses: mjdevaccount/ci-kit/.github/workflows/gitleaks.yml@v1
    with: { mode: tree }
```

```yaml
# on push to main
jobs:
  update:
    uses: mjdevaccount/ci-kit/.github/workflows/pr-autoupdate.yml@v1
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

```python
from ci_kit import astread, discover, site_lint
from ci_kit.ratchet import RatchetCount
```

Locally, or from a Makefile:

```bash
uvx --from git+https://github.com/mjdevaccount/ci-kit@v1.0.0 ci-doc-lint --root ../x
uvx --from git+https://github.com/mjdevaccount/ci-kit@v1.0.0 ci-pin-drift --file uv.lock --remote https://github.com/mjdevaccount/ci-kit.git
```

pre-commit:

```yaml
- repo: https://github.com/mjdevaccount/ci-kit
  rev: v1.0.0
  hooks: [{ id: doc-refs }, { id: secret-argv }]
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
  `--baseline` and `--js` (prove `.mjs`/`.js` guards through `node <guard> --selftest`) are yours.
- `pr-autoupdate.yml` declares generic secret names; `--label` renames the conflict label.
- The tree a gate reads is `--root`, else `CI_KIT_ROOT`, else the git toplevel holding the
  working directory. Never the tool's own location.
- `runs-on` for the no-secret workflows is the caller's `CI_RUNNER` repository variable (a JSON
  label list) when set, else `ubuntu-latest`. The secret-holding workflows are hosted on purpose.

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
workflows reference the action as `mjdevaccount/ci-kit@v1`, a moving major that is re-pointed at
each release (`git tag -f v1 && git push -f origin v1` only after that release's CI is green); a
breaking change is a new major (`v2`) with those references updated in the same commit.
