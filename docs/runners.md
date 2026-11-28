# Self-hosted runner capacity (`ci-runners`, `runners` extra)

`ci_kit/runners/` (v1.51.0, `renders` v1.52.0, `supervisor` v1.54.0, scripts v1.56.0, `supervisor.sh` + `--verify-prom` v1.57.0, `runner_impl_info` + `--scripts-sha256` v1.58.0, Python twins `ci_kit.runners.tick` v1.59.0; **the `runners` extra**, `ci-kit[runners]`: pyyaml). Self-hosted GitHub
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
| `scripts_dir` (v1.56.0) | a directory: `--write` copies the supervisor's packaged scripts there (`ci_kit/runners/sh/`: `runs.sh`, the demand reader; `github-auth.sh`, the App-token credential), `--check` flags a missing or drifted copy and any file the package does not ship (the directory is the package's alone). See [The scripts](#the-scripts) |
| `renders.<name>` (v1.52.0) | an extra GENERATED file keyed on the same hash as the JSONs: `output` (relative to `--root`) and a `template` ([`string.Template`](https://docs.python.org/3/library/string.html#template-strings): `${source_sha256}`, `${config}`, `${scripts_sha256}` (v1.56.0); `$$` is a literal `$`). For a consumer of the hash that is not a supervisor, such as an alert rule that holds every installed supervisor's published hash to main's. `--check` holds it to a fresh render; an unknown placeholder or a bare `$` is a malformed kit (exit 2). With `--out-dir` it is written under its file name there, so a shadow run can diff it too |
| `supervisor` + `blocks.<key>.supervisor` (v1.54.0) | a supervisor's own WIRING, so the host script holds no domain constants: the GitHub `owner`, `metric_prefix`, `conf_dir` / `run_dir` / `prom_dir`, the installed `alloc_jq`, the container `labels` (`repo`, `keep`, `canary`), `work_root`, `runner_env`, bounded shared `caches` and an optional pull-through `registry_cache`; per block its `state_dir`, `dockerfile`, `image`, `prom_file`, `busy_limit_s` / `idle_limit_s` / `stray_limit_s`, `network`, the `caches` it mounts and whether it runs the `registry_cache`. A wired block's render gains ONE key, `supervisor`, with every path resolved and `foreign_vmids` (every other block's VM: an instance that finds its own VM there must refuse). A kit without it renders byte-identically to before. Shapes are checked as FULL matches, no looser than the supervisor's own run-time check (absolute paths other than `/`, docker label keys, env names with one-line values, `ip:port`, an https upstream of host, port and path) and two instances may not share a state dir, metrics file or image tag. Full shape: `ci_kit/runners/supervisor.py` |
| `source_hash` | `raw` (default) or `canonical`, below |
| `Kit(rules=...)` (Python) | callables `(cfg, kit) -> [finding]` run after every built-in check passes |

## The CLI

```bash
ci-runners --check --config ci/runners.yml --kit ci/runners-kit.yml   # CI: schema + budgets + fresh renders
ci-runners --write --config ci/runners.yml --kit ci/runners-kit.yml   # regenerate the renders
ci-runners --write ... --out-dir "$RUNNER_TEMP/r"                     # render elsewhere, e.g. to diff against another renderer
ci-runners --alloc < tick.json                                        # one admission verdict (alloc.jq's twin)
ci-runners --verify-prom ci_runner.prom --render ci-runners.json [--scripts-sha256 X]  # every series its alerts read
ci-runners --selftest
```

Exit 0 clean, 1 findings or a stale / orphaned render, 2 could not look (unreadable or unparsable
file, malformed kit, no pyyaml).

`--verify-prom` (v1.57.0) checks a supervisor's file is COMPLETE: it reads one `.prom` file
against the RENDER that supervisor was installed with, so every requirement comes from what shipped.
It does not prove WHEN the file was written (a crashed tick leaves its file): freshness stays with
the installer (a tick that began after the move, ended 0, and wrote a file newer than its begin).
WHICH script wrote it is provable with `--scripts-sha256 <pin>`: `supervisor.sh` publishes
`<prefix>runner_impl_info{scripts_sha256}` hashed from the directory it runs from (the same value
`scripts_sha256` computes), and the flag requires it at the pin; without the flag, the script being
replaced passes too, since it publishes the same series. A file the textfile
collector would reject (an unparsable line or label block, a series twice) is a finding.
Only samples labelled `supervisor="<instance>"` count. Required: `runner_collect_ok 1`, the
render's `source_sha256` in `runner_config_info`, its `max_total`, every gauge an alert or a
per-tick sweep reads, each pool's target / hold / declared peak, the working-set maximum of each pool
with a floor (`min >= 1`; none is a NOTE), the secure conditions exactly on a `secure` render (and
at least one container inspected), and the registry cache's gauges plus the daemon's mirror on a
`socket` render that declares one (the cache off or not answering is a NOTE). Exit 0 / 1 / 2 as
above; a `sealed` render is 2 (`supervisor.sh` does not run it).

The Python API (`ci_kit.runners`): `validate` returns every way the capacity file is wrong;
`render` writes one JSON per block; `extras` renders the kit's `renders`; `freshness` says whether
the committed renders are current;
`allocate` is the admission rule. pyyaml is needed only to READ the YAML files; every function
takes parsed data.

## The scripts

A runner host runs bash, jq and curl, not ci-kit, so the supervisor's shell ships as package data
and is COPIED into the consumer's tree (`scripts_dir`), then installed from there.
`supervisor.sh` (v1.57.0) is the one that RUNS: `supervisor.sh INSTANCE RENDER ISOLATION:PREFIX`
from a systemd oneshot and timer, reading every path, label, limit and cache from its render (the
kit's `supervisor` section) and holding no domain constant of its own. It sources the other two
from beside itself, and sets their inputs from the render (`CI_RUNNER_CONF`, `CI_RUNNER_RUN`,
`CI_RUNNER_METRIC_PREFIX`). Exit 0 looked, 2 could not look; every sample carries
`supervisor="<instance>"`. The other two are SOURCED, never run, and read their inputs from the
environment they are sourced in:

| script | the caller provides | it provides |
|---|---|---|
| `runs.sh` | `OWNER`, `gh_get <path>` (body on stdout) | `open_runs`, `runs_read`, `recent_merge`, `job_demand`, `memo_get`: every non-terminal run, every page up to `RUN_PAGES`, what lies past it counted (`UNREAD`), runs a status filter dropped merged back (`LAG`) |
| `github-auth.sh` | `CI_RUNNER_CONF`, `CI_RUNNER_RUN`, `OWNER`, `CI_RUNNER_METRIC_PREFIX`, optional `CI_RUNNER_AUTH_HINT`, an `emit` function | `gh_auth` (a GitHub App installation token, cached and minted under flock; the PAT on any failure, LOUDLY), `gh_auth_fresh`, `gh_auth_emit` (`<prefix>github_auth{mode}`, `<prefix>github_app_configured`, the token's expiry) |

`scripts_sha256` (`ci_kit.runners.scripts`) names the set: sha256 over the `sha256sum` manifest
of every script sorted by name, 12 hex characters. A host recomputes it from the installed copies
with `LC_ALL=C sha256sum -t -- <names, sorted> | sha256sum | cut -c1-12`, so a supervisor can publish
what it runs and a `renders` template holds that to what the kit pins.

The scripts ship mode 644, and that is deliberate: the package copy is DATA (`scripts()` reads its
bytes), `write_scripts` copies bytes, so the consumer's copy takes its own umask, and the mode a host
runs with is the installer's to set -- `chmod 755` where a unit's ExecStart runs `supervisor.sh`
directly, read-only where a consumer's shim SOURCES it. A mode is not in `scripts_sha256` (bytes
only), so it is never drift either way.

### Python twins (`ci_kit.runners.tick`, v1.59.0)

The tick's PURE decisions have Python twins, for replaying a trace or testing a policy without a
host: `carry_head` (the drain head carried over a blind run, bounded), `young_table` /
`young_charge` / `young_of` (the room young runners will still take), `since_start`, `refill_more`
(does the refill window stretch), `redecide_entry` / `redecide_state` / `redecide_live_free` (what a
refill-pass re-decide hands the allocator), `secure_violations`, `registry_probe_lines`,
`stray_age` and `job_demand`. The bash is the implementation and the twins are pinned to it:
`tests/test_runners_tick_parity.py` cuts each function out of the shipped `supervisor.sh` (or
sources `runs.sh`) and runs it in real bash and jq under the supervisor's `set -uo pipefail`, on
hand-built cases for every branch plus a seeded random matrix, and requires the same output. jq's
semantics are kept where they decide a case: only null and false are false, `//` takes the right
side for both, and `>` orders across types (a string `Memory` is "> 0").

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
