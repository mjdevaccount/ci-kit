# Versioning and releases

## The rule

Change, PR, CI green, merge. **Library releases are immutable `vX.Y.Z` tags, never moved**: a
consumer's lockfile records the tag and its commit, so moving one breaks `--frozen` installs; a fix
is the next patch tag, and `version` in `pyproject.toml` moves with it. A breaking change is a new
major (`v2`), with every reference to the old major updated in the same commit.

The repo is **version-first**: the PR that moves `version` IS the release.

- A PR that adds behaviour (`feat:`) bumps the minor (`1.50.0` -> `1.51.0`).
- A PR that fixes behaviour (`fix:`) bumps the patch (`1.50.0` -> `1.50.1`).
- A PR that changes no shipped behaviour (docs, a lockfile refresh, CI-only tooling) keeps the
  version.

CI holds the two together: the `test` job's step "pyproject version is not behind the newest
release tag" turns the NEXT PR red when `version` is behind the newest `vX.Y.Z` tag (v1.6.1, v1.7.0
and v1.7.1 were all cut from commits that still said 1.6.0). A tag is immutable once pushed, so the
check cannot stop a stale one; a version bumped ahead of its tag passes. Exit 2 = it could not read
the version or the tags.

## Nobody cuts a tag by hand

**`ci-version-tag`** (`ci_kit/version_tag.py`, v1.20.0). In a version-first repo every version
bump on the default branch's first-parent history gets its `vX.Y.Z` tag at the bump commit, and
every tag its release.

- An untagged bump is tagged through the API, verified, and `--release-workflow` is dispatched on
  it. A GITHUB_TOKEN tag fires no push workflow; `workflow_dispatch` is the event that token may
  start, so the release workflow must read `github.ref_type == 'tag'`, never
  `event_name == 'push'`.
- A tag missing its GitHub release (`--github-release`) or its PyPI files (`--pypi NAME`): no run =
  dispatch, a run in flight = pending, a run that FINISHED and left it missing = exit 1 (a failed
  publish is rerun by a person, never re-dispatched blind).
- `.devN` / `+local` versions and versions below `--since` are skipped; a tag on another commit is
  printed, never moved.
- Writes only on the default branch (elsewhere = dry run).

It was built after a sibling library's release merged and was never tagged, with nothing red.
Reusable: `version-tag.yml` ([reusable-workflows.md](reusable-workflows.md#version-tagyml)).

**ci-kit's own caller** is `.github/workflows/tag-version.yml`: it runs `ci-version-tag` on every
`pyproject.toml` change to main (and daily, and on `workflow_dispatch` with a `dry_run` input),
tags the bump commit, and dispatches `major-tag.yml` on the new tag, so the major follows in the
same run.

## The moving major: `v1`

The reusable workflows reference the action as `mjdevaccount/ci-kit@v1`, and consumers pin
`@v1` (`doc-refs.yml@v1`, `uvx --from ...ci-kit@v1`). **Nobody moves it by hand**:
`.github/workflows/major-tag.yml` points each `vX` at the highest `vX.Y.Z` on every release tag
push, on `workflow_dispatch`, and daily, and verifies the move with `ls-remote`.

- vX moves to the commit of the highest vX.Y.Z (semver order, `sort -V`), never to a lower one, so
  a backport tag (v1.4.1 after v1.6.0) leaves v1 alone.
- Pre-release tags (v1.7.0-rc1) never move it. A major with no vX tag yet gets one.
- The daily cron is the outside witness that heals a major tag left behind by a failed run.
- Also reusable (`workflow_call`): another repo runs the same job against ITS OWN tags; the caller
  declares the triggers and `contents: write`
  ([reusable-workflows.md](reusable-workflows.md#major-tagyml)).

**Rollback floor:** moving `v1` back goes no lower than v1.40.0. Since v1.42.0 the reusables send
the pr-token composite input names (`client-id`, `deprecated-client-id`, `deprecated-private-key`)
that actions older than v1.40.0 do not declare ([pr-ops/push-token.md](pr-ops/push-token.md)).

## Pinning

| how you consume it | pin |
|---|---|
| a reusable workflow or the action | `@v1` (the moving major) |
| the library, from a lockfile | an immutable tag: `ci-kit @ git+https://github.com/mjdevaccount/ci-kit@v1.51.0` |
| `uvx` from a Makefile or a shell | either; an immutable tag is reproducible |
| pre-commit | `rev: vX.Y.Z` |

`ci-pin-drift` ([gates/supply-chain.md](gates/supply-chain.md)) fails a repo whose pins of this kit
lag the newest release. Renovate's shared preset bumps first-party kit pins in a daily window
([gates/supply-chain.md](gates/supply-chain.md#renovatedefaultjson5-the-shared-renovate-preset)).
