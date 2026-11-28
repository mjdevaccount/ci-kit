# Ratchets

A ratchet freezes today's findings in a baseline the consumer repo commits, and fails only on
growth. Shrinking is never a failure. The mechanism is `ci_kit/ratchet.py` (`RatchetSet` /
`RatchetCount`, see [architecture.md](../architecture.md#shrink-only-baselines)), and most of
these gates are a predicate on `ci_kit/site_lint.py`'s shared scan and CLI
([library.md](../library.md)), so they share flags: `@FILE`, `--root`, `--dir DIR:FLOOR`,
`--selftest`, `--baseline` and `--write-baseline`.

| command | module | may not grow | baseline (default) |
|---|---|---|---|
| `ci-line-cap` | `ci_kit/line_cap.py` | modules over the line cap, or their length | `ci/line_cap_baseline.txt` |
| `ci-function-clones` | `ci_kit/function_clones.py` | functions that are structural twins of one in another file | `ci/function_clones_baseline.txt` |
| `ci-dup-code` | `ci_kit/dup_code.py` | token-level code clones (jscpd) | `ci/jscpd-baseline.json` |
| `ci-test-standins` | `ci_kit/standins.py` | signature-blind test stand-ins | `ci/test_standins_baseline.txt` |
| `ci-untested-modules` | `ci_kit/untested_modules.py` | source modules no test references | `ci/untested_modules_baseline.txt` |
| `ci-guards-have-tests` | `ci_kit/guards_have_tests.py` | guard scripts no test names | `ci/guards_without_tests.txt` |
| `ci-http-clients` | `ci_kit/http_clients.py` | hand-built HTTP clients outside the seam | `ci/http_clients_baseline.txt` |
| `ci-sql-placement` | `ci_kit/sql/placement.py` | SQL outside the repository layer | `ci/sql_placement_baseline.txt` |
| `ci-env-reads` | `ci_kit/env_reads.py` | hand pipelines reading a `.env` file | `ci/env_reads_baseline.txt` |
| `ci-guard-selfproof` | `ci_kit/guard_selfproof.py` | guard scripts with no selftest | `<scripts>/guard_selfproof_baseline.txt` |

## `ci-line-cap`

A python module over the line cap (`--cap`, default 400, PHYSICAL lines) may not appear, and one
already over may not grow (v1.49.0). A per-file `RatchetCount`; `--write-baseline` keeps the
file's own `#` header. Every `--dir DIR:FLOOR` is read against its OWN floor (a total hides a tree
that read nothing), and with any `--unruled DIR:WHY`, tracked python under a top-level dir neither
ruled nor excused is exit 2 (`.` = repo-root files).

Why a ratchet: in the repo this came from, a 400-line cap was stated as hard in three places and
enforced nowhere. Measured 2026-09-18, 47 of 859 modules were over it, 7,891 lines over in total.
A rule violated 47 times without one failing build is a preference, and worse than no rule,
because review keeps citing it with authority the check never earned. The OK line says how much
slack the baseline can give back.

## `ci-function-clones`

A file may not ADD a function that is an exact structural twin of one in ANOTHER file (v1.49.0).
Every function and method (nested ones included) is normalised: names and arguments become `_`,
constants their type, the docstring is dropped, annotations go, attribute names are KEPT (so
`self.repo.fetch()` and `self.repo.store()` stay distinct). `--min-nodes` (default 40) sets the
size floor. A per-file count ratchet; a copy into a new file names both files. It is the
rename-proof sibling of `ci-dup-code`'s token windows: the duplication that hurts is usually
re-implemented logic that then drifts, not a pasted block.

## `ci-dup-code`

**The same code written twice** fails the PR. [jscpd](https://github.com/kucherenko/jscpd) 5.4.2
(pinned release binary, sha256-verified, cached under `CI_KIT_CACHE_DIR`; no node) over code
formats (python, bash, powershell, csharp, js/ts, sql, yaml, ...; json/md/toml are noise, not code)
at **70 tokens / 7 lines**. Measured 2026-10-31 on three live repos: below that the hits are
5-line test prologues, above it they are copied helpers and lab-script twins, the complaint itself.

- jscpd's own fingerprint baseline (`ci/jscpd-baseline.json`, `--write-baseline`): a line shift
  or a rename is not new; an edit INSIDE a frozen clone splits it into new clones (consolidate,
  not re-freeze). `--write-baseline` **refuses to grow**: the first one is the only one that may
  be larger than the last.
- A clone counts only between two `git ls-files` files, so CI and a laptop agree.
- The verdict is read from the JSON report, never from jscpd's exit code (0 on a missing path and
  an empty scan): no report, or zero files analyzed, is **exit 2**.
- Kit ignores: worktrees, venvs, `**/migrations/**`, fixtures/testdata/golden/snapshots,
  lockfiles, minified files. A repo adds `--ignore GLOB` (repeatable), `--format LIST`,
  `--min-tokens` / `--min-lines`.
- Chosen over pylint `duplicate-code`: that reads one language and reports a clone with no stable
  identity to baseline.

```bash
uvx --from git+https://github.com/mjdevaccount/ci-kit@v1.51.0 ci-dup-code --write-baseline   # once: freezes today's clones
```

Reusable workflow: `dup-code.yml` (no history needed; commit `ci/jscpd-baseline.json`).
Pre-commit hook: `dup-code`.

## `ci-test-standins`

A test may not ADD a SIGNATURE-BLIND stand-in (v1.50.0): a `lambda` or a same-file def installed
over a `--package` (required) callable by `monkeypatch.setattr` / `mock.patch` / `patch.object`
carries its own signature, so a new keyword on the real function passes PR CI and raises later.
The fix is `mock.create_autospec(real, side_effect=fake)`: the fake takes the real signature.
Per-file `RatchetCount`; waiver `# standin-ok: <why>` (a bare tag is reported).

## `ci-untested-modules`

A source module under a `--dir` that no tracked test under `--tests` imports or names may not
appear (v1.50.0). Dotted names are resolved against the tree; `__init__` and protobuf stubs are
skipped; `--exclude-part` drops a path segment. Shrink-only `RatchetSet`; waiver
`# untested-ok: <why>` in the first 10 lines. One definition of "tested", via
`ci_kit/testrefs.py`, replaced three methods that counted the same thing three ways (117, 166, 92).

## `ci-guards-have-tests`

Every guard script (`--guards` dir, `--prefix` / `--suffix`) is NAMED by a tracked test outside
prose (v1.50.0): a docstring or comment mention is not a test. A guard whose only tests carry a
`--deselected-mark` (never run in the PR suite) is reported. Shrink-only `RatchetSet`. It parses
rather than greps because its first, grep-based life reported every guard covered; parsed, one
was not: its only three mentions were docstrings. Shares `ci_kit/testrefs.py` with
`ci-untested-modules`, so the two cannot disagree about what "a test references X" means.

## `ci-http-clients`

A hand-built HTTP client outside the repo's seam (`--seam PATH`, where timeouts and redaction
live) may not grow, per file (v1.50.0): an `httpx.Client` / `AsyncClient` call (aliases followed),
an httpx module helper (`httpx.get(` opens its own client), or an import of `requests` /
`urllib.request` / `urllib3`. Read from the AST, so a docstring is never a client. `--use TEXT` is
the fix the finding names. Per-file `RatchetCount`.

## `ci-sql-placement`

**The `sql` extra.** SQL outside the repository layer may not grow (v1.50.0): parsed
INSERT/UPDATE/DELETE/TRUNCATE statements (through `sqlast.module_sites`) plus sqlalchemy `text(`
calls, per file, minus `--owner` / `--exempt` path prefixes (`*` = one segment). Per-file
`RatchetCount`. The other parsed-SQL gates are on [sql.md](sql.md).

## `ci-env-reads`

A shell script pulls a key out of a `.env` FILE with a hand pipeline (`sed -n 's/^K=//p' FILE`,
`grep '^K=' FILE | cut -d= -f2-`, `awk -F= '/^K=/' FILE`) instead of the repo's reader. Hand
copies disagreed in RESULTS: first vs last duplicate, quotes kept, a value cut at `#`, `$$` left
doubled where docker compose yields `$`. Reading from a pipe (`docker inspect ... | sed`) is quiet
on purpose. Per-file count ratchet (`--baseline`), `--exempt` the reader itself, waiver
`# env-read-ok: <why>`.

## `ci-guard-selfproof`

Every guard script proves it can FAIL: a module-level `selftest() -> list[str]` (empty = proved)
rejecting a known-bad input and accepting a known-good one, or `--selftest` for a JS guard. A
guard without one is a shrink-only baseline entry; a NEW one fails. A selftest returning an int,
raising, or failing to import is a failure by name.

- Defaults: reads `scripts/` for `lint_*`. `--scripts`, `--prefix`, `--baseline` and `--js` are
  yours; `--js` proves `.mjs` / `.js` guards through `node <guard> --selftest`, which must exit 0
  AND print a last line `<name> selftest: OK`.
- **Run it where your guards' dependencies are installed** (`uv run`, or `pip install` ci-kit
  into the CI python): importing a guard imports what it imports. The action's isolated uvx
  environment holds ci-kit alone, so it fits only a repo whose guards are stdlib-only.
- With no `scripts/` to read it exits 2.

The case that made it standing: an adversarial audit (2026-08-17) found three ratchets that could
not fire (a warning budget running a bare `npx` that reported "0 warning(s), baseline 54", a route
lint blind to 25 of 98 routes, a lint ruleset outside the CI change filter). All three were green
and all three had passed human review.
