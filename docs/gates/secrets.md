# Secret, literal and hidden-Unicode gates

| command | module | refuses |
|---|---|---|
| `ci-secret-argv-lint` | `ci_kit/secret_argv.py` | a secret expanded on the host into a `docker exec` / `curl` / `ssh` argv |
| `ci-gitleaks` | `ci_kit/gitleaks.py` | a committed secret (gitleaks, pinned by version + sha256) |
| `ci-hidden-unicode` | `ci_kit/hidden_unicode.py` | an invisible, bidi or control character in any tracked text file |
| `ci-declared-literal` | `ci_kit/declared_literal.py` | a literal that must live in ONE declared file appearing anywhere else |

## `ci-secret-argv-lint`: a secret expands where it is used

No secret expanded on the **host** into a `docker exec` / `curl` / `ssh` argv, where it lands in
`docker events`, `docker inspect` and `ps`, nor hoisted onto the host with
`$(docker exec c printenv KEY)`. The fix is to let it expand where it lives:
`docker exec c sh -c '... "$KEY" ...'`.

The case that started it (2026-10-20): timer scripts read an API key out of a running container
with `docker exec <c> printenv`, then passed it back in as `docker exec <c> curl -H "X-API-Key: $K"`.
The host shell expanded `$K`, so the value sat in plain text in every `docker events` record, in
`docker inspect` of the exec and in host `ps`. Ten scripts carried the shape.

Three rules, read off logical shell lines (backslash continuations joined) of every tracked
`.sh` / `.bash` / `.yml` / `.yaml` / `Makefile` / shebang-sh file; markdown and comment lines are
exempt:

- **R1 exec-argv.** A `docker exec` / `docker run` (and their compose forms) whose argv carries a
  host-expanded secret: an auth header (`-H "X-API-Key: $.."`, Authorization, Cookie,
  `X-*-Token`), `--password` / `--token "$.."`, `-u "user:$.."`, `-e NAME_KEY="$.."`, or any `$VAR`
  whose name is secret-shaped (`*API_KEY`, `*TOKEN`, `*SECRET`, `*PASSWORD`). Single-quoted spans
  are masked: they reach the container unexpanded, which is the fix. For `-e`, pass the NAME and
  let docker read the value from its own env: `PGPASSWORD="$p" docker run -e PGPASSWORD ...`.
- **R2 hoist.** `$(docker exec ... printenv <SECRET_NAME>)`, or a `$(docker exec ...)` whose
  container command echoes or printfs a secret-named variable: reading a secret onto the host at
  all. Piped straight into a hash (`... | sha256sum`) it is a fingerprint and is quiet, as is a
  pipe into the curl/wget that sends it; any other pipe still lands the value. Read inside `$( )`,
  `< <( )` and backticks, through `sudo` / `timeout N`.
- **R3 host-argv.** A host-side curl / wget / ssh whose argv carries the same secret shapes as R1,
  visible in `ps` to every process on the box. Pass it on stdin or a process substitution instead:
  `curl -H @<(printf 'X-API-Key: %s\n' "$K")`.

A line that ends inside an open quote (a multi-line `sh -c '...'`) is joined too, up to 20
physical lines.

Reusable workflow: `secret-argv.yml`. Pre-commit hook: `secret-argv`.

## `ci-gitleaks`: the secret scan, pinned

The secret scan from a pinned [gitleaks](https://github.com/gitleaks/gitleaks) release (v1.17.0):
pinned by version and sha256 through `release_bin`, cached, retried, with a REST asset fallback.
No docker and no third-party action, so it runs on a pool without a docker socket. It replaced
`gitleaks-action` after that action's tarball download answered 504 for every repository for over
an hour while the REST asset API served the same bytes (2026-11-08).

| mode | scans |
|---|---|
| `range` | a PR's own commits (`base.sha..head.sha`), a push's (`before..after`; a new branch scans after's ancestry), a merge group's, and every ref on any other event |
| `history` | HEAD's ancestry, **never `--all`**: a fixture on another open branch must not fail this PR |
| `tree` | the working tree only (`--no-git`) |

- A scan that looked at nothing is exit 2, never a pass. `range` asks git (`rev-list --count`)
  whether the rev spec matched any commit, because gitleaks' own "N commits scanned" counts only
  commits that added text; `history` with zero commits scanned is exit 2. A range needs
  `fetch-depth: 0`; a shallow clone is exit 2, loudly.
- **A baseline entry that can never match is exit 2, before any scan.** Every `.gitleaksignore`
  gitleaks loads (`./`, `<source>/`, and `ignore-path`, a file or a folder) is judged, and a dead
  entry (an absolute `/repo/` path, a malformed or trailing-comment fingerprint) is refused. So
  `ignore-path: ''` still checks the repo's own baseline. The case: fingerprints written by an
  older docker-based scan read `/repo/...`, matched nothing under the new scan, and a PR went red
  on an item the file said was handled.

Reusable workflow: `gitleaks.yml` (`mode: tree | history | range`; `action` is the old name for
`range`).

## `ci-hidden-unicode`: no invisible characters

No invisible character in ANY tracked text file (v1.31.0), in file contents AND file names:
zero-width characters, bidi embed/override/isolate (Trojan Source, CVE-2021-42574), variation
selectors, tag characters, Hangul fillers, soft hyphen, CGJ, Braille blank, C0/C1 controls (a raw
NUL in a `.ts` / `.cs` string included), and FEFF past byte 0. **Write the escape; there is no
waiver.** A reviewer cannot tell a harmless invisible character from a bidi override that makes
code read differently from how it runs, because both are invisible in the diff. So the rule is
"none, written raw": `​` in a Python or JSON string, the bare emoji in markdown.

- **`--renovate`** (v1.49.0) ALSO refuses NBSP, the Unicode spaces and U+2028/2029: Renovate's
  pinned `hiddenUnicodeCharactersRegex`, so the gate fires before the dependency dashboard does.
  `json_escaped()` writes JSON that passes either set.
- **Binary files.** A file is skipped as binary only when its suffix is declared binary (the
  kit's list plus `--binary-suffix .EXT`; a text suffix is refused) AND its bytes are not UTF-8,
  so no suffix can hide UTF-8 text. Any other non-UTF-8 file, or a tracked path missing on disk,
  is exit 2, and EVERY such file is named with its fix.

Reusable workflow: `hidden-unicode.yml` (`args` for `--binary-suffix`). Pre-commit hook:
`hidden-unicode`.

## `ci-declared-literal`: one value, one file

A literal that must live in ONE declared file (an address range, a hostname, a port, an account
id) appears in no other tracked file (v1.39.0). Everything else reads it from that file, so moving
the value is one edit, not a grep-and-pray sweep.

**The kit ships no policy.** Each `--rule LABEL` carries its own `--pattern` (Python `re`,
repeatable, OR'd), `--example` (repeatable, required), `--declared` files and `--message`;
`--exempt-suffix` exempts a file kind. A regex cannot ride in the word-split `args`, so the policy
is an args file, UTF-8, one argument per line (blank and `#` lines are notes):

```text
--rule
nets
--pattern=\b192\.0\.2\.\d{1,3}\b
--example=GW=192.0.2.1
--declared
.env.example
--message
<your remediation>
--exempt-suffix
.md
```

- **Every rule proves it can fire.** Each `--example` must be flagged, each `--pattern` must match
  an example, and a declared file that is present must hold a match; otherwise exit 2. No rule, a
  bad or empty-matching regex, or zero files read is exit 2.
- Several `--rule` blocks may share one file, each with its own declared set. `--exempt-suffix` is
  matched as written: `.md` does not exempt `NOTES.MD`.
- **Comments.** Comment lines are exempt, never more than the predecessor gate exempted (a
  differential test holds it). Comment openers depend on the file kind, each a subset of the
  predecessor's six; `--comment-prefix .j2={#` adds one (`*` = every file but PowerShell, whose
  comments are parsed: a `<# #>` block is stripped and the rest scanned; a PowerShell key is exit 2).
- A note line right after a value-taking flag is exit 2, so a value starting with `#` uses
  `--message=#...`. `@file` resolves against the working directory (the checkout root in the
  workflow), not `--root`.
- A file with NUL bytes is also read as UTF-16-LE and -BE, whatever its name.
- There is no `--help`: exit 0 from a gate run comes only from a scan that read files
  (`--selftest` alone writes no receipt), and the workflow asserts the scan receipt
  (`CI_KIT_DECLARED_LITERAL_RECEIPT`: `read=N rules=N findings=N`). The summary line names files
  read, exempt and rules loaded.

Reusable workflow: `declared-literal.yml` (`args` required, normally `@ci/literals.args`).
