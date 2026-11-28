# Documentation gates

Three gates keep prose honest: one asks whether a path a doc names **existed and is gone**, one
asks whether an agent manual's paths **resolve today**, and one re-renders generated facts.

| command | module | question |
|---|---|---|
| `ci-doc-lint` | `ci_kit/doc_lint.py` | did a path this doc names fall from a positive count to zero? |
| `ci-manual-lint` | `ci_kit/manual_lint.py` | does every path the agent manual names resolve in `git ls-files` today? |
| `ci-cog` | `ci_kit/cog_gate.py` | does every cog-generated fact match what its generator renders now? |

## `ci-doc-lint`: a path that existed and is gone

A doc names a path that **existed and is gone**. The obvious rule, "does every backticked path
resolve today", was measured over a live corpus first and reported 971 broken pointers across 140
of 189 files at roughly 90% false positives: HTTP routes, flag lists that contain slashes, prose
like `tier1/tier2/tier3`, real modules named by a short form. A gate that is nine-tenths noise is
muted in a week, and a muted gate is worse than none because its pass still reads as coverage.

So the gate asks Tan, Wagner and Treude's question instead ("Detecting outdated code element
references in software repository documentation", EMSE 2024): **did this path's count in the
source tree go from a positive integer to zero?** Routes, flag lists and prose were never files,
so they never fell from positive to zero, and they go quiet with **no allowlist**. On the same
corpus, 971 findings at ~90% noise became 63 findings that were almost all real. History is HEAD's
ancestry; a shallow clone cannot see it, so the reusable workflow checks out with
`fetch-depth: 0` and a shallow clone is exit 2, not a blind pass.

- **The permanent blind spot.** A path that was never real (a typo, an invented filename) has no
  positive-to-zero transition and is invisible to this rule forever. Write load-bearing pointers
  as real markdown links, which a link checker can verify; `ci-manual-lint` asks the noisier
  question for the one file where it is worth it.
- **Source files too.** The gate also reads source files for `docs/**.md` pointers and
  `.claude/rules/*.md` auto-load triggers. `--code-doc-dir D` adds another doc tree source files
  may point into (default `docs`).
- **The one exemption lives in the file.** A teardown record or dated changelog is correct when
  it names a deleted file. It declares itself with `<!-- doc-rot: historical -->` (plus an
  optional reason), so anyone reading it sees the claim; `--audit` prints every marker so the
  exemption set never grows silently.
- **Shrink-only baseline** (`--baseline` writes it).
- **No built-in exemptions.** `--exempt-prefix P` names a namespace of yours whose spans look like
  paths but are not (a job scheduler's `u/user/flow`, say); nothing is exempt by default.

```bash
ci-doc-lint                        # check every tracked *.md (exit 1 on a finding)
ci-doc-lint --root DIR             # gate DIR instead of the repo holding the working directory
ci-doc-lint --exempt-prefix P      # never treat a span starting with P as a path (repeatable)
ci-doc-lint --code-doc-dir D       # a doc tree source files may point into (repeatable; default docs)
ci-doc-lint --audit                # list the historical-marker exemptions and exit 0
ci-doc-lint --baseline             # write the shrink-only ratchet file
ci-doc-lint --selftest             # prove each rule against text that should trip it
```

Reusable workflow: `doc-refs.yml`. Pre-commit hook: `doc-refs`.

## `ci-manual-lint`: an agent manual's paths resolve today

Every path an agent manual (`CLAUDE.md`) names **resolves today** in `git ls-files`. This is the
question `ci-doc-lint` deliberately cannot answer (a path that was never real), asked only of the
file every agent session loads, where a wrong pointer is followed rather than read.

- Quiet by **shape**, not by allowlist: routes, flags, prose with slashes, image tags and formulas
  are not read as paths.
- A sibling-repo pointer is checked when that checkout is present (`--sibling`) and silent when
  not: a checker that cannot reach a file must not claim it is missing.
- It checks `CLAUDE.md` (`--manual` for another) and nothing else unless asked. Optional checks,
  each a flag: stale migration ranges, a line budget, empty placeholder packages, reference-doc
  dirs plus a freshness stamp, and a total index (`--index docs/README.md`: every doc listed).
- Commit the flags in an args file and run `ci-manual-lint @scripts/manual_lint.args`, so CI and
  a workstation sweep read the same rules.
- It replaced four per-repo copies of the same script that had drifted to 0.46-0.53 similarity.

With no manual to read, it exits 2 (ci-kit's own CI runs that blind direction).

## `ci-cog`: generated facts match their generator

**The `cog` extra** (cogapp). A count or a roster in prose is a copy, and copies rot: one audit
found four docs wrong by 30-160%. Those facts become [cog](https://cog.readthedocs.io/) blocks
rendered by the repo's own generators, and this gate is `cog --check` in-process over every
TRACKED `--suffix` file (default `*.md`, discovered by `git ls-files`) carrying a `--marker` block.

- `--include` is cog's `-I`; `--rerun` names the fix in the STALE message; `--write` regenerates.
- A file whose name only contains the suffix (`x.md.fixture`) is not cogged. The marker is read
  from the bytes, so a file that will not decode cannot hide a block by being skipped.
- Exit 1 = cog's stale. A generator that RAISES, or no tracked file carrying a block, is exit 2.

Moved into the kit in v1.50.0.
