# SQL gates (`sql` extra)

`ci_kit.sql` holds gates that read SQL embedded in python source **parsed** (sqlglot, postgres
dialect), so a docstring, a `-- comment` or a string literal that merely *mentions* SQL is never
read as SQL, and a SQL value that will not parse is **exit 2**, never a clean pass.

Install with the `sql` extra (`ci-kit[sql]`): sqlglot is the only dependency the kit's gates take
at their core, and only here. The composite action installs it for these tools on its own.

| command | module | finds |
|---|---|---|
| `ci-sql-casts` | `ci_kit/sql/casts.py` | asyncpg bind/cast footguns, rules A-D |
| `ci-pit-reads` | `ci_kit/sql/pit_reads.py` | SQL wall-clock reads (`now()`, `current_date`, ...), a per-file count ratchet |
| `ci-decimal-reads` | `ci_kit/sql/decimal_reads.py` | a NUMERIC column projected without a cast |
| `ci-phantom-columns` | `ci_kit/sql/phantom_columns.py` | a single-table statement naming a column its table lacks |
| `ci-column-comments` | `ci_kit/sql/column_comments.py` | a migration column comment that declares a domain with no CHECK enforcing it |
| `ci-sql-placement` | `ci_kit/sql/placement.py` | SQL outside the repository layer (a ratchet, see [ratchets.md](ratchets.md#ci-sql-placement)) |

## The gates

- **`ci-sql-casts`**: `:x::type` (A), `CAST(:x AS timestamptz)` on a str (B, ratchet),
  `now() - :age` (C), `:x + interval` (D, ratchet).
- **`ci-pit-reads`**: SQL wall-clock reads give plausible numbers from the WRONG day under a
  replay clock; a per-file count ratchet.
- **`ci-decimal-reads`**: a NUMERIC column projected without a cast decodes as `Decimal`. Ground
  truth is a `--cols` file of `schema.table.column`; a missing or empty one is exit 2.
- **`ci-phantom-columns`**: checked against a `--schema` JSON built from a migrated database.
  Precision over recall.
- **`ci-column-comments`**: a comment that declares a domain (a magnitude, `0..1`, an enum,
  `>= 0`) with no CHECK enforcing it.

## Shared readers and the `Config` voice

`sqlast` (python module to parsed SQL `Site`s) and `sqllit` (python module to SQL string
literals, tokenizer-based, no parse) are the shared readers. Statement heads: SELECT, INSERT,
UPDATE, DELETE, WITH, CREATE ... AS, and since v1.9.0 TRUNCATE.

Each gate keeps its detection here and takes its voice (gate name, re-run command, doc pointer,
baseline paths) from a `Config`, so a repo that already had the gate keeps its output
byte-identical. Since v1.5.0 every gate's `main` is a `site_lint.check` CLI: `--selftest` works
under a wrapper's `Config`, a crash mid-scan is **exit 2** (did not finish looking) instead of a
traceback, and `--write-baseline` / `--list` are its mode flags. So a repo wrapper that hands its
argv to `ci_kit.sql.<gate>.main` gets all of that too.

## Using them

As a dev dependency (their baselines live in your repo):

```toml
[dependency-groups]
dev = ["ci-kit[sql] @ git+https://github.com/mjdevaccount/ci-kit@v1.51.0"]
```

```bash
uv run ci-sql-casts --scan-dir app --baseline ci/sql_casts_baseline.txt --d-baseline ci/sql_bind_arith_baseline.txt
uv run ci-pit-reads --scan-dir app --baseline ci/pit_reads_baseline.txt
uv run ci-decimal-reads --scan-dir app --cols ci/numeric_columns.txt --baseline ci/decimal_reads_baseline.txt
uv run ci-column-comments --migrations migrations --legacy-max 7
```
