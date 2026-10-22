"""ci_kit.sql -- gates that read SQL embedded in python source, PARSED (sqlglot, postgres dialect).

Install with the `sql` extra (`ci-kit[sql]`): sqlglot is the only dependency the kit takes, and
only here. Every gate keeps the kit's contract: exit 0 clean, 1 findings, 2 did-not-finish-looking
-- a SQL value sqlglot cannot parse is a 2, never a clean pass.

  sqlast           python module -> parsed SQL `Site`s (the reader every gate shares)
  sqllit           python module -> SQL string literals (tokenizer-based, no parse)
  casts            asyncpg bind/cast footguns (rules A-D)            `ci-sql-casts`
  pit_reads        SQL wall-clock reads, a per-file count ratchet    `ci-pit-reads`
  decimal_reads    uncast NUMERIC columns in projections             `ci-decimal-reads`
  phantom_columns  a column its table does not have                  `ci-phantom-columns`
  column_comments  a migration comment declaring an unenforced domain `ci-column-comments`

Each gate module holds the DETECTION and a `Config` holding the VOICE (gate name, the command a
human re-runs, the doc a failure points at, baseline paths and headers), so a repo that already
had the gate can keep its output byte-identical while the logic lives here once.
"""
