# Structural pass/fail gates

These gates have no baseline: a finding is a finding. Each is a `site_lint.check` CLI or its own
small CLI, with the same 0/1/2 contract and a `--selftest`.

| command | module | refuses |
|---|---|---|
| `ci-blind-parse` | `ci_kit/blind_parse.py` | a parse-failure handler that answers with a clean file's value |
| `ci-quarantine` | `ci_kit/quarantine.py` | a quarantined test with no reason, issue, or unexpired date |
| `ci-gate-lint` | `ci_kit/ci_gate.py` | a workflow whose skippable jobs do not funnel into one always-running aggregate |
| `ci-api-diff` | `ci_kit/api_diff.py` | a breaking change to a committed OpenAPI contract (oasdiff) |
| `ci-image-pr-filter` | `ci_kit/image_pr_filter.py` | a Dockerfile COPY that the PR build's paths-filter does not list |
| `ci-migration-open-prs` | `ci_kit/migration_open_prs.py` | a migration number already taken on the base or by an older open PR |

## `ci-blind-parse`

The other half of `ci_kit/astread.py` (v1.49.0). Every `except SyntaxError` handler (a tuple
holding it, or its subclasses `IndentationError` / `TabError`) must END in a raise, `sys.exit`, or
a call known never to return (`astread.blind`, plus `--noreturn NAME`). `continue`, `break`,
`pass` and any `return` answer a parse failure with a clean file's value. Structural, on an AST: a
MENTION is not a handler. Waiver `# blind-ok: <why>` (a bare one is a finding). Fewer than
`--min-handlers` handlers is exit 2: the rule cannot fire. ci-kit's own CI runs it on `ci_kit/`.

## `ci-quarantine`

Every `@pytest.mark.quarantine` (`--mark`) carries literal `reason=`, `issue=` (`#N`, `prefix#N`,
`owner/repo#N`, or an issues URL) and `until=YYYY-MM-DD`, not past and at most `--max-days` (60)
out (v1.49.0). `**kwargs`, positional or non-literal arguments are findings. "Today" is `--tz`
(default UTC; an unknown zone is exit 2, never a guessed date) or `--today`. `--summary PATH`
appends the mark table (a nightly's step summary).

Quarantine lets a known-bad test leave the gating run while a non-gating job keeps reporting it;
this gate keeps quarantine from being a quiet delete. An expired mark fails the PR that happens to
run after it, on purpose: extend it or fix the test, with the issue in hand.

## `ci-gate-lint`

**The `workflow` extra** (PyYAML), v1.13.0. **A job that can SKIP must funnel into one
always-running aggregate.** GitHub counts a skipped job as a passing required check and lets
auto-merge through: a consumer PR (2026-11-06) merged with four required jobs skipped after its
paths-filter `detect` job died on runner DNS. The fix is one `if: always()` job running
[re-actors/alls-green](https://github.com/re-actors/alls-green) over `${{ toJSON(needs) }}`,
needing every job, as the ONLY required check. This gate keeps that shape from rotting. It flags:

- a job with `needs:` and no aggregate, or more than one aggregate;
- an aggregate that is not `always()`, or judges something other than `toJSON(needs)`;
- a job outside the aggregate's `needs` (the aggregate does not judge it: protection must name
  it, or it joins `needs`);
- an aggregate on a self-hosted or expression runner with no `setup-python` before it
  (alls-green runs `python -m`; a python3-only image exits 127);
- an `allowed-skips` entry (a JSON list or comma-separated, as alls-green reads it) that is an
  upstream (`detect`), has no `if:`, or names no job.

`--workflow` is repeatable (default `.github/workflows/ci.yml`); a missing, unparseable or jobless
workflow is exit 2. Branch protection naming the aggregate lives outside the checkout; an audit
that reads the repository settings has to check that half.

## `ci-api-diff`

A committed OpenAPI contract may **grow, never break**. [oasdiff](https://github.com/oasdiff/oasdiff)
1.35.0 (a pinned release binary, like jscpd) classifies the spec at
`git merge-base <--base-ref> HEAD` against the working-tree file (`--path`, default
`docs/api/openapi.json`) as ERR / WARN / INFO. Any ERR (a route removed, a response field
dropped, a request parameter made required) is **exit 1**.

- The verdict is parsed from `oasdiff changelog --format json`, never its exit code.
- No merge base, an unparseable spec or no binary is **exit 2**; a spec NEW at the merge base
  cannot break (0, and it says so).
- A deliberate break is a line in oasdiff's own `--err-ignore FILE`, committed in the breaking PR.
  There is no skip flag.
- `--base A --revision B` compares two files.
- Library: `changes()` (classified) and `structural()` (what moved, by operation and component,
  including description-only edits the changelog does not classify).

## `ci-image-pr-filter`

**The `workflow` extra**, with `ci_kit/dockerfile.py` (v1.50.0). Every tracked FILE the
`--dockerfile` COPYs or ADDs by name is listed in the `--workflow`'s dorny/paths-filter `--filter`
key, so a PR renaming it cannot get a green "nothing to build". The filter is compared exactly (a
list of paths, not globs: a glob matcher here would be a second picomatch that accepts more than
the real one). A COPY of a path the repo does not hold is a finding; no filter key or no COPY is
exit 2.

## `ci-migration-open-prs`

A numbered migration (`--dir`, `--pattern` with one group) this branch ADDS may not reuse a number
on `--base`, or one an OLDER open, non-draft PR adds (v1.50.0; stdlib HTTP). Each PR's CI compares
against the base only, so two open PRs can both add `387_*.sql` and both go green; merged, a
migrator that skips any file at or below its max version never runs the second one. No error,
no row. A newer PR holding the same number is that PR's finding: whoever opened second renumbers.

- ONE GraphQL call over every open PR (`--repo`, else `$GITHUB_REPOSITORY`; token `GH_TOKEN` /
  `GITHUB_TOKEN` / `gh auth token`). `--prs-json` replays a saved list.
- No token or unreachable is exit 2; so is a pattern that matches nothing on the base or the
  branch (a repo with no migrations yet included: a mistyped `--dir` must not pass).
