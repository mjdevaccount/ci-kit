# Self-hosted runner capacity (`ci-runners`, `runners` extra)

`ci_kit/runners/` (v1.51.0, `renders` v1.52.0, `supervisor` v1.54.0; **the `runners` extra**, `ci-kit[runners]`: pyyaml). Self-hosted GitHub
runner CAPACITY as a library: the capacity file's schema, the host / guest / runner-VM budget
invariants (measured `peak_mb` against real RAM, floors, ceilings, the live room), one JSON render
per runner block for a supervisor to read with jq, and the runtime admission rule (`alloc.jq`,
shipped as package data, plus its Python twin `allocate()`, pinned equal by a parity test).

It sizes self-hosted GitHub Actions runners that run as ephemeral containers inside runner VMs on
one host (Proxmox or similar), shared by per-repo POOLS. The kit owns the mechanism; a domain
supplies two files.

## The capacity file (`runners.yml`)

- `host`: threads, RAM, `cpu_overcommit`, `reserves_mb`.
- `guests`: vmid, cores, RAM; `state: running|stopped|on_demand`, `planned`, `budgeted: false`,
  `shares: <reserve>`.
- one block per runner tier: `vm`, `max_total`, overcommits, `vm_reserve_mb`, `mem_book_ratio` +
  `live_floor_mb`, `backfill`, `redecide`, `defaults`, `pools`.

Each pool has `min_idle`, optional `max_runners` (absent = derived from admission), a MEASURED
`peak_mb`, optional `young_mb` / `starve_s`, and, for a second pool of one repo by job class,
`repo` + its own `labels` + its own `variable`.

## The kit (`runners-kit.yml`)

What the domain decides once. These are its extension points:

| kit key | what it extends |
|---|---|
| `blocks.<key>` | a runner block the capacity file may hold: `instance`, `prefix` (unique, or two supervisors reap each other's runners), `isolation` (passed to the supervisor), `label` (the trust tier: a pool carries `label` or `label-<class>` and no other block's), `variable` (the `runs-on` kill switch), `output`, `required`, `one_job_per_boot` (max_total 1 on an `on_demand` VM) |
| `share_duties.<reserve>` | a guest that `shares` a host reserve runs inside it, so the reserve's other owner must yield: `--check` requires `doc` to carry `text` |
| `out_dir`, `generated`, `write_command` | where renders go (relative to `--root`), the banner they carry, the command a stale finding names |
| `alloc_jq` | a path: `--write` copies ci-kit's `alloc.jq` there, `--check` flags a copy that drifted |
| `renders.<name>` (v1.52.0) | an extra GENERATED file keyed on the same hash as the JSONs: `output` (relative to `--root`) and a `template` ([`string.Template`](https://docs.python.org/3/library/string.html#template-strings): `${source_sha256}`, `${config}`; `$$` is a literal `$`). For a consumer of the hash that is not a supervisor, such as an alert rule that holds every installed supervisor's published hash to main's. `--check` holds it to a fresh render; an unknown placeholder or a bare `$` is a malformed kit (exit 2). With `--out-dir` it is written under its file name there, so a shadow run can diff it too |
| `supervisor` + `blocks.<key>.supervisor` (v1.54.0) | a supervisor's own WIRING, so the host script holds no domain constants: the GitHub `owner`, `metric_prefix`, `conf_dir` / `run_dir` / `prom_dir`, the installed `alloc_jq`, the container `labels` (`repo`, `keep`, `canary`), `work_root`, `runner_env`, bounded shared `caches` and an optional pull-through `registry_cache`; per block its `state_dir`, `dockerfile`, `image`, `prom_file`, `busy_limit_s` / `idle_limit_s` / `stray_limit_s`, `network`, the `caches` it mounts and whether it runs the `registry_cache`. A wired block's render gains ONE key, `supervisor`, with every path resolved and `foreign_vmids` (every other block's VM: an instance that finds its own VM there must refuse). A kit without it renders byte-identically to before. Shapes are checked (absolute paths, docker label keys, env names, `host:port`, https upstream) and two instances may not share a state dir, metrics file or image tag. Full shape: `ci_kit/runners/supervisor.py` |
| `source_hash` | `raw` (default) or `canonical`, below |
| `Kit(rules=...)` (Python) | callables `(cfg, kit) -> [finding]` run after every built-in check passes |

## The CLI

```bash
ci-runners --check --config ci/runners.yml --kit ci/runners-kit.yml   # CI: schema + budgets + fresh renders
ci-runners --write --config ci/runners.yml --kit ci/runners-kit.yml   # regenerate the renders
ci-runners --write ... --out-dir "$RUNNER_TEMP/r"                     # render elsewhere, e.g. to diff against another renderer
ci-runners --alloc < tick.json                                        # one admission verdict (alloc.jq's twin)
ci-runners --selftest
```

Exit 0 clean, 1 findings or a stale / orphaned render, 2 could not look (unreadable or unparsable
file, malformed kit, no pyyaml).

The Python API (`ci_kit.runners`): `validate` returns every way the capacity file is wrong;
`render` writes one JSON per block; `extras` renders the kit's `renders`; `freshness` says whether
the committed renders are current;
`allocate` is the admission rule. pyyaml is needed only to READ the YAML files; every function
takes parsed data.

## Admission

`alloc.jq`, run by the supervisor with no Python: a start needs a free slot AND its pool's
`peak_mb` free inside `mem_budget_mb`, capped each tick by the VM's live MemAvailable less
`live_floor_mb`. A pool that does not fit is held, and after `starve_s` it reserves its room and
the run drains for it, with bounded backfill.

## `source_sha256` and the reinstall it causes

A supervisor reinstalls whenever its render's `source_sha256` changes. In `raw` mode (the default,
and what existing renders carry) the hash covers the capacity file's TEXT, so a comment-only edit
reinstalls every supervisor. `canonical` hashes the parsed data instead, so comments and formatting
reinstall nothing. It is opt-in and OFF by default because switching modes changes every render's
hash once: turning it on is ONE deliberate reinstall of every supervisor, done when the operator
chooses, not as a side effect of upgrading ci-kit.

## Proof

The golden test (`tests/test_runners_must_fire.py`) holds the render BYTE-IDENTICAL to the
hand-rolled renderer this replaced, on a fixture rendered by that renderer.
