# The library

The gates are built on a handful of shared modules, and a consumer can build its own gates on
them. The base package has no runtime dependency (Python 3.11+).

```toml
[project]
dependencies = ["ci-kit @ git+https://github.com/mjdevaccount/ci-kit@v1.51.0"]
```

```python
from ci_kit import astread, discover, site_lint
from ci_kit.ratchet import RatchetCount
```

| module | what |
|---|---|
| `ci_kit/discover.py` | which files a gate reads, from `git ls-files` |
| `ci_kit/astread.py` | read + parse a source file, or exit 2 |
| `ci_kit/site_lint.py` | the CLI scaffold every python-tree gate shares |
| `ci_kit/ratchet.py` | the shrink-only baseline |
| `ci_kit/testrefs.py` | which names a test file mentions outside prose |
| `ci_kit/dockerfile.py` | a Dockerfile's COPY/ADD instructions as tokens |
| `ci_kit/ntfy.py` | one ntfy push |
| `ci_kit/release_bin.py` | a third-party CLI pinned by version + sha256 ([gates/supply-chain.md](gates/supply-chain.md#release_bin-third-party-clis-pinned-by-digest)) |
| `ci_kit/git_ssh.sh` | a private git dependency in a runner install ([gates/supply-chain.md](gates/supply-chain.md#git_sshsh-a-private-git-dependency-in-ci)) |
| `ci_kit/pr_list.py` | the open-PR list as one narrow GraphQL query ([pr-ops/autoupdate.md](pr-ops/autoupdate.md#the-pr-list-ci_kitpr_listpy)) |
| `ci_kit/sql/sqlast.py`, `ci_kit/sql/sqllit.py` | parsed-SQL readers, the `sql` extra ([gates/sql.md](gates/sql.md)) |

## `discover`

- `tracked()` = `git ls-files`, never a filesystem walk (a walk sees your untracked files, CI does
  not; a checkout under `.claude/worktrees/` once blinded three gates).
- `require_scanned()` = zero files is exit 2.
- `count_by_dir()` + `require_scanned_each()` (v1.9.0) = a floor PER ruled dir, so one tree going
  dark cannot hide under another's hundreds.
- Two NAMED file lists (v1.14.0): `tracked_each()` = absolute paths, every dir contributes or exit
  2 naming `gate[dir]` (the SQL gates' `iter_files`); `tracked_or_empty()` = absolute paths, `[]`
  when no scan dir exists, so the gate exits 2 in its own words (a git failure is still 2).

## `astread`

Read + `ast.parse` a source file, or **exit 2** naming the file and the gate (`blind()`).
BOM-tolerant (`utf-8-sig`), never `errors="replace"`. A parse failure answered with `continue` is
byte-identical to "read and clean"; `ci-blind-parse` ([gates/structure.md](gates/structure.md))
keeps that shape out of a consumer's own gates.

## `site_lint`

The ratchet gate's scan / fixture selftest / CLI: a per-file count (`RatchetCount`) or a set of
keys (`RatchetSet`), with the gate's own words through optional hooks (`header`, `describe`, `ok`,
`written`, and `hard` for findings no baseline grants), so a second ratchet lint is a predicate,
not a copy.

- `check` is the same CLI for a **pass/fail** gate with no baseline (`--selftest`, the gate's own
  mode flags, findings -> 1, could not look -> 2, an optional scan floor; voice via `header` /
  `describe` / `advice` / `ok`, or `report` for output that fits no template).
- v1.49.0: `gate_parser` / `gate_root` / `dir_floors` / `selftest_exit`, the flags a python-tree
  gate shares: `@FILE`, `--root`, `--dir DIR:FLOOR` per-dir floors, `--selftest`, `--baseline` +
  `--write-baseline`.
- v1.14.0: `waived_lines(text, waiver)` = an inline waiver must carry a reason (>= 8 chars, else
  it is BARE and reported, not honoured); `repo_on_path(root, marker)` = put the checkout on
  `sys.path` only when it holds the marker file, so a must-fire fixture repo keeps the installed
  package.

## `ratchet`

`RatchetSet` / `RatchetCount`, the shrink-only baseline every ratchet lint shares
([architecture.md](architecture.md#shrink-only-baselines)); `kept_header()` (v1.49.0) = a
re-freeze keeps the caller's own baseline header.

## `testrefs`

(v1.50.0) Which names a test file mentions OUTSIDE prose (docstrings and comments are not
references) and which repo modules its dotted names resolve to. Shared by `ci-guards-have-tests`
and `ci-untested-modules` ([gates/ratchets.md](gates/ratchets.md)).

## `dockerfile`

(v1.50.0) A Dockerfile's COPY/ADD instructions as token lists (continuations joined, comments
inside them dropped, JSON and shell forms, `--from=` stages skipped), stdlib only. Used by
`ci-image-pr-filter` ([gates/structure.md](gates/structure.md)).

## `ntfy`

ONE ntfy push (env `NTFY_URL`; unset = not pushed). The transport only: callers own the wording
and the dedup (pr-autoupdate's conflict label, for one). Moved out of `pr_autoupdate` (v1.27.0) so
the updater stays policy and any other ci-kit tool pages the same way.
