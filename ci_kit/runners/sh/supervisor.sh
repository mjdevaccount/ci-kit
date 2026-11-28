#!/usr/bin/env bash
# supervisor.sh -- ci-kit runners: the supervisor tick for a self-hosted runner pool set on a
# Proxmox host. Ships as ci-kit package data (ci_kit/runners/sh/); `ci-runners --write` copies it
# into a kit's scripts_dir. Run by a systemd oneshot+timer as `supervisor.sh INSTANCE RENDER
# ISOLATION:PREFIX` (a full tick, then a 45s refill loop, restarted 5s after it ends); it sources
# github-auth.sh and runs.sh from beside itself.
#
# Keeps each repo's pool of ephemeral GitHub Actions runners alive inside a runner VM, sized by ONE
# shared slot budget (the render's max_total, decided by the allocator jq). Each one is a
# container started with a single-use JIT config (POST .../generate-jitconfig) through the QEMU
# guest agent: the GitHub token never enters the VM (jobs there can be root via the Docker
# socket), and there is no network path from the VM to this host at all. A runner takes ONE job,
# exits, and its container is --rm'd; the next tick replaces it.
#
# Every tick writes a .prom file to node-exporter's textfile dir, which an outside Prometheus
# scrapes. A tick that could not look writes <prefix>runner_collect_ok 0; a tick that never runs
# leaves the file to age, and an alert on the file's mtime (node-exporter's textfile mtime) pages.
#
# Exit 0 = looked (whatever it found); 2 = could not look (no token, agent down, API down).
#
# TWO INSTANCES, ONE SCRIPT: the installer writes one unit per instance with the three arguments;
# every path / label / limit is the render's.
#   socket instance -- the socket VM; jobs get the VM's docker socket (so no secrets there).
#   secure instance -- the secure VM; token-only jobs, NO socket, the run_secure shape below.
# Everything an instance writes is its own: host state, ETag cache, lock, .prom file. Every runner
# filter keys on the instance's name PREFIX (one per instance): two instances on one prefix would
# delete each other's runners mid-registration and mix their busy counts. Every metric
# carries supervisor="<instance>": node-exporter merges both .prom files into one scrape,
# and two files writing the same unlabelled series is a duplicate that fails the whole collector.
set -uo pipefail

# usage: supervisor.sh INSTANCE RENDER ISOLATION:PREFIX
# THE RENDER IS THE ONLY SOURCE. Pools, labels, the VM id AND every path, container label, reap
# limit and cache come from ONE file per instance: the kit's runners config, rendered (its
# `supervisor` section) and installed by the installer, which also writes the unit's three
# arguments. This script holds no domain constant of its own. ISOLATION:PREFIX rides the INSTALLED
# UNIT, never the file alone: a socket render installed under the secure instance would otherwise
# start socket runners for the token-holding jobs. A render that cannot be read leaves no .prom
# path to report on (it is IN the render): exit 2 unpublished, and the file's stopped mtime is
# what the staleness alert pages.
INSTANCE="${1:-}"
CONFIG="${2:-}"
EXPECT="${3:-}"
sv() { jq -er ".supervisor.$1" "${CONFIG}" 2>/dev/null; }
if ! { [ -n "${INSTANCE}" ] && [ -n "${EXPECT}" ] && [ -r "${CONFIG}" ] \
       && CONF="$(sv conf_dir)" && OWNER="$(sv owner)" && MP="$(sv metric_prefix)" \
       && STATE="$(sv state_dir)" && RUN="$(sv run_dir)" && OUT="$(sv prom_file)" \
       && DOCKERFILE="$(sv dockerfile)" && IMAGE="$(sv image)" && ALLOC="$(sv alloc_jq)" \
       && WORK_ROOT="$(sv work_root)" && BUSY_LIMIT_S="$(sv busy_limit_s)" \
       && IDLE_LIMIT_S="$(sv idle_limit_s)" && STRAY_LIMIT_S="$(sv stray_limit_s)" \
       && L_REPO="$(sv labels.repo)" && L_KEEP="$(sv labels.keep)" && L_CANARY="$(sv labels.canary)"; }; then
  echo "usage: ci-runner-supervisor.sh INSTANCE RENDER ISOLATION:PREFIX -- ${CONFIG:-<no render>} is unreadable or has no complete supervisor section" >&2
  exit 2
fi
# IDENTITY BEFORE ANY PATH IS USED. Every path above is the render's, so a render placed under
# the wrong instance would take the other instance's lock and write its .prom before a later
# check refused it. The render must say it is THIS instance with the unit's isolation:prefix,
# and its run dir must be this instance's -- checked here, where a refusal touches nothing.
if [ "$(jq -r '"\(.instance):\(.isolation):\(.prefix)"' "${CONFIG}" 2>/dev/null)" != "${INSTANCE}:${EXPECT}" ] \
   || [ "${RUN##*/}" != "${INSTANCE}" ]; then
  echo "ci-runner-supervisor: ${CONFIG} is not instance ${INSTANCE} with ${EXPECT} (or its run_dir is not ${INSTANCE}'s) -- refused before touching anything" >&2
  exit 2
fi
# THE REST OF THE SECTION, SHAPED: every list the script expands and every value that reaches a
# guest script, held to a strict pattern (\A..\z: a trailing newline is not a match).
# A render that fails here exits 2 unpublished, like an unreadable one.
_P='\A/[A-Za-z0-9._/-]+\z'; _N='\A[A-Za-z0-9][A-Za-z0-9._-]*\z'; _E='\A[A-Za-z_][A-Za-z0-9_]*\z'
if ! jq -e --arg p "${_P}" --arg n "${_N}" --arg e "${_E}" '.supervisor
     | (.runner_env | type == "array" and all(.name | test($e)) and all(.value | type == "string" and test("\\A[^\\n]*\\z")))
     and (.caches | type == "array" and all(.dir | test($p)) and all(.name | test($n)) and all(.env | test($e))
                  and all(.max_bytes | type == "number" and . > 0 and . == floor) and all(.set | type == "array" and all(.name | test($e)) and all(.value | type == "string" and test("\\A[^\\n]*\\z"))))
     and ((.registry_cache // null) == null or (.registry_cache
          | (.image | test("\\A[A-Za-z0-9._/:@-]+\\z")) and (.name | test($n)) and (.dir | test($p))
            and (.addr | test("\\A[0-9.]+:[0-9]+\\z")) and (.debug_addr | test("\\A[0-9.]+:[0-9]+\\z"))
            and (.upstream | test("\\Ahttps://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._/-]*)?\\z"))
            and (.ttl | test("\\A[0-9]+[smh]\\z")) and (.max_bytes | type == "number" and . > 0 and . == floor)
            and (.log_environment | test($n))))' "${CONFIG}" >/dev/null 2>&1; then
  echo "ci-runner-supervisor: ${CONFIG}: supervisor.runner_env / caches / registry_cache malformed -- refused" >&2
  exit 2
fi
# the docker network secure runners join (absent for socket)
SECURE_NET="$(jq -r '.supervisor.network // empty' "${CONFIG}")"
# every runner's env, in the render's order: -e NAME=VALUE ...
mapfile -d '' RUNNER_ENV < <(jq -j '.supervisor.runner_env[] | ("-e", "\(.name)=\(.value)") | . + "\u0000"' "${CONFIG}")
# the shared caches a socket runner mounts, in the render's order: -v DIR:DIR -e ENV=DIR [-e K=V ...]
mapfile -d '' CACHE_ARGS < <(jq -j '.supervisor.caches[] | ("-v", "\(.dir):\(.dir)", "-e", "\(.env)=\(.dir)", (.set[] | "-e", "\(.name)=\(.value)")) | . + "\u0000"' "${CONFIG}")
mapfile -t CACHE_ROWS < <(jq -r '.supervisor.caches[] | "\(.dir) \(.max_bytes) \(.name)"' "${CONFIG}")
mapfile -t CACHE_DIRS < <(jq -r '.supervisor.caches[].dir' "${CONFIG}")
# the credential helper (github-auth.sh, sourced below) reads the same host paths: these are its input names
export CI_RUNNER_CONF="${CONF}" CI_RUNNER_RUN="${RUN%/"${INSTANCE}"}" CI_RUNNER_METRIC_PREFIX="${MP}"
API=https://api.github.com
# Each runner's work folder lives at the SAME absolute path in the VM and in its container. Jobs
# drive the VM's daemon through the socket, so a `docker run -v $GITHUB_WORKSPACE/x:...` is
# resolved by the VM, not the runner: under the image's default _work the VM saw no such path,
# made empty dirs, and a job's bind-mounted config arrived empty (a service started with no config).
# ONE uv cache for every runner, kept across jobs. GitHub's Actions cache missed on nearly every
# self-hosted job (PR-scoped keys, and main rarely saves when its jobs are skipped), so each
# job downloaded every wheel, then spent 15-21s uploading ~500 MB that nothing read. Workflows
# turn setup-uv's cache off when the pool's runs-on variable is set; UV_CACHE_DIR here wins over
# setup-uv's default. uv locks its cache, so concurrent jobs are safe. copy, not hardlink: the cache
# and the venv are different bind mounts, and a hardlink across mounts fails (uv warns, then copies).
# Bounded below: wiped past its max_bytes while no runner is busy. TRUST: any job can write it,
# so a job can poison a later job's wheels -- the same exposure the VM's docker image store has
# already (a job can retag postgres:*); nothing that runs here holds a secret.
# ci-kit's pinned release binaries (jscpd, oasdiff), the same way. The runner's
# ~/.cache/ci-kit dies with its --rm container, so EVERY dup-code / api-diff job fetched from GitHub
# releases, and a releases 504 turned those jobs red. ci-kit's release_bin.py
# writes atomically, so concurrent jobs share it safely. A pin is ~tens of MB and a bump is
# a new directory; same wipe rule as uv. TRUST as above, plus: the sha256 is checked at FETCH, not on
# a cached read, so a job could swap a later job's binary -- the same exposure as a wheel here.
# terraform's providers, the same way. A `terraform` job ran `terraform init`
# cold every time, and a cold init fetches the provider's SHA256SUMS, .sig and zip from github.com
# releases -- a releases 504 failed it ("failed to retrieve authentication checksums"). Measured
# (TF_LOG=TRACE, a committed .terraform.lock.hcl, a seeded TF_PLUGIN_CACHE_DIR, github.com releases
# 504ing at the time): a warm init contacts registry.terraform.io only and prints "Using
# <provider> ... from the shared cache directory". TRUST: terraform (>= 1.4) uses a cached
# provider ONLY when its h1 hash is in the committed lock file, so a poisoned entry is refused,
# not run -- stronger than the two caches above. Terraform does not lock this directory: two
# COLD inits of one version at once can race; a torn entry fails the lock-hash match, and the job
# fetches again. A provider is ~30 MB unpacked; same wipe rule.
# DOCKER HUB PULL-THROUGH CACHE (socket VM only): CNCF distribution in proxy mode, a
# container on the VM's daemon, and that daemon's `registry-mirrors` pointing at it. Every Docker Hub
# pull a job makes (docker run, services:, testcontainers, compose) asks it first. Why: a job died on
# `Get registry-1.docker.io/v2/<image>/manifests/sha256:...: TLS handshake timeout` (rc 125, exit 2),
# a digest-pinned image the VM did not hold -- a nightly
# `image prune -af --filter until=168h` filters on the image's BUILD date, so every unused upstream
# pin older than a week is gone each morning and the next job re-pulls it from Docker Hub live.
# Measured in the socket VM: a cached digest serves (manifests + blobs, sha256 matching) with the
# cache's own tcp/443 cut; a mirror that refuses makes dockerd log "Attempting next endpoint" and pull
# from Docker Hub (FAIL-OPEN, so a dead cache only loses the protection -- an alert on
# <prefix>registry_cache_up says so).
# Image from GHCR, not Docker Hub: starting the cache must not need what it protects against.
# TRUST: no more than the image store beside it -- root in the VM can stop it and serve its own
# tags on this port, just as it can `docker tag` over postgres:16 today. A digest pull is verified
# by dockerd whatever the mirror serves, and the runner image's base is GHCR (no mirror applies).
# Not in the secure VM (no socket: its jobs pull nothing; a second container there is a foreign one)
# nor a sealed VM (rolled back to a clean snapshot every job: a cache inside it starts cold each
# time, and one outside it would be a LAN hole for a root, secret-holding job).
# From the render's supervisor.registry_cache (absent = no cache: every REG_* is empty and
# registry_tick never runs). addr is loopback on host networking: runners (--network host) and
# dockerd only; debug_addr is its Prometheus /metrics (registry_proxy_*), read by registry_tick.
# NUL-separated: a value cannot shift its neighbours; shapes are checked above.
mapfile -d '' _REG < <(jq -j '.supervisor.registry_cache // empty
  | (.image, .name, .addr, .debug_addr, .dir, .upstream, .ttl, .max_bytes, .log_environment) | tostring + "\u0000"' "${CONFIG}")
REG_IMAGE="${_REG[0]:-}" REG_NAME="${_REG[1]:-}" REG_ADDR="${_REG[2]:-}" REG_DEBUG="${_REG[3]:-}" REG_CACHE="${_REG[4]:-}"
REG_UPSTREAM="${_REG[5]:-}" REG_TTL="${_REG[6]:-}" REG_CACHE_MAX_B="${_REG[7]:-}" REG_LOGENV="${_REG[8]:-}"
REG_MIRROR="http://${REG_ADDR}"
# The TTL (e.g. 336h): a blob is refetched that long after it was FIRST pulled, used or not
# (distribution's TTL scheduler does not refresh on a hit). Digests are immutable, so the TTL bounds
# disk, not freshness; a TAG is re-resolved upstream on every pull while upstream answers, and from
# the cache's last copy when it does not (<prefix>registry_upstream_ok). The size cap below is the hard bound.

metrics=()
ok=1
emit() { metrics+=("$1"); }
# THE CREDENTIAL: github-auth.sh, the ONE helper every supervisor sources -- the GitHub App's
# installation token (its own rate bucket), else the PAT at ${CONF}/github.token, loudly. Not
# sourceable (an install that lost it) = the PAT exactly as before, and the auth gauges say pat.
TOKEN=""
# shellcheck source=github-auth.sh
if . "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/github-auth.sh"; then
  gh_auth || :
  gh_auth_emit
else
  echo "ci-runner-supervisor: cannot source github-auth.sh beside $(readlink -f "${BASH_SOURCE[0]}") -- using the PAT" >&2
  TOKEN="$(cat "${CONF}/github.token" 2>/dev/null)"
  gh_auth_fresh() { :; }
  emit "${MP}github_auth{mode=\"app\"} 0"; emit "${MP}github_auth{mode=\"pat\"} $([ -n "${TOKEN}" ] && echo 1 || echo 0)"
  # configured from disk: a lost helper beside a placed App key is what the PAT-fallback alert pages
  emit "${MP}github_app_configured $([ -e "${CONF}/github-app.pem" ] && [ -e "${CONF}/github-app.id" ] && echo 1 || echo 0)"
fi
# Every value read out of a VM is untrusted text headed for a .prom line: a newline in it would
# inject a sample. Numbers only, or nothing is emitted.
isnum() { [[ "$1" =~ ^[0-9]+$ ]]; }
# MEMPEAK[repo] = the largest runner working set (list_runners) sampled for that repo this run.
declare -A MEMPEAK=()
# SIBPEAK = the largest sibling-container working-set SUM sampled this run (list_runners' "@sib").
SIBPEAK=""
# secure only: runners run_secure started and then removed because their own inspect was off shape.
# The tick goes on (that start counts as failed), so without this gauge nothing outside saw it.
START_VIOLATIONS=0

publish() {
  local tmp="${OUT}.$$"
  # every sample line gains supervisor="<instance>" here, once, so no emit can forget it
  {
    echo "# HELP ${MP}runner_collect_ok 1 = the supervisor looked at every pool this tick."
    echo "# TYPE ${MP}runner_collect_ok gauge"
    echo "${MP}runner_collect_ok ${ok}"
    printf '%s\n' "${metrics[@]}"
    for r in "${!MEMPEAK[@]}"; do echo "${MP}runner_mem_working_set_max_bytes{repo=\"${r}\"} ${MEMPEAK[${r}]}"; done
    if [ -n "${SIBPEAK}" ]; then echo "${MP}runner_vm_sibling_working_set_max_bytes ${SIBPEAK}"; fi
    # this tick's conditional GETs (gh_get): charged (200/other) vs free (304), and the LOWEST
    # remaining any response reported -- absent when no response carried one. An if, not `&&`:
    # a false test as the block's LAST command made the group exit 1, so the file never moved.
    # retries: attempts CURL_RETRY spent on this tick's GETs. A GET that succeeds on its second try
    # logs 200 like any other, so without this a worsening link reads green (an alert on the retries).
    if [ -r "${API_LOG:-}" ]; then
      awk -v p="${MP}" '{ n[$1 == "304" ? "not_modified" : ($1 == "200" ? "ok" : "error")]++ }
        $2 ~ /^[0-9]+$/ && (min == "" || $2 + 0 < min) { min = $2 + 0 }
        $3 ~ /^[0-9]+$/ { retries += $3 }
        $4 ~ /^[0-9]+$/ { lim = $4 + 0 }
        END { for (k in n) printf "%sgithub_requests{result=\"%s\"} %d\n", p, k, n[k]
              printf "%sgithub_request_retries %d\n", p, retries
              if (min != "") printf "%sgithub_ratelimit_remaining %d\n", p, min
              if (lim != "") printf "%sgithub_ratelimit_limit %d\n", p, lim }' "${API_LOG}"
    fi
    if [ "${ISOLATION:-}" = secure ]; then echo "${MP}runner_isolation_start_violations ${START_VIOLATIONS}"; fi
    true
  } | awk -v s="${INSTANCE}" '/^#/ || NF == 0 { print; next }
      { b = index($0, "{"); sp = index($0, " ")
        if (b && b < sp) print substr($0, 1, b) "supervisor=\"" s "\"," substr($0, b + 1)
        else print substr($0, 1, sp - 1) "{supervisor=\"" s "\"}" substr($0, sp) }' > "${tmp}" \
    && mv -f "${tmp}" "${OUT}"
}

# guest <timeout> <argv...> -> stdout of the command in the VM; nonzero if the agent or the
# command failed. qm prints {"exitcode":N,"out-data":"..."}.
guest() {
  local t="$1"; shift
  local js
  js="$(qm guest exec "${VMID}" --timeout "${t}" -- "$@" 2>/dev/null)" || return 2
  # 2>/dev/null: stop_sampler kills the sampler mid list_runners, and this write into its dead
  # $(...) pipe was 'printf: write error: Broken pipe' (hundreds a day, benign: the reader is gone)
  printf '%s' "$(jq -r '."out-data" // ""' <<<"${js}")" 2>/dev/null
  [ "$(jq -r '.exitcode // 1' <<<"${js}")" = 0 ]
}

# The bearer rides a process substitution, never curl's argv: argv is in the Proxmox host's `ps`
# (a secret-on-argv lint checks it).
# CONNECT TIMEOUT: dozens of ticks a day died on `curl: (28) Connection timed out after
# 20002 ms` -- a lost SYN to api.github.com (IPv4 only; the same host connects in ~45 ms; spread
# over every hour), and that tick read the pool blind. A connect gets 5s. A GET retries twice (it
# is idempotent, and gh_get re-reads on any failure anyway); a write never retries: a POST whose
# reply was lost may already have registered a runner.
CURL_CONNECT=(--connect-timeout 5)
CURL_RETRY=(--retry 2 --retry-delay 1 --retry-max-time 30)
gh_api() { # method path [json]
  gh_auth_fresh  # a degraded run can outlive a token re-used with 10 min left
  curl -fsS "${CURL_CONNECT[@]}" --max-time 20 -X "$1" -H @<(printf 'Authorization: Bearer %s\n' "${TOKEN}") \
    -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" \
    ${3:+-d "$3"} "${API}$2"
}

# THE RATE LIMIT IS THE SCALING LIMIT. The PAT gets 5,000 requests/h, and each pool
# costs 3 list reads a full tick (runners, queued runs, in-progress runs) plus one per active run:
# 2 pools were ~400/h, a dozen pools read unconditionally would be ~3,000-4,000/h -- the tick blind
# by the half hour. So every poll is a CONDITIONAL GET: the last ETag rides If-None-Match, and a 304
# (nothing changed -- a quiet repo, i.e. most of them) answers from the cache below and is NOT
# charged against the limit (measured: remaining unchanged across three 304s). The
# cache is tmpfs: a reboot just costs one full read per URL. Every response's
# X-RateLimit-Remaining and status lands in API_LOG, which publish() turns into
# <prefix>github_ratelimit_remaining (an alert reads it low) and the per-tick request counters --
# command substitutions are subshells, so a file, not a variable, carries them out.
ETAG_DIR="${RUN}/etag"
API_LOG="${ETAG_DIR}/tick.log"
# ONE supervisor at a time: the timer never overlaps itself, but a hand run beside it would share
# the ETag cache and API_LOG and double-start runners for the same slots. A held lock exits 0 (the
# holder is looking); a holder that hangs stops the file's mtime, which the staleness alert reads.
if ! mkdir -p "${ETAG_DIR}" || ! (umask 077 && mkdir -p "${STATE}"); then
  echo "ci-runner-supervisor: cannot create ${ETAG_DIR} or ${STATE}" >&2; ok=0; publish; exit 2
fi
if ! exec 9>"${RUN}/supervisor.lock"; then
  echo "ci-runner-supervisor: cannot open ${RUN}/supervisor.lock" >&2; ok=0; publish; exit 2
fi
flock -n 9 || { echo "ci-runner-supervisor: another run holds the lock; leaving it be" >&2; exit 0; }
: > "${API_LOG}"
# Per-run jobs URLs are new every run: drop entries no tick has read for 2h (a 304 touches its
# body), so the tmpfs cache holds the working set, not every run since boot.
find "${ETAG_DIR}" -maxdepth 1 -type f ! -name tick.log -mmin +120 -delete 2>/dev/null || true
gh_get() { # path -> body on stdout; nonzero = could not read
  local key d code etag rem lim
  local -a cond=()
  gh_auth_fresh  # in a $( ): a re-mint lands in the shared cache, the next call reads it back
  key="$(printf '%s' "$1" | sha1sum | cut -c1-16)"; d="${ETAG_DIR}/${key}"
  etag="$(cat "${d}.etag" 2>/dev/null)"
  [ -n "${etag}" ] && [ -s "${d}.body" ] && cond=(-H "If-None-Match: ${etag}")
  # a curl that fails before headers must not leave the LAST call's headers to be read as this one's
  rm -f "${d}.hdr" "${d}.new"
  code="$(curl -sS "${CURL_CONNECT[@]}" "${CURL_RETRY[@]}" --max-time 20 -o "${d}.new" -D "${d}.hdr" -w '%{http_code} %{num_retries}' \
    -H @<(printf 'Authorization: Bearer %s\n' "${TOKEN}") "${cond[@]}" \
    -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" "${API}$1")" || :
  [ -n "${code}" ] || code="000 0"  # -w prints "000 <retries>" on a failed transfer: keep its count
  tries="${code#* }"; code="${code%% *}"; [ "${tries}" = "${code}" ] && tries=0
  # the LAST response's value: -D appends a retried attempt's headers (CURL_RETRY) above the final
  # one's, so each status line resets it -- a failed attempt's ETag is never saved as the 200's
  rem="$(tr -d '\r' < "${d}.hdr" 2>/dev/null | awk -F': ' '/^HTTP\// { v = "" } tolower($1) == "x-ratelimit-remaining" { v = $2 } END { print v }')"
  # and the bucket's SIZE: 5000 = the PAT, the App's installation is larger -- which bucket the
  # remaining is counted against (github-auth.sh), read from GitHub's own answer
  lim="$(tr -d '\r' < "${d}.hdr" 2>/dev/null | awk -F': ' '/^HTTP\// { v = "" } tolower($1) == "x-ratelimit-limit" { v = $2 } END { print v }')"
  echo "${code} ${rem:--} ${tries} ${lim:--}" >> "${API_LOG}"
  case "${code}" in
    304) touch "${d}.body" "${d}.etag"; cat "${d}.body" ;;
    200) tr -d '\r' < "${d}.hdr" | awk -F': ' '/^HTTP\// { v = "" } tolower($1) == "etag" { v = $2 } END { print v }' > "${d}.etag"
         mv -f "${d}.new" "${d}.body" && cat "${d}.body" ;;
    *)   rm -f "${d}.etag"; return 1 ;;
  esac
}

# DEMAND (which runs are open, which of their jobs a pool can serve): runs.sh, the ONE reader
# this script and the sealed supervisor share -- memo_get, runs_read, recent_merge, open_runs,
# job_demand. Installed beside this script by the installer.
# shellcheck source=runs.sh
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/runs.sh" \
  || { echo "ci-runner-supervisor: cannot source runs.sh beside $(readlink -f "${BASH_SOURCE[0]}")" >&2; ok=0; publish; exit 2; }

# docker_bytes "3.671GB" -> 3671000000. `docker ... prune` prints go-units HumanSize: DECIMAL
# (kB = 1000), one unit of B kB MB GB TB PB. Unknown text counts 0, never a crash of the tick.
docker_bytes() {
  awk -v s="$1" 'BEGIN { n = 0; u = "B"; if (match(s, /^[0-9.]+/)) { n = substr(s, 1, RLENGTH); u = substr(s, RLENGTH + 1) }
    m["B"] = 1; m["kB"] = 1e3; m["MB"] = 1e6; m["GB"] = 1e9; m["TB"] = 1e12; m["PB"] = 1e15
    printf "%d", n * (u in m ? m[u] : 0) }'
}

# ONE `docker ps` FOR EVERY POOL. Each guest exec is ~0.85s over the agent, and a
# listing per pool was ~15s a tick with a dozen pools -- again every 3s in the refill loop. One call
# lists every runner as "repo name working-set" and the pools are partitioned from it here.
# WORKING SET, NOT memory.peak: the runner container's cgroup v2 memory.current less
# memory.stat's inactive_file -- kubelet's container_memory_working_set_bytes, the number the
# kernel cannot drop under pressure (systemd driver: the docker-<id>.scope under system.slice;
# "-" when unreadable). memory.peak counts page cache: with a dozen pools live it read 8-10 GB for
# single runners while the whole VM had ~2 GB used and most of its memory in buff/cache, so the
# peak-over-declared alert fired on half the pools for a cost the kernel reclaims (one runner:
# memory.current ~7.7 GB, nearly all of it inactive file cache -> working set ~1 GB).
# Still not a bound on the JOB: sibling containers it starts on the VM's daemon (testcontainers,
# compose) sit outside the runner's cgroup. Sampled every refill pass (3s) and kept as a max.
# THE SIBLINGS RIDE THE SAME 3s SAMPLE. The listing also sums the working set of
# every container WITHOUT the repo label (buildx_buildkit_*, testcontainers, compose) onto
# one trailing "@sib <bytes>" line; runners_of() strips it before anything counts runners, and
# note_sib() keeps its max as SIBPEAK. The once-a-tick read below (<prefix>runner_vm_sibling_
# working_set_bytes) was BLIND to the one sibling that matters: an image build's buildkit
# container lived ~50s at 3-4 GB working set by hand, and the gauge read 20 MB.
# The label goes through an "x" prefix: an unlabelled container's empty label would otherwise
# collapse under `read` and shift its name into the repo field.
# shellcheck disable=SC2016  # expands in the guest's bash, not here
list_runners() {
  # pipefail: without it the pipeline's status is the while-loop's (0), so a failed `docker ps`
  # read as "no runners anywhere" and every pool was refilled on top of its live runners.
  guest 15 bash -c 'set -o pipefail; docker ps --no-trunc --format "{{.ID}} x{{.Label \"'"${L_REPO}"'\"}} {{.Names}}" | { s=0; while read -r id repo name; do repo=${repo#x}; d="/sys/fs/cgroup/system.slice/docker-${id}.scope"; ws=-; if cur=$(cat "${d}/memory.current" 2>/dev/null); then ia=; while read -r k v; do [ "${k}" = inactive_file ] && ia=${v}; done 2>/dev/null <"${d}/memory.stat"; [ -n "${ia}" ] && ws=$((cur - ia)); fi; if [ -n "${repo}" ]; then echo "${repo} ${name} ${ws}"; elif [ "${ws}" != - ]; then s=$((s + ws)); fi; done; echo "@sib ${s}"; }'
}
# The trailer is found by POSITION (the last line), never by content: a job is root in the VM and
# writes container labels, so a runner row reading "@sib 0" could otherwise fake the sum and drop
# itself out of the runner count.
runners_of() { sed '$d' <<<"$1"; } # a listing less its sibling line
# Fold a listing's sibling sum into SIBPEAK (numbers only: it is read out of the VM). A measured 0
# is a value -- an idle VM must publish 0, not nothing, or "unmeasured" and "zero" look alike.
note_sib() {
  local tag s
  read -r tag s <<<"$(tail -n1 <<<"$1")"
  [ "${tag}" = "@sib" ] && isnum "${s}" || return 0
  if [ -z "${SIBPEAK}" ] || [ "${s}" -gt "${SIBPEAK}" ]; then SIBPEAK=${s}; fi
  return 0
}
names_of() { awk -v r="$1" '$1 == r { print $2 }' <<<"$2"; } # repo listing -> its runner names
young_waivable() { # runners_json prefix job_runner_names -> the online, idle runners no job names (YOUNG WAIVER)
  jq -r --arg p "$2" --arg jn "$3" \
    "(\$jn | split(\" \") | map(select(. != \"\"))) as \$named
     | .runners[] | select(${MINE} and .status==\"online\" and (.busy|not))
     | .name | select(. as \$n | \$named | any(. == \$n) | not)" <<<"$1"
}
# Fold a listing's samples into MEMPEAK. A working set is a SNAPSHOT, not a kernel high-water
# mark like memory.peak: the refill passes (every 3s) sample far more of a short job than the
# full tick alone would, but a spike between samples is not seen. The guest OOM counter is the backstop.
# Only repos in this config: the repo label is written by whoever starts the container, and
# a job is root in the VM, so a label is untrusted text headed for a .prom label value.
note_peaks() {
  local repo _n pk
  while read -r repo _n pk; do
    [[ "${pk}" =~ ^[0-9]+$ ]] || continue
    case " ${REPOS} " in *" ${repo} "*) ;; *) continue ;; esac
    [ "${pk}" -gt "${MEMPEAK[${repo}]:-0}" ] && MEMPEAK[${repo}]=${pk}
  done <<<"$1"
}

# No config is a failed look, never a default: a guessed pool size is how two copies drift.
VMID="$(jq -er '.runner_vm.vmid' "${CONFIG}" 2>/dev/null)" \
  && REPOS="$(jq -er '[.pools[].repo] | join(" ")' "${CONFIG}" 2>/dev/null)" \
  && MAX_TOTAL="$(jq -er '.max_total' "${CONFIG}" 2>/dev/null)" \
  && MEM_BUDGET_MB="$(jq -er '.mem_budget_mb' "${CONFIG}" 2>/dev/null)" \
  && CONFIG_SHA="$(jq -er '.source_sha256' "${CONFIG}" 2>/dev/null)" \
  && PREFIX="$(jq -er '.prefix' "${CONFIG}" 2>/dev/null)" \
  && ISOLATION="$(jq -er '.isolation' "${CONFIG}" 2>/dev/null)" \
  && [ "$(jq -r '.instance' "${CONFIG}" 2>/dev/null)" = "${INSTANCE}" ] \
  && [ -r "${ALLOC}" ] \
  || { echo "ci-runner-supervisor: cannot read ${CONFIG} (for instance ${INSTANCE}) or ${ALLOC} (the installer places them)" >&2; ok=0; publish; exit 2; }

# FULL-TICK SAMPLER. The refill loop samples every 3s, but only for its 45s; the full
# tick before it (reaper, GitHub reads, allocation: ~45s) listed ONCE. A 30s image build
# fell inside one full tick, after its listing, and neither MEMPEAK nor
# SIBPEAK saw it. So the full tick runs this lister in the background, same 3s, and stop_sampler
# folds it in before the mid-tick publish; the refill loop then samples for itself. Lines are
# tagged here, by position ("R" runner rows, "S" the trailer), never by what the VM printed.
# SOCKET INSTANCE ONLY: the secure VM has no daemon for jobs (no siblings), and there a guest call
# failed by a second concurrent caller reads as an ISOLATION VIOLATION in run_secure.
SAMPLES="${ETAG_DIR}/samples.${INSTANCE}.log"
: >"${SAMPLES}"
SAMPLER_PID=""
if [ "${ISOLATION}" = socket ]; then
  ( while :; do
      r="$(list_runners)" && { sed '$d' <<<"${r}" | sed 's/^/R /'; tail -n1 <<<"${r}" | sed 's/^/S /'; } >>"${SAMPLES}"
      sleep 3
    done ) &
  SAMPLER_PID=$!
  trap 'kill "${SAMPLER_PID}" 2>/dev/null' EXIT
fi
stop_sampler() {
  local tag a b
  [ -n "${SAMPLER_PID}" ] || return 0
  kill "${SAMPLER_PID}" 2>/dev/null; wait "${SAMPLER_PID}" 2>/dev/null
  trap - EXIT; SAMPLER_PID=""
  # the witness: 0 samples over a ~45s full tick = the lister failed every call (blind again)
  emit "${MP}runner_fulltick_samples $(grep -c '^S ' "${SAMPLES}" || true)"
  note_peaks "$(awk '$1 == "R" { sub(/^R /, ""); print }' "${SAMPLES}")"
  while read -r tag a b; do
    [ "${tag}" = S ] && [ "${a}" = "@sib" ] && isnum "${b}" || continue
    if [ -z "${SIBPEAK}" ] || [ "${b}" -gt "${SIBPEAK}" ]; then SIBPEAK=${b}; fi
  done <"${SAMPLES}"
  return 0
}
# The isolation mode follows the instance, never the file alone: a socket render installed under
# the secure name would otherwise start socket runners for the token-holding jobs.
# POOL != REPO: a pool is keyed by its own id (runner names, the repo container
# label, metrics, the allocator) and serves gh_repo -- two pools may serve one repo by job class
# (say `app` + `app-heavy`). Every GitHub call goes to GHREPO[pool]; a pool's runners are the
# names "<prefix><pool>-<epoch>-<rand>" EXACTLY, because `app`'s prefix also starts every
# `app-heavy` name. An older render has no gh_repo: the pool id is the repo.
# SWITCH[pool] = the repo variable its jobs' runs-on reads (e.g. CI_RUNNER, CI_SECURE_RUNNER,
# CI_RUNNER_HEAVY): the pages name it as the kill switch, so a class pool's alert never tells the
# operator to delete its sibling's variable. A pool without one fails the name check: refused, exit 2.
declare -A GHREPO SWITCH
while read -r k g v; do
  [[ "${k}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ && "${g}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ && "${v}" =~ ^[A-Z][A-Z0-9_]*$ ]] \
    || { echo "ci-runner-supervisor: ${CONFIG}: bad pool/repo/variable name ${k}/${g}/${v}" >&2; ok=0; publish; exit 2; }
  GHREPO[${k}]="${g}"; SWITCH[${k}]="${v}"
done < <(jq -r '.pools[] | "\(.repo) \(.gh_repo // .repo) \(.variable // "")"' "${CONFIG}")
# jq filter over a /runners item: true when it is THIS pool's runner (--arg p "<prefix><pool>-")
MINE='(.name | startswith($p) and (ltrimstr($p) | test("^[0-9]+-[0-9]+$")))'
case "${ISOLATION}" in socket|secure) ;; *) echo "ci-runner-supervisor: ${CONFIG}: unknown isolation ${ISOLATION}" >&2; ok=0; publish; exit 2 ;; esac
if [ "${ISOLATION}:${PREFIX}" != "${EXPECT}" ]; then
  echo "ci-runner-supervisor: ${CONFIG} says ${ISOLATION}/${PREFIX}; the unit for instance ${INSTANCE} expects ${EXPECT}" >&2; ok=0; publish; exit 2
fi
# ...and the secure instance never drives the socket VM: a secure render with the socket VM's id
# would start token-holding runners in the VM whose jobs own the docker socket. secure_checks'
# hardening read-back (below) refuses a socket VM too; this catches it before anything starts.
# foreign_vmids = every OTHER runner block's VM, from the same capacity file: fails CLOSED (a render
# without the list is refused, never read as "no other VM").
if ! jq -e --argjson v "${VMID}" '(.supervisor.foreign_vmids | type) == "array" and (.supervisor.foreign_vmids | index($v)) == null' "${CONFIG}" >/dev/null 2>&1; then
  echo "ci-runner-supervisor: ${CONFIG} names VM ${VMID}, another runner block's VM (or has no foreign_vmids)" >&2; ok=0; publish; exit 2
fi
emit "${MP}runner_config_info{source_sha256=\"${CONFIG_SHA}\"} 1"
emit "${MP}runner_max_total ${MAX_TOTAL}"
emit "${MP}runner_mem_budget_bytes $(( MEM_BUDGET_MB * 1024 * 1024 ))"
# the plain RAM less reserve when the booking runs past it (mem_book_ratio): the low-memory alert's
# 10% is of THIS, never of the booking
_base="$(jq -r '.mem_base_mb // empty' "${CONFIG}" 2>/dev/null)"
isnum "${_base}" && emit "${MP}runner_mem_base_bytes $(( _base * 1024 * 1024 ))"

# The guest kernel's OOM kills: with the balloon off, the host sees qemu's RSS flat at the VM's
# full size, and host-side memory pressure read 0 through a global OOM inside the VM. Only the
# guest's own counter sees it. Read FIRST, before any bail-out below: an OOMing VM is exactly the
# one whose docker or token step exits early, and a counter missing on those ticks is a counter
# missing when it matters. Monotonic until the VM reboots; an alert on it uses increase().
oom_n="$(guest 10 awk '$1 == "oom_kill" { print $2 }' /proc/vmstat)" || oom_n=""
isnum "${oom_n}" && emit "${MP}runner_vm_oom_kills_total ${oom_n}"
# The guest's running kernel and boot time: nothing in cloud-init reboots after an unattended kernel
# upgrade, and the secure VM's isolation is the kernel (an alert reads the boot time as stale).
if kb="$(guest 10 awk '$1 == "btime" { print $2 }' /proc/stat)" && [[ "${kb}" =~ ^[0-9]+$ ]]; then
  emit "${MP}runner_vm_boot_time_seconds ${kb}"
fi
if kr="$(guest 10 uname -r)" && [[ "${kr}" =~ ^[A-Za-z0-9._+-]+$ ]]; then
  emit "${MP}runner_vm_kernel_info{release=\"${kr}\"} 1"
fi

if [ -z "${TOKEN}" ]; then
  echo "ci-runner-supervisor: no credential -- no GitHub App token and no PAT at ${CONF}/github.token" >&2
  ok=0; publish; exit 2
fi

if ! guest 30 docker info --format '{{.ServerVersion}}' >/dev/null; then
  echo "ci-runner-supervisor: VM ${VMID} guest agent or docker not answering" >&2
  ok=0; publish; exit 2
fi

# THE IMAGE IS PINNED BY ID, recorded HERE. A job is root in the VM (the socket), so anything it
# can write there -- the runner image's tag, a Dockerfile on disk, the legacy build cache
# -- is untrusted: a job could retag a backdoored image and every later runner (including ones
# handed secrets) would start from it. An image ID is a content hash: a job can delete it, never
# change it. So: run by ID; when the ID is gone, rebuild from the HOST's Dockerfile (stdin, no
# context) with --pull --no-cache, and record the new ID.
image_id="$(cat "${STATE}/image.id" 2>/dev/null)"
if [ -z "${image_id}" ] || ! guest 10 docker image inspect "${image_id}" >/dev/null; then
  echo "ci-runner-supervisor: pinned image ${image_id:-<none>} absent in the VM; rebuilding (minutes)" >&2
  js="$(qm guest exec "${VMID}" --timeout 1500 --pass-stdin 1 -- \
        docker build --pull --no-cache --label "${L_KEEP}=1" -q -t "${IMAGE}" - < "${DOCKERFILE}" 2>/dev/null)"
  image_id="$(jq -r '."out-data" // ""' <<<"${js}" | grep -oE 'sha256:[0-9a-f]{64}' | tail -1)"
  if [ "$(jq -r '.exitcode // 1' <<<"${js}")" != 0 ] || [[ "${image_id}" != sha256:* ]]; then
    echo "ci-runner-supervisor: image build failed: $(jq -r '."err-data" // ""' <<<"${js}" | tail -3)" >&2
    ok=0; publish; exit 2
  fi
  printf '%s' "${image_id}" > "${STATE}/image.id"
fi

# REAPER: nothing but runners may outlive a job. A job's own containers (testcontainers, buildx
# builders) die with it in the normal case; one left running past the longest job is leftover at
# best and a planted watcher (exec-ing into later runners) at worst. Kill anything older than the
# stray limit (e.g. 3h) that is not a runner. A runner's CONTAINER age says nothing about its JOB:
# an idle runner can wait hours, then take a job, and a container-age limit killed one 5 min into
# a unit job ("lost communication"). So GitHub's busy flag decides: an idle runner is recycled at
# the idle limit (e.g. 6h), a busy one only at the busy limit (e.g. 13h: idle limit + GitHub's 6h
# job cap + margin). If the busy list can't be read, every runner counts as busy. rm -v: a reaped
# container's anonymous volumes go with it (the SWEEP below says why).
# UNLABELLED = `<no value>`, not empty: docker's template prints that for a label the container
# does not have, so for a long time no container ever read as a stray -- every one got the idle
# runner's limit, and the stray limit here and secure's "a stray at once" could not fire (found
# when the pull-through cache's hand-started container outlived the stray limit).
# ONE runners read per pool per tick (conditional, gh_get), kept in RUNNERS_ALL for the pool loop
# below: reading it twice was a charged request per pool per tick for nothing.
busy=""
busy_blind=0
declare -A RUNNERS_ALL
for repo in ${REPOS}; do
  RUNNERS_ALL[${repo}]="$(gh_get "/repos/${OWNER}/${GHREPO[${repo}]}/actions/runners?per_page=100")" \
    && b="$(jq -r '.runners[] | select(.busy) | .name' <<<"${RUNNERS_ALL[${repo}]}")" \
    || { busy_blind=1; unset 'RUNNERS_ALL[${repo}]'; continue; }
  busy="${busy} ${b//$'\n'/ }"
done
# AFTER the loop: set inside it, a later repo's names were appended to "ALL" and the reaper's
# exact " ALL " match never fired -- one unreadable repo had its busy runners reaped at 6h.
[ "${busy_blind}" = 1 ] && busy="ALL"
# FOREIGN CONTAINERS (secure), counted BEFORE the reaper: secure reaps a stray at once, so a count
# taken after it read 0 for a container that WAS running. Anything
# without the runner label (or the isolation canary's, below) was started by something other than
# this supervisor -- in a VM with no socket for jobs, that is the finding. Named in the journal;
# <prefix>runner_vm_foreign_containers is what an alert reads (max over 15m, so one tick shows).
if [ "${ISOLATION}" = secure ]; then
  # shellcheck disable=SC2016  # expands in the guest's bash, not here
  fl="$(guest 10 bash -c 'set -o pipefail; docker ps -a --format "{{.Names}} x{{.Label \"'"${L_REPO}"'\"}}{{.Label \"'"${L_CANARY}"'\"}}" | awk "\$2 == \"x\" { print \$1 }"')" \
    || { echo "ci-runner-supervisor: cannot list containers for the foreign check" >&2; ok=0; publish; exit 2; }
  foreign=$(grep -c . <<<"${fl}" || true)
  [ "${foreign}" -gt 0 ] && echo "ci-runner-supervisor: FOREIGN container(s) in VM ${VMID}: ${fl//$'\n'/ } (reaping)" >&2
  emit "${MP}runner_vm_foreign_containers ${foreign}"
fi
# Limits per instance (the render's): secure reaps a busy runner early and a stray at once.
# The ONE exemption is the pull-through cache this supervisor started (registry_tick), by the full
# container ID recorded on the HOST -- never a label or a name, which any job could put on a watcher.
# Socket VM only: the secure instance passes no ID, so nothing in the secure VM is ever exempt.
REG_ID=""
[ "${ISOLATION}" = socket ] && REG_ID="$(cat "${STATE}/registry.id" 2>/dev/null)"
[[ "${REG_ID}" =~ ^[0-9a-f]{64}$ ]] || REG_ID=""
guest 30 bash -c 'now=$(date +%s); busy=" $1 "; docker ps -q | xargs -r docker inspect -f "{{.Id}} {{.Name}} {{.State.StartedAt}} {{index .Config.Labels \"'"${L_REPO}"'\"}}" | while read -r id name started repo; do [ -n "$4" ] && [ "$id" = "$4" ] && continue; [ "$repo" = "<no value>" ] && repo=""; name=${name#/}; age=$(( now - $(date -d "$started" +%s) )); if [ -z "$repo" ]; then lim=$3; else case "$busy" in " ALL "|*" $name "*) lim=$2 ;; *) lim='"${IDLE_LIMIT_S}"' ;; esac; fi; [ "$age" -gt "$lim" ] && docker rm -fv "$id" >/dev/null && echo "reaped $id ${repo:-non-runner} age=${age}s"; done; true' _ "${busy}" "${BUSY_LIMIT_S}" "${STRAY_LIMIT_S}" "${REG_ID}" \
  | sed 's/^/ci-runner-supervisor: /' >&2 || true
# THE REAPER'S WITNESS: the oldest container it should have removed and has not -- no
# repo label, not the recorded cache: exactly the reaper's own exemptions (the secure canary
# needs none: it is started and removed inside secure_checks, after this read) -- read apart from
# the reaper's own parse (jq over the inspect JSON, where a missing label is null, not `<no
# value>`). The reaper's `|| true` and its journal line were all there was: its stray limits could
# not fire for months and nothing said so. Past the limit + a tick = an alert on this gauge;
# absent 30m = the read is failing, and a second alert says so. A container that exits between ps
# and inspect (a --rm runner, a testcontainer) is dropped from the read, not the read itself:
# inspect still prints the ones it found; `docker ps` failing is the read failing.
# shellcheck disable=SC2016  # jq's $x / $now, not the shell's
STRAY_JQ='[.[] | (.Config.Labels // {}) as $l | select(.Id != $x and ($l["'"${L_REPO}"'"] // "") == "")
  | $now - (.State.StartedAt | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601)] | (max // 0) | floor'
# shellcheck disable=SC2016  # expands in the guest's bash, not here
stray_age="$(guest 15 bash -c 'ids=$(docker ps -q --no-trunc) || exit 1; j=$(printf "%s\n" $ids | xargs -r docker inspect 2>/dev/null); printf "%s" "${j:-[]}" | jq -r --arg x "$1" --argjson now "$(date +%s)" "$2"' _ "${REG_ID}" "${STRAY_JQ}")" || stray_age=""
isnum "${stray_age}" && emit "${MP}runner_vm_stray_oldest_seconds ${stray_age}"

# Work folders whose runner is gone (it took its job and exited --rm). Socket runners only: a
# secure runner's work folder is inside its own container and goes with it.
if [ "${ISOLATION}" = socket ]; then
  guest 30 bash -c 'live=" $(docker ps --format "{{.Names}}" | tr "\n" " ") "; for d in '"${WORK_ROOT}/${PREFIX}"'*; do [ -d "$d" ] || continue; case "$live" in *" ${d##*/} "*) ;; *) rm -rf "$d" ;; esac; done; true' >/dev/null || true
fi

# SWEEP, EVERY TICK: the producer is every job, so the cleanup runs at the job's cadence, not
# nightly. A container whose image declares a VOLUME (postgres:*, prom/prometheus) gets an
# ANONYMOUS volume, and `docker rm -f` without -v leaves it behind: test scripts that start and
# remove postgres containers leak a few ~46 MB data dirs a run. That came to hundreds of
# volumes / tens of GB between two nightly prunes, a burst took most of the free space down to
# ~1 GB inside an hour, and a job died "No space left on device".
# What goes: ANONYMOUS volumes (label com.docker.volume.anonymous) that NO container references
# (dangling; a stopped container counts as a reference) and are older than VOL_MIN_AGE_S. Named
# volumes and compose projects' are left to a nightly `volume prune -af`. Why the age gate and
# not a bare `volume prune -f`: an unnamed `docker volume create` is ALSO anonymous (measured in
# the VM), so a job holding one between create and `docker run -v` would lose it to
# the next tick. `docker volume rm` refuses one that got attached since the listing.
# Exited containers go at 6h (GitHub's job cap), so none can still belong to a running job; their
# volumes go a tick later. Tagged images stay on the nightly week (warm pulls are the speed
# win); dangling images and builder cache go per tick, below.
# A failed sweep is not a blind tick: <prefix>runner_vm_volumes_bytes climbs (an alert on it)
# or goes missing (an absence alert), then the disk-free alert.
# The removed count is a COUNTER kept in a host file (a per-tick gauge is scraped several times
# a tick, so sum_over_time over-counts): increase(<prefix>runner_vm_volumes_pruned_total[1h]) is
# the producer's rate. A lost file restarts it at 0, which increase() reads as a reset.
VOL_MIN_AGE_S=900
VOL_PRUNED_F="${STATE}/volumes_pruned_total"
vol_total="$(cat "${VOL_PRUNED_F}" 2>/dev/null)"; [[ "${vol_total}" =~ ^[0-9]+$ ]] || vol_total=0
# shellcheck disable=SC2016  # expands in the guest's bash, not here
if vol_pruned="$(guest 120 bash -c 'docker container prune -f --filter until=6h >/dev/null || exit 1; now=$(date +%s); docker volume ls -q -f dangling=true -f label=com.docker.volume.anonymous | xargs -r docker volume inspect -f "{{.Name}} {{.CreatedAt}}" | while read -r n c; do [ $(( now - $(date -d "$c" +%s) )) -gt "$1" ] && echo "$n"; done | xargs -r docker volume rm 2>/dev/null; true' _ "${VOL_MIN_AGE_S}")"; then
  vol_total=$(( vol_total + $(grep -cE '^[0-9a-f]{64}$' <<<"${vol_pruned}" || true) ))
  printf '%s' "${vol_total}" > "${VOL_PRUNED_F}.new" && mv -f "${VOL_PRUNED_F}.new" "${VOL_PRUNED_F}"
else
  echo "ci-runner-supervisor: volume sweep failed" >&2
fi
emit "${MP}runner_vm_volumes_pruned_total ${vol_total}"
if vol_names="$(guest 10 docker volume ls -q)"; then
  emit "${MP}runner_vm_volumes $(grep -c . <<<"${vol_names}" || true)"
fi
vol_b="$(guest 60 du -sb /var/lib/docker/volumes | cut -f1)" || vol_b=""
isnum "${vol_b}" && emit "${MP}runner_vm_volumes_bytes ${vol_b}"

# IMAGES AND BUILD CACHE, EVERY TICK. The producer is an hourly image build (several GB a layer
# set): each build retags, the old image goes untagged,
# and BUILDKIT KEEPS ITS LAYERS as cache records until `builder prune`. Under a nightly 72h
# window that was ~17 GB of overlay2, which `docker system df` reported as 58 MB of "Build Cache"
# (layers shared with a deleted image are charged to nothing). The VM filled overnight and
# unit and lint jobs died "No space left on device" while the disk-free alert sat
# PENDING for two hours: the free gauge bounced over 10 GB on every tick, so `for: 15m` never
# held. So, at the producer's cadence: dangling images go (the running runners pin the untagged old
# runner image, and the keep label is honoured), builder cache past 2h goes (a re-run right after a
# failure still finds its layers warm; the hourly build lays down ~5 GB of NEW layers each time,
# so every hour kept is ~5 GB of disk -- 6h was ~29 GB), and the REAL store size -- du -x of overlay2,
# ONE filesystem, because the running containers' `merged` dirs are overlay mounts under it and a
# plain du counts every lower layer again through each (7 GB high with 4 containers)
# -- is a gauge, because Docker's own numbers under-report exactly the pile that matters
# (<prefix>runner_vm_overlay2_bytes; `docker system df` said ~1 GB of build cache against over 10 GB on disk). prune_ok 0 is a sweep that
# failed (an alert on <prefix>runner_vm_image_prune_ok); the reported bytes are a counter like the volume one, and
# say what docker CLAIMED, so the gauge's drop against them is the under-report.
IMG_PRUNED_F="${STATE}/images_pruned_bytes_total"
img_total="$(cat "${IMG_PRUNED_F}" 2>/dev/null)"; [[ "${img_total}" =~ ^[0-9]+$ ]] || img_total=0
img_ok=0
# shellcheck disable=SC2016  # expands in the guest's bash, not here
if img_out="$(guest 300 bash -c 'a=$(docker image prune -f --filter "label!='"${L_KEEP}"'" | sed -n "s/^Total reclaimed space: //p") || exit 1; b=$(docker builder prune -af --filter until=2h | sed -n "s/^Total:[[:space:]]*//p") || exit 1; echo "${a:-0B} ${b:-0B}"')"; then
  img_ok=1
  img_total=$(( img_total + $(docker_bytes "${img_out%% *}") + $(docker_bytes "${img_out##* }") ))
  printf '%s' "${img_total}" > "${IMG_PRUNED_F}.new" && mv -f "${IMG_PRUNED_F}.new" "${IMG_PRUNED_F}"
else
  echo "ci-runner-supervisor: image/builder prune failed" >&2
fi
emit "${MP}runner_vm_image_prune_ok ${img_ok}"
emit "${MP}runner_vm_image_prune_reported_bytes_total ${img_total}"
# The du is 3s warm / 13s cold (measured), but ONE tick's miss must not blank the
# gauge or refuse the install: a guest exec that outlives its timeout returns a pid and no exitcode,
# and the first tick after an install did exactly that once. So the last good value lives
# in STATE and is re-emitted for up to 30 min, and measure_ok says whether THIS tick measured --
# an alert reads it, as one reads prune_ok. Past 30 min the gauge goes ABSENT rather than
# stale: a frozen number under the overlay2-size alert would read as calm.
OVL_F="${STATE}/overlay2_bytes"
ovl_ok=0
if ovl_b="$(guest 120 du -sbx /var/lib/docker/overlay2 | cut -f1)" && [[ "${ovl_b}" =~ ^[0-9]+$ ]]; then
  ovl_ok=1
  printf '%s %s\n' "${ovl_b}" "$(date +%s)" > "${OVL_F}.new" && mv -f "${OVL_F}.new" "${OVL_F}"
else
  echo "ci-runner-supervisor: overlay2 du failed this tick; re-emitting the last value if under 30 min old" >&2
  { read -r ovl_b ovl_ts < "${OVL_F}"; } 2>/dev/null || { ovl_b=""; ovl_ts=""; }
  { [[ "${ovl_ts}" =~ ^[0-9]+$ ]] && [ $(( $(date +%s) - ovl_ts )) -le 1800 ]; } || ovl_b=""
fi
emit "${MP}runner_vm_overlay2_measure_ok ${ovl_ok}"
[[ "${ovl_b}" =~ ^[0-9]+$ ]] && emit "${MP}runner_vm_overlay2_bytes ${ovl_b}"

# run_socket <repo> <name> <jit>: the SOCKET runner -- root over the VM's docker socket, a work folder
# bind-mounted at the same path in the VM. Never for a job that holds a secret.
# TMPDIR inside the mounted folder too: tests bind-mount `mktemp -d` dirs (e.g. a routing-config
# test that mounts a temp dir into a container). One runner = one job, so it is still per-job.
# GIT_HTTP_LOW_SPEED_* (the render's runner_env): a stalled fetch ABORTS after 60s under 1 KB/s, so
# actions/checkout's own retry runs. Without it a depth-1 fetch hung ~5 min until concurrency
# cancelled the run.
run_socket() {
  local repo="$1" name="$2" jit="$3"
  guest 10 install -d -o 1001 -g 1001 "${WORK_ROOT}/${name}" "${WORK_ROOT}/${name}/_tmp" >/dev/null \
    && guest 30 docker run -d --rm --name "${name}" --label "${L_REPO}=${repo}" \
      -v "${WORK_ROOT}/${name}:${WORK_ROOT}/${name}" -e "TMPDIR=${WORK_ROOT}/${name}/_tmp" \
      "${CACHE_ARGS[@]}" \
      "${RUNNER_ENV[@]}" --network host --group-add "${sock_gid}" -v /var/run/docker.sock:/var/run/docker.sock \
      "${image_id}" ./run.sh --jitconfig "${jit}" >/dev/null
}

# secure_args <repo>: the SECRET-POOL container shape, into SA. ONE array for the runner AND the
# isolation canary, so what the canary proves is what a runner gets.
# No socket, no bind mount, no host network, no added group, every capability dropped, no setuid
# escalation, uid 1001, the default seccomp profile (load-bearing: it blocks the user-namespace
# route behind CVE-2024-1086 et al.), and its own icc=false bridge so two runners cannot reach each
# other. cpus / memory / pids are the pool's, ENFORCED here (swap = memory, so no swap).
# A unit test refuses a forbidden flag here; secure_violations re-reads
# the result from `docker inspect`.
secure_args() {
  local cpus mem pids
  read -r cpus mem pids < <(jq -r --arg r "$1" '.pools[] | select(.repo == $r) | "\(.cpus) \(.memory_mb) \(.pids)"' "${CONFIG}")
  [[ "${mem}" =~ ^[0-9]+$ && "${pids}" =~ ^[0-9]+$ && "${cpus}" =~ ^[0-9.]+$ ]] || return 1
  SA=(--cap-drop ALL --security-opt no-new-privileges --user 1001:1001
      --memory "${mem}m" --memory-swap "${mem}m" --pids-limit "${pids}" --cpus "${cpus}"
      --network "${SECURE_NET}")
}

# inspect_secure <name...> -> one `docker inspect` JSON object per line (the whole object: a field
# the check does not list yet is still there to add, and nothing is split on a delimiter).
inspect_secure() {
  guest 20 docker inspect -f '{{json .}}' "$@"
}

# run_secure <repo> <name> <jit>: the SECRET-POOL runner, then its own inspect before the tick goes
# on: a runner off the shape is removed at once and the start counts as failed.
run_secure() {
  local repo="$1" name="$2" jit="$3" bad
  secure_args "${repo}" || return 1
  guest 30 docker run -d --rm --name "${name}" --label "${L_REPO}=${repo}" "${SA[@]}" \
    "${RUNNER_ENV[@]}" \
    "${image_id}" ./run.sh --jitconfig "${jit}" >/dev/null || return 1
  bad="$(inspect_secure "${name}" | secure_violations)" && [ -z "${bad}" ] && return 0
  echo "ci-runner-supervisor: ISOLATION VIOLATION at start: ${bad:-<inspect failed>}; removing ${name}" >&2
  START_VIOLATIONS=$(( START_VIOLATIONS + 1 ))
  guest 10 docker rm -f "${name}" >/dev/null 2>&1 || true
  return 1
}

# secure_checks: the secure VM's isolation, re-proven every tick from what is RUNNING, so a later
# edit cannot quietly bring the socket back. Publishes the icc=false bridge, the
# VM's own hardening, user.max_user_namespaces (0 = second layer behind seccomp), and
# the violations of every live runner PLUS a canary: a container started with SA (the runners'
# exact args), wearing the canary label, inspected, removed. The canary means a quiet pool -- most
# ticks, min 0 -- still proves the shape (checked >= 1), not a pass on nothing inspected.
# A violation removes the violating runner and returns 1: the caller publishes ok=0 and exits
# BEFORE fill, so no new runner starts on a shape that just failed.
# Nonzero = could not look, or looked and found a violation.
# shellcheck disable=SC2016  # expands in the guest's bash, not here
secure_checks() {
  local icc userns hard insp bad canary rc=0
  icc="$(guest 20 bash -c 'docker network inspect "$1" >/dev/null 2>&1 || docker network create --driver bridge -o com.docker.network.bridge.enable_icc=false "$1" >/dev/null; docker network inspect -f "{{index .Options \"com.docker.network.bridge.enable_icc\"}}" "$1"' _ "${SECURE_NET}")" || return 1
  emit "${MP}runner_secure_icc_disabled $([ "${icc}" = false ] && echo 1 || echo 0)"
  # icc back on: two token-holding runners on the bridge can reach each other. No start this tick.
  [ "${icc}" = false ] || { echo "ci-runner-supervisor: ${SECURE_NET} has icc=${icc}" >&2; rc=1; }
  # The VM's hardening, read back (cloud-init wrote it once; nothing re-read it): the INPUT drops
  # that keep a container off the VM's own ports (guest agent aside, the daemon's API is not on
  # TCP, but the VM's sshd is), and daemon.json's icc/no-new-privileges defaults. A socket VM
  # has neither, so a secure instance pointed at it reads 0 here.
  hard="$(guest 10 bash -c 'iptables -C INPUT -i br-+ -j DROP 2>/dev/null && iptables -C INPUT -i docker0 -j DROP 2>/dev/null && jq -e ".icc == false and .\"no-new-privileges\" == true" /etc/docker/daemon.json >/dev/null 2>&1 && echo 1 || echo 0')" || return 1
  [[ "${hard}" =~ ^[01]$ ]] || return 1
  [ "${hard}" = 1 ] || { echo "ci-runner-supervisor: VM ${VMID} hardening read-back FAILED (INPUT drops / daemon.json)" >&2; rc=1; }
  emit "${MP}runner_secure_vm_hardened ${hard}"
  userns="$(guest 10 cat /proc/sys/user/max_user_namespaces)" && isnum "${userns}" \
    && emit "${MP}runner_vm_userns_max ${userns}"
  # userns open: the layer behind seccomp is gone. No start this tick (an unread value is unmeasured, not 0).
  isnum "${userns}" && [ "${userns}" != 0 ] && { echo "ci-runner-supervisor: VM ${VMID} max_user_namespaces=${userns}" >&2; rc=1; }
  # THE CANARY: any pool's args (they differ only in sizes); sleep, never the runner.
  secure_args "${REPOS%% *}" || return 1
  canary="${PREFIX}canary-$(date +%s)-${RANDOM}"
  guest 30 docker run -d --rm --name "${canary}" --label "${L_CANARY}=1" "${SA[@]}" \
    --entrypoint sleep "${image_id}" 120 >/dev/null \
    || { guest 10 docker rm -f "${canary}" >/dev/null 2>&1; echo "ci-runner-supervisor: canary start failed" >&2; return 1; }
  # EVERY running container, not just repo-labelled ones: a label is not proof of origin, so
  # a container that wore the canary label (exempt from the foreign count) was never inspected.
  # The canary by name first, so a missing canary fails the look.
  insp="$(guest 20 bash -c 'set -o pipefail; docker inspect -f "{{json .}}" "$1" && c=$(docker inspect -f "{{.Id}}" "$1") && docker ps -q --no-trunc | while read -r i; do [ "$i" = "$c" ] || echo "$i"; done | xargs -r docker inspect -f "{{json .}}"' _ "${canary}")"
  local irc=$?
  guest 10 docker rm -f "${canary}" >/dev/null 2>&1 || true
  [ "${irc}" = 0 ] || return 1
  bad="$(secure_violations <<<"${insp}")" || return 1
  emit "${MP}runner_isolation_checked $(grep -c . <<<"${insp}" || true)"
  emit "${MP}runner_isolation_violations $(grep -c . <<<"${bad}" || true)"
  if [ -n "${bad}" ]; then
    echo "ci-runner-supervisor: ISOLATION VIOLATION: ${bad//$'\n'/; } -- removing them; no runner starts this tick" >&2
    # shellcheck disable=SC2046  # one name per line, names are [A-Za-z0-9._-] (docker's own rule)
    guest 20 docker rm -f $(awk '{ print $1 }' <<<"${bad}") >/dev/null 2>&1 || true
    rc=1
  fi
  return "${rc}"
}

# secure_violations: stdin = `docker inspect` JSON, one container per line; stdout = "<name>
# <what>,..." for each one off run_secure's shape (an ALLOW-list where docker has one: security
# options, networks, runtime). Pure (jq), so it is unit-tested. A line that is not
# JSON fails the call (could not look), never reads clean.
secure_violations() {
  jq -cr --arg net "${SECURE_NET}" '. as $c | .HostConfig as $h
    | [ (if ($h.Binds // []) != [] then "bind" else empty end),
        (if (($h.Mounts // []) != []) or ((.Mounts // []) != []) then "mount" else empty end),
        (if ($h.VolumesFrom // []) != [] then "volumes-from" else empty end),
        (if (($h.CapDrop // []) | index("ALL")) == null then "cap-drop" else empty end),
        (if ($h.CapAdd // []) != [] then "cap-add" else empty end),
        (if (($h.SecurityOpt // []) | map(select(IN("no-new-privileges", "no-new-privileges:true", "no-new-privileges=true"))) | length) == 0
           then "no-new-privileges" else empty end),
        (if (($h.SecurityOpt // []) | map(select(IN("no-new-privileges", "no-new-privileges:true", "no-new-privileges=true") | not)) | length) > 0
           then "security-opt" else empty end),
        (if (.AppArmorProfile // "") == "unconfined" then "apparmor" else empty end),
        (if .Config.User != "1001:1001" then "user" else empty end),
        (if $h.NetworkMode != $net or ((.NetworkSettings.Networks // {}) | keys) != [$net] then "network" else empty end),
        (if $h.Privileged != false then "privileged" else empty end),
        (if ($h.GroupAdd // []) != [] then "group-add" else empty end),
        (if ($h.Devices // []) != [] then "device" else empty end),
        (if ($h.PidMode // "") != "" then "pid" else empty end),
        (if ($h.IpcMode // "") | IN("", "private", "none") | not then "ipc" else empty end),
        (if ($h.UsernsMode // "") != "" then "userns" else empty end),
        (if ($h.UTSMode // "") != "" then "uts" else empty end),
        (if ($h.CgroupnsMode // "private") != "private" then "cgroupns" else empty end),
        (if ($h.Sysctls // {}) != {} then "sysctl" else empty end),
        (if ($h.Runtime // "runc") != "runc" then "runtime" else empty end),
        (if (($h.Memory // 0) > 0 and $h.MemorySwap == $h.Memory and ($h.PidsLimit // 0) > 0 and ($h.NanoCpus // 0) > 0) | not
           then "limits" else empty end) ]
    | select(length > 0) | "\($c.Name // "?" | ltrimstr("/")) \(join(","))"'
}

# registry_probe_lines: stdin = what the guest's probe printed (line 1 "<cache /v2/ code> <Docker Hub
# /v2/ code>", then the cache's registry_proxy_* counter lines) -> emit-ready lines on stdout. Every
# byte is from the VM, so a line that is not exactly a known counter with a number is dropped, and
# a line 1 that is not two 3-digit codes publishes nothing (absent = unmeasured, never 0 or 1).
# Upstream reachable = 401 (anonymous /v2/ asks for a token) or 200; 000 is curl's no-answer.
registry_probe_lines() {
  local codes line n t v
  IFS= read -r codes || return 0
  if [[ "${codes}" =~ ^([0-9]{3})\ ([0-9]{3})$ ]]; then
    echo "${MP}registry_cache_up $([ "${BASH_REMATCH[1]}" = 200 ] && echo 1 || echo 0)"
    case "${BASH_REMATCH[2]}" in 200|401) echo "${MP}registry_upstream_ok 1" ;; *) echo "${MP}registry_upstream_ok 0" ;; esac
  else
    return 0
  fi
  while IFS= read -r line; do
    [[ "${line}" =~ ^registry_proxy_(requests|hits|misses|pulled_bytes|pushed_bytes)_total\{type=\"(blob|manifest)\"\}\ ([0-9]+(\.[0-9]+)?([eE][+]?[0-9]+)?)$ ]] || continue
    n="${BASH_REMATCH[1]}"; t="${BASH_REMATCH[2]}"; v="$(awk -v x="${BASH_REMATCH[3]}" 'BEGIN { printf "%.0f", x }')"
    echo "${MP}registry_proxy_${n}_total{type=\"${t}\"} ${v}"
  done
}

# reg_state: what the socket VM holds under the cache's name, from POSITIVE evidence only --
# "<id> <image>", ABSENT (docker said there is no such object), or nothing at all when the guest
# agent or docker did not answer. Unknown is never acted on: a guest-agent flap read as "gone"
# would recreate a healthy cache, and in a Docker Hub outage the new one cannot start.
# `docker CONTAINER inspect`: an untyped inspect also answers for a volume, network or image of that
# name (measured), so a job's `docker volume create <the cache's name>` would have pinned the
# state at "unknown" for good. Docker 26 says "No such container: <name>"; "No such object" kept for
# the untyped wording.
reg_state() {
  local st
  # shellcheck disable=SC2016  # expands in the guest's bash, not here
  st="$(guest 10 bash -c 'o=$(docker container inspect -f "{{.Id}} {{.Config.Image}}" "$1" 2>&1) && { printf "%s\n" "$o"; exit 0; }; case "$o" in *[Nn]"o such container"*|*[Nn]"o such object"*) echo ABSENT ;; *) exit 1 ;; esac' _ "${REG_NAME}")" || return 1
  [[ "${st}" =~ ^(ABSENT|[0-9a-f]{64}\ [^[:space:]]+)$ ]] || return 1
  printf '%s' "${st}"
}

# reg_live_mirrors: the LIVE daemon's mirror list (docker info -- what protects; rc != 0 = could not
# read). reg_has_mirror: that list (stdin) holds the cache. reg_file_has_mirror: daemon.json does
# (what the next dockerd restart loads).
reg_live_mirrors() { guest 10 docker info --format '{{json .RegistryConfig.Mirrors}}'; }
reg_has_mirror() { jq -e --arg m "${REG_MIRROR}/" 'type == "array" and index($m) != null' >/dev/null 2>&1; }
# shellcheck disable=SC2016  # jq's $m, not the shell's
reg_file_has_mirror() { guest 10 jq -e --arg m "${REG_MIRROR}" '."registry-mirrors" | type == "array" and index($m) != null' /etc/docker/daemon.json >/dev/null; }

# reg_write_mirrors <json array>: daemon.json's registry-mirrors becomes exactly that, then SIGHUP
# (dockerd reloads registry-mirrors live; running containers untouched, measured). An EMPTY array
# is the kill switch's write: a reload of a file WITHOUT the key leaves the live list as it was
# (measured), so removing the key would turn nothing off.
reg_write_mirrors() {
  # shellcheck disable=SC2016  # expands in the guest's bash, not here
  guest 30 bash -c 'f=/etc/docker/daemon.json; { jq -c --argjson m "$1" ".\"registry-mirrors\" = \$m" "$f" 2>/dev/null || jq -nc --argjson m "$1" "{\"registry-mirrors\": \$m}"; } > "$f.new" && mv -f "$f.new" "$f" && systemctl reload docker' _ "$1" >/dev/null
}

# registry_tick: the socket VM's Docker Hub pull-through cache (constants at the top of the file) --
# bound its size, keep the pinned container up, keep the daemon's mirror on it, and publish what it
# saw. Never fails the tick: every failure mode here is fail-open (dockerd goes to Docker Hub), so
# it is gauges for the alerts (<prefix>registry_*), not a blind tick.
registry_tick() {
  local b st id mir probe l
  # KILL SWITCH: `touch ${STATE}/registry.off` on the host. The tick then removes the cache and
  # sets the daemon's mirrors to [] (every pull to Docker Hub direct, as before the cache) and keeps
  # them so; an alert on <prefix>registry_mirror_configured keeps saying the protection is off until the file goes.
  if [ -e "${STATE}/registry.off" ]; then
    emit "${MP}registry_disabled 1"
    guest 60 docker rm -f "${REG_NAME}" >/dev/null 2>&1
    rm -f "${STATE}/registry.id"; REG_ID=""
    mir="$(reg_live_mirrors)" || mir=""
    if [ -n "${mir}" ] && { reg_has_mirror <<<"${mir}" \
         || reg_file_has_mirror; }; then
      reg_write_mirrors '[]' && echo "ci-runner-supervisor: registry.off -- VM ${VMID} daemon's registry mirror removed" >&2
      mir="$(reg_live_mirrors)" || mir=""
    fi
    [ -n "${mir}" ] && emit "${MP}registry_mirror_configured $(reg_has_mirror <<<"${mir}" && echo 1 || echo 0)"
    return 0
  fi
  emit "${MP}registry_disabled 0"
  emit "${MP}registry_cache_max_bytes ${REG_CACHE_MAX_B}"
  # 65534 (nobody) owns the tree: the container runs as it, read-only root, no capability. Before
  # the du: a VM that never had the tree would publish no size, and the install refuses that.
  guest 10 install -d -o 65534 -g 65534 "${REG_CACHE}" >/dev/null
  st="$(reg_state)" || st=""
  # SIZE: wiped past the cap only between jobs, as the other caches. The container goes first (its
  # TTL scheduler state lives in that tree) and the store is emptied only once docker says it is
  # GONE: a store wiped under a live registry leaves its in-memory blob index pointing at nothing.
  b="$(guest 60 du -sb "${REG_CACHE}" | cut -f1)" || b=""
  if isnum "${b}"; then
    if [ "${b}" -gt "${REG_CACHE_MAX_B}" ] && [ -z "${busy// /}" ]; then
      guest 60 docker rm -f "${REG_NAME}" >/dev/null 2>&1
      st="$(reg_state)" || st=""
      if [ "${st}" = ABSENT ]; then
        guest 120 find "${REG_CACHE}" -mindepth 1 -delete >/dev/null \
          && echo "ci-runner-supervisor: registry cache ${b}B > ${REG_CACHE_MAX_B}B with no job running; wiped" >&2 \
          && b=0
      fi
    fi
    emit "${MP}registry_cache_bytes ${b}"
  fi
  # THE CONTAINER: the one this supervisor started (its ID on the host, REG_ID) on the pinned image.
  # Unknown (no answer) -> left alone. Gone -> started. Anything else under the name (replaced by a
  # job, an older pin, one this host has no record of) -> replaced, but only with no job running: a
  # removal under a pull fails that pull. A crash is docker's (--restart unless-stopped), not ours:
  # distribution 3.1.2 PANICS at start when upstream does not answer its /v2/ ping (measured), so
  # re-creating in a Docker Hub outage would only trade a running cache for one that cannot start.
  if [ -z "${st}" ]; then
    echo "ci-runner-supervisor: could not read the registry cache's state (guest agent / docker); left as it is" >&2
  elif [ -n "${REG_ID}" ] && [ "${st}" = "${REG_ID} ${REG_IMAGE}" ]; then
    :
  elif [ "${st}" != ABSENT ] && [ -n "${busy// /}" ]; then
    echo "ci-runner-supervisor: ${REG_NAME} is not the recorded cache on the pin; replaced at the next tick with no job running" >&2
  else
    [ "${st}" = ABSENT ] || guest 60 docker rm -f "${REG_NAME}" >/dev/null 2>&1
    if id="$(guest 240 docker run -d --name "${REG_NAME}" --restart unless-stopped --network host \
          --user 65534:65534 --cap-drop ALL --security-opt no-new-privileges --read-only --tmpfs /tmp \
          --memory 512m --pids-limit 256 --log-opt max-size=10m --log-opt max-file=2 \
          -v "${REG_CACHE}:/var/lib/registry" \
          -e "REGISTRY_HTTP_ADDR=${REG_ADDR}" -e "REGISTRY_HTTP_DEBUG_ADDR=${REG_DEBUG}" \
          -e REGISTRY_HTTP_DEBUG_PROMETHEUS_ENABLED=true \
          -e "REGISTRY_PROXY_REMOTEURL=${REG_UPSTREAM}" -e "REGISTRY_PROXY_TTL=${REG_TTL}" \
          -e REGISTRY_STORAGE_MAINTENANCE_UPLOADPURGING_ENABLED=true \
          -e REGISTRY_LOG_LEVEL=info -e "REGISTRY_LOG_FIELDS_ENVIRONMENT=${REG_LOGENV}" \
          "${REG_IMAGE}")" && [[ "${id}" =~ ^[0-9a-f]{64}$ ]]; then
      printf '%s' "${id}" > "${STATE}/registry.id.new" && mv -f "${STATE}/registry.id.new" "${STATE}/registry.id"
      REG_ID="${id}"
      echo "ci-runner-supervisor: started the registry cache ${id:0:12} (${REG_IMAGE##*@})" >&2
    else
      echo "ci-runner-supervisor: could not start the registry cache -- pulls go to Docker Hub direct" >&2
    fi
  fi
  # THE MIRROR, read from the LIVE daemon, never from daemon.json alone: a file the daemon did not
  # load protects nothing. Missing from the live list OR the file -> written and reloaded (the file
  # too: a reload of a file that lost the key leaves the live list as it was, measured, so the live
  # read stays 1 while the next dockerd restart would come up unmirrored). docker info shows the URL
  # with a trailing slash.
  mir="$(reg_live_mirrors)" || mir=""
  if [ -n "${mir}" ] && { ! reg_has_mirror <<<"${mir}" \
       || ! reg_file_has_mirror; }; then
    reg_write_mirrors "[\"${REG_MIRROR}\"]" \
      && echo "ci-runner-supervisor: VM ${VMID} daemon (live list or daemon.json) lacked the registry mirror; set ${REG_MIRROR} and reloaded" >&2
    mir="$(reg_live_mirrors)" || mir=""
  fi
  [ -n "${mir}" ] && emit "${MP}registry_mirror_configured $(reg_has_mirror <<<"${mir}" && echo 1 || echo 0)"
  # THE PROBE, one exec: the cache answers /v2/, Docker Hub answers /v2/ from this VM (the cache's
  # own vantage: host networking), and the cache's own counters (hit rate = hits / requests).
  # shellcheck disable=SC2016  # expands in the guest's bash, not here
  probe="$(guest 25 bash -c 'c=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://$1/v2/"); u=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 '"${REG_UPSTREAM}"'/v2/); echo "${c:-000} ${u:-000}"; curl -s --max-time 5 "http://$2/metrics" | grep -E "^registry_proxy_"; true' _ "${REG_ADDR}" "${REG_DEBUG}")" || probe=""
  while IFS= read -r l; do [ -n "${l}" ] && emit "${l}"; done < <(registry_probe_lines <<<"${probe}")
  return 0
}

if [ "${ISOLATION}" = socket ]; then
  if [ "${#CACHE_DIRS[@]}" -gt 0 ]; then
    guest 10 install -d -o 1001 -g 1001 "${CACHE_DIRS[@]}" >/dev/null || { ok=0; publish; exit 2; }
  fi
  [ -z "${REG_IMAGE}" ] || registry_tick
  # bound_cache <dir> <max_b> <metric> <label>: publish its size; wipe it past max, only between
  # jobs (a wipe under a running `uv sync`, jscpd fetch or `terraform init` fails that job).
  bound_cache() {
    local dir="$1" max="$2" metric="$3" label="$4" b
    b="$(guest 60 du -sb "${dir}" | cut -f1)" || b=""
    isnum "${b}" || return 0
    emit "${metric} ${b}"
    if [ "${b}" -gt "${max}" ] && [ -z "${busy// /}" ]; then
      guest 120 find "${dir}" -mindepth 1 -delete >/dev/null \
        && echo "ci-runner-supervisor: ${label} cache ${b}B > ${max}B with no job running; wiped" >&2
    fi
  }
  for row in "${CACHE_ROWS[@]}"; do
    read -r d m n <<<"${row}"
    bound_cache "${d}" "${m}" "${MP}runner_${n//-/_}_cache_bytes" "${n}"
  done
  sock_gid="$(guest 10 stat -c %g /var/run/docker.sock)" || { ok=0; publish; exit 2; }
else
  secure_checks || { ok=0; publish; exit 2; }
fi
free_b="$(guest 10 df --output=avail -B1 / | tail -1 | tr -d ' ')" || free_b=""
isnum "${free_b}" && emit "${MP}runner_vm_disk_free_bytes ${free_b}"
# REAL MEMORY BESIDE THE BOOKING. Admission books every runner at its pool's declared
# peak (at times most of the VM's memory committed) while `free -m` in the VM read a fraction of it
# used and most of it available, and nothing published the second number -- so how far a declared peak sits from
# reality, and the SIBLING containers a job starts on this daemon (buildx_buildkit_*,
# testcontainers, promtool) that no runner cgroup counts, were only visible by hand. One exec:
# MemAvailable, then the working set summed over every container WITHOUT the repo label.
# shellcheck disable=SC2016  # expands in the guest's bash, not here
vmmem="$(guest 15 bash -c 'set -o pipefail; a=$(awk "/^MemAvailable:/ {print \$2 * 1024}" /proc/meminfo) || exit 1; s=0; n=0; while read -r id repo; do [ -n "${repo}" ] && continue; d="/sys/fs/cgroup/system.slice/docker-${id}.scope"; cur=$(cat "${d}/memory.current" 2>/dev/null) || continue; ia=0; while read -r k v; do [ "${k}" = inactive_file ] && ia=${v}; done <"${d}/memory.stat"; s=$((s + cur - ia)); n=$((n + 1)); done < <(docker ps --no-trunc --format "{{.ID}} {{.Label \"'"${L_REPO}"'\"}}") && echo "${a} ${s} ${n}"')" || vmmem=""
read -r vm_avail sib_ws sib_n <<<"${vmmem}"
vm_read=0
if isnum "${vm_avail}" && isnum "${sib_ws}" && isnum "${sib_n}"; then
  vm_read=1
  emit "${MP}runner_vm_mem_available_bytes ${vm_avail}"
  emit "${MP}runner_vm_sibling_working_set_bytes ${sib_ws}"
  emit "${MP}runner_vm_sibling_containers ${sib_n}"
fi

# fill <repo> <target> <labels-json>: start the shortfall between ${running} (the caller's live
# container count) and target, appending each start to ${names}/${running}. Nonzero = a mint or
# start failed: the pool is short and nobody would know from the container count alone.
fill() {
  local repo="$1" target="$2" labels="$3" name jit wf=""
  for _ in $(seq $((running + 1)) "${target}"); do
    name="${PREFIX}${repo}-$(date +%s)-${RANDOM}"
    # a socket runner's work folder is the VM path its jobs' bind mounts resolve against (top of file);
    # a secure runner keeps the runner's default _work inside its own container
    [ "${ISOLATION}" = socket ] && wf=",\"work_folder\":\"${WORK_ROOT}/${name}\""
    jit="$(gh_api POST "/repos/${OWNER}/${GHREPO[${repo}]}/actions/runners/generate-jitconfig" \
      "{\"name\":\"${name}\",\"runner_group_id\":1,\"labels\":${labels}${wf}}" \
      | jq -r '.encoded_jit_config // empty')"
    if [ -z "${jit}" ]; then
      echo "ci-runner-supervisor: ${repo}: jitconfig mint failed" >&2; return 1
    fi
    if "run_${ISOLATION}" "${repo}" "${name}" "${jit}"; then
      running=$((running + 1)); names="${names}"$'\n'"${name}"
    else
      # A `guest 30` timeout can still leave the container starting in the VM; remove it, or the
      # next recount misses it and starts a second runner for the same slot.
      guest 10 docker rm -f "${name}" >/dev/null 2>&1 || true
      echo "ci-runner-supervisor: ${repo}: docker run failed" >&2; return 1
    fi
  done
}

now="$(date +%s)"
# THE SHARED SLOT BUDGET (the render's max_total): look at EVERY pool first, because one
# pool's burst is paid for by another's idle runners; decide once (the allocator, ALLOC); then act.
# TARGET[repo] is what this run may hold the pool at -- the full tick fills to it and the refill
# loop below tops up to it, so neither can take the budget past max_total.
declare -A NAMES RUNNERS LABELS OLDEST QUEUED JOBWAIT ONLINE BUSYN TARGET MINP IDLEN STARTED UNREADN LAGN
# YWAIVE[name]=1: a runner the live cap's young charge skips (YOUNG WAIVER below)
declare -A YWAIVE=()
refill_failed=" "
# AGING (the allocator's AGE_S): when each pool last got a runner, one epoch file per pool in
# /run. A pool with no file starts its clock now (first run, or after a reboot), so it ages from
# here instead of never. A failed write only means that pool ages from an older start.
START_DIR="${RUN}/last_start"
mkdir -p "${START_DIR}" 2>/dev/null || true

# carry_head prev_head plan_head state nblind -> "<head or -> <nblind>" (the head the next run
# drains for). A blind prev_head is carried only over an EMPTY plan head or the same one: when the
# allocator drained for ANOTHER pool this run, its caps were set for that head, and writing the
# blind one back would hand the next run a head this run never drained for.
# Past BLIND_CARRY_MAX it is dropped even when the allocator itself kept it as the plan head --
# otherwise the bound never binds (the allocator carries a blind prev_head's room).
carry_head() {
  local prev="$1" head="$2" state="$3" n="$4"
  if [ -n "${prev}" ] && { [ -z "${head}" ] || [ "${head}" = "${prev}" ]; } \
     && [ "$(jq -r --arg r "${prev}" '[.[] | select(.repo == $r) | .blind // false] | any' <<<"${state}")" = true ]; then
    n=$((n + 1))
    if [ "${n}" -le "${BLIND_CARRY_MAX}" ]; then head="${prev}"
    else
      echo "ci-runner-supervisor: drain head ${prev} blind ${n} runs in a row; dropped" >&2
      n=0; head=""
    fi
  else
    n=0
  fi
  printf '%s %s\n' "${head:--}" "${n}"
}

mark_start() { printf '%s\n' "$(date +%s)" > "${START_DIR}/$1" 2>/dev/null || true; }
since_start() {
  local t; t="$(head -c 20 "${START_DIR}/$1" 2>/dev/null | tr -cd '0-9')"
  [ -n "${t}" ] || { mark_start "$1"; t="${now}"; }
  echo $(( now > t ? now - t : 0 ))
}
# Runners of a repo NOT in this config (a pool just disabled or renamed) still hold slots until
# they take a job or the reaper recycles them: they go in as one FULL pool nobody acts on, so the
# budget never lends their slots out. Can't count them = can't keep the budget: stop looking.
raw="$(list_runners)" \
  || { echo "ci-runner-supervisor: cannot list runner containers" >&2; ok=0; publish; exit 2; }
note_sib "${raw}"
live="$(runners_of "${raw}")"
note_peaks "${live}"
all_repos="$(awk 'NF { print $1 }' <<<"${live}")"
unlisted=$(grep -cvxF -f <(tr ' ' '\n' <<<"${REPOS}") <<<"${all_repos}" || true)
if [ -n "${all_repos}" ] && [ "${unlisted}" -gt 0 ]; then
  state="[{\"repo\":\"(unlisted)\",\"min\":0,\"max\":${unlisted},\"running\":${unlisted},\"busy\":${unlisted},\"idle\":0,\"queued\":0,\"oldest\":0,\"blind\":true}]"
else
  state='[]'
fi
for repo in ${REPOS}; do
  min="$(jq -er --arg r "${repo}" '.pools[] | select(.repo == $r) | .min' "${CONFIG}")" \
    && max="$(jq -er --arg r "${repo}" '.pools[] | select(.repo == $r) | .max' "${CONFIG}")" \
    && LABELS[${repo}]="$(jq -ec --arg r "${repo}" '.pools[] | select(.repo == $r) | .labels' "${CONFIG}")" \
    || { echo "ci-runner-supervisor: ${CONFIG} lost ${repo} mid-tick" >&2; ok=0; publish; exit 2; }
  # A pool this tick could not see books exactly the runners it HAS (its live containers, all
  # counted busy, nothing to give, no demand): nothing is started or reclaimed in it, and it takes
  # no slot it is not using. Booking its MAX was safe with two pools; with a dozen
  # pools whose maxes sum past max_total, a few blind ones -- every repo the credential does not
  # cover yet, or one repo's API hiccup -- zeroed `free` and starved the pools that could see.
  # Its floor is pinned to that count too, or the allocator would grant it starts nobody makes.
  names="$(names_of "${repo}" "${live}")"
  nlive=$(grep -c . <<<"${names}" || true)
  blind="{\"repo\":\"${repo}\",\"min\":${nlive},\"max\":${nlive},\"running\":${nlive},\"busy\":${nlive},\"idle\":0,\"queued\":0,\"oldest\":0,\"blind\":true}"
  # runners: the reaper's read above (RUNNERS_ALL; absent = that read failed). Every list is a
  # conditional GET, so a quiet repo -- most of them -- costs no rate limit at all.
  [ -n "${RUNNERS_ALL[${repo}]+set}" ] && runners="${RUNNERS_ALL[${repo}]}" \
    && open_runs "${GHREPO[${repo}]}" \
    || { ok=0; state="$(jq -c --argjson p "${blind}" '. + [$p]' <<<"${state}")"; continue; }
  UNREADN[${repo}]="${UNREAD}"; LAGN[${repo}]="${LAG}"
  running=${nlive}

  # An ephemeral runner deregisters itself after its job; one whose container died first stays
  # registered and offline forever. Delete those -- only ones with no live container.
  # Checked against EVERY live container, not this pool's: a sibling pool of the same repo
  # (a heavy class pool) has its runners in this list too, and one still registering reads offline.
  while read -r id rname; do
    [ -z "${id}" ] && continue
    awk '{ print $2 }' <<<"${live}" | grep -qx "${rname}" && continue
    gh_api DELETE "/repos/${OWNER}/${GHREPO[${repo}]}/actions/runners/${id}" >/dev/null 2>&1 || true
  done < <(jq -r --arg p "${PREFIX}" '.runners[] | select((.name|startswith($p)) and .status=="offline") | "\(.id) \(.name)"' <<<"${runners}")

  # DEMAND is JOBS, not runs: one run fans out to several. A run stays `queued` while any job
  # waits, but an in_progress run can hold queued jobs too, and a
  # `pending` one can hold a queued job for an hour, so EVERY open
  # status's list is read, EVERY run in them (open_runs, runs.sh). API use is bounded by memo_get (one read per run per tick, shared by sibling pools) and
  # the conditional GET (an unchanged jobs list is a free 304). Only jobs THIS pool
  # can serve count (every label in its set): a hosted job beside them is not
  # demand, and a job asking for a label no pool carries is a workflow bug, not a slow pool.
  # The same reads give the oldest queued self-hosted JOB's wait: what a job actually sees. The
  # run age counts from the push and the online gauge is one sample per tick -- both read healthy
  # while jobs queued a median 82s for a refill.
  qjobs=0; job_wait=0; hot=""; jobs_ok=1; jnames=""
  for rid in $(jq -rs '[.[].workflow_runs[]] | unique_by(.id) | sort_by(.created_at) | .[] | .id' <<<"${queued_runs}${active_runs}"); do
    jpath="/repos/${OWNER}/${GHREPO[${repo}]}/actions/runs/${rid}/jobs?filter=latest&per_page=100"
    memo_get "${jpath}" || { ok=0; jobs_ok=0; continue; }
    read -r n w < <(job_demand "${MEMO[${jpath}]}" "${LABELS[${repo}]}" self-hosted "${now}") \
      || { ok=0; jobs_ok=0; continue; }
    [ -n "${n}" ] || { ok=0; jobs_ok=0; continue; }
    # every runner a job of these runs names (the YOUNG WAIVER never waives one: it has a job)
    jnames="${jnames} $(jq -r '[.jobs[]? | .runner_name // empty] | join(" ")' <<<"${MEMO[${jpath}]}" 2>/dev/null)" \
      || jobs_ok=0
    qjobs=$((qjobs + n))
    [ "${n}" -gt 0 ] && hot="${hot} ${rid}"
    [ "${w}" -gt "${job_wait}" ] && job_wait=${w}
  done

  # Oldest QUEUED run: a job no runner picked up. With the runners dead this is the only number
  # that grows -- a stuck PR looks exactly like a quiet day otherwise. Only runs holding a queued
  # job THIS pool serves count: the queued LIST also holds GHOSTS -- a run that reads `queued`
  # with zero jobs and refuses cancel, which aged the oldest-queued gauge to weeks. A failed jobs
  # read is ok=0, never a quiet pool.
  oldest=$(jq --argjson now "${now}" --arg hot "${hot}" \
    '($hot | split(" ") | map(select(. != "") | tonumber)) as $h
     | [.workflow_runs[] | select(.id as $i | $h | index($i)) | .created_at | fromdate]
     | if length == 0 then 0 else ($now - min) end' <<<"${queued_runs}")
  online=$(jq --arg p "${PREFIX}${repo}-" "[.runners[] | select(${MINE} and .status==\"online\")] | length" <<<"${runners}")
  nbusy=$(jq --arg p "${PREFIX}${repo}-" "[.runners[] | select(${MINE} and .busy)] | length" <<<"${runners}")
  idle=$(jq --arg p "${PREFIX}${repo}-" "[.runners[] | select(${MINE} and .status==\"online\" and (.busy|not))] | length" <<<"${runners}")

  NAMES[${repo}]="${names}"; RUNNERS[${repo}]="${runners}"; OLDEST[${repo}]="${oldest}"
  QUEUED[${repo}]="${qjobs}"; JOBWAIT[${repo}]="${job_wait}"; ONLINE[${repo}]="${online}"; BUSYN[${repo}]="${nbusy}"
  # idle for the refill's demand sum = live containers not busy, NOT GitHub's idle count: a runner
  # started in the last run's final seconds can still be registering (not yet `online`), and
  # leaving it out let a zero-floor pool start a second runner for a job that one takes.
  MINP[${repo}]="${min}"; IDLEN[${repo}]=$(( running > nbusy ? running - nbusy : 0 ))
  # YOUNG WAIVER: a runner GitHub reads ONLINE and IDLE, in a pool with NO queued job
  # it could take, has no job to grow into -- its working set is already in MemAvailable. Only those
  # skip the young charge; a busy one (its job is growing), a registering one (not yet online), and
  # every runner of a pool with queued work keep it. FAIL CLOSED: a pool whose jobs
  # read failed (qjobs reads 0 for "could not look") or left runs unread waives nothing, and a runner
  # any read job names is never waived -- the runner list is read before the jobs, so a job assigned
  # in between leaves its runner reading idle while the job (in_progress) no longer counts as queued.
  if [ "${qjobs}" -eq 0 ] && [ "${jobs_ok}" = 1 ] && [ "${UNREAD:-0}" = 0 ]; then
    while read -r yn; do [ -n "${yn}" ] && YWAIVE[${yn}]=1; done \
      < <(young_waivable "${runners}" "${PREFIX}${repo}-" "${jnames}")
  fi
  state="$(jq -c --arg r "${repo}" --argjson mn "${min}" --argjson mx "${max}" --argjson ru "${running}" \
           --argjson b "${nbusy}" --argjson i "${idle}" --argjson q "${qjobs}" --argjson o "${job_wait}" \
           --argjson ss "$(since_start "${repo}")" \
           '. + [{repo: $r, min: $mn, max: $mx, running: $ru, busy: $b, idle: $i, queued: $q, oldest: $o, since_start: $ss}]' <<<"${state}")"
done

# MEMORY-WEIGHTED ADMISSION: every live runner counts at its pool's measured peak_mb
# against mem_budget_mb; an (unlisted) runner is charged the largest peak (the allocator's rule).
# The drain HEAD is carried across runs (the allocator's STARVATION rule): without it a pool sorted
# ahead of the head took back the room the drain had just freed. ${RUN} (tmpfs): a reboot forgets
# it, which only costs one re-pick. A missing/garbled file is no head, never an error.
HEAD_FILE="${RUN}/drain_head"
prev_head="$(head -c 200 "${HEAD_FILE}" 2>/dev/null | tr -cd 'A-Za-z0-9._-')"
# the drain backfill's kill switch (the render's backfill: false); absent = on
BACKFILL="$(jq -r 'if .backfill == false then "false" else "true" end' "${CONFIG}" 2>/dev/null)"
[ "${BACKFILL}" = false ] || BACKFILL=true
emit "${MP}runner_backfill_enabled $([ "${BACKFILL}" = true ] && echo 1 || echo 0)"
# LIVE CAP (the render's mem_book_ratio + live_floor_mb): the VM's MemAvailable
# (read above) less live_floor_mb caps what this run admits. No live read -> null, and the
# allocator falls back to mem_base_mb (no overcommit blind). Unset floor = no live cap.
MEM_BASE_MB="$(jq -r '.mem_base_mb // "null"' "${CONFIG}" 2>/dev/null)"; isnum "${MEM_BASE_MB}" || MEM_BASE_MB=null
LIVE_FLOOR_MB="$(jq -r '.live_floor_mb // empty' "${CONFIG}" 2>/dev/null)"
# YOUNG RUNNERS: a runner started in the last YOUNG_S is still registering or
# setting up, so MemAvailable does not show it yet; without this the live room re-admits the same
# memory next tick and a burst reaches the full booking in two ticks. Each young runner is charged
# its pool's YOUNG CHARGE less its measured working set (unmeasured = the whole charge). Name =
# ${PREFIX}<repo>-<epoch>-<rand>; an unparseable name is not young. A job is root in the VM and can
# plant a container with a forged future epoch: that shrinks the GLOBAL live room (every pool's
# admission) until it is reaped -- a self-DoS of CI, never an over-admission. The epoch must be a
# plain decimal (no leading 0: bash reads 089 as octal and aborts the sum).
# THE YOUNG CHARGE is the render's young_mb, else peak_mb. Charging every
# young runner its pool's WORST job held the queue while the VM sat idle: after the booking went
# live the young term averaged about 40% of the memory really available (at times nearly all of
# it), with 0 OOM kills, live_free below zero, and well over a hundred jobs queued. A pool's 7d
# p95 working set is about half its peak: young_mb is that p95 plus headroom, and
# an alert on <prefix>runner_young_declared_bytes pages when the measured p95 climbs past it. The booking still charges
# every runner its full peak_mb; this only sizes the room for growth MemAvailable cannot see yet.
# A pool missing from the map (a render that lost it) is charged the LARGEST peak, never zero.
YOUNG_S=300
YOUNG_JSON="$(jq -c '[.pools[] | {(.repo): (.young_mb // .peak_mb)}] | add // {}' "${CONFIG}" 2>/dev/null)"
[ -n "${YOUNG_JSON}" ] || YOUNG_JSON='{}'
YOUNG_MAX="$(jq -r '[.pools[].peak_mb] | max // 0' "${CONFIG}" 2>/dev/null)"; isnum "${YOUNG_MAX}" || YOUNG_MAX=0
young_charge() { # pool -> the MiB a not-yet-visible runner of it is charged
  local c
  c="$(jq -r --arg r "$1" '.[$r] // empty' <<<"${YOUNG_JSON}")"
  isnum "${c}" && echo "${c}" || echo "${YOUNG_MAX}"
}
# YOUNG WAIVER: the tick's GitHub read marks YWAIVE (above); young_of
# skips those runners. The re-decide reuses the tick's read, so a job that lands on a waived runner
# inside the refill window is under-charged by at most its pool's young_mb until the next tick --
# inside live_floor_mb, and the near-OOM and OOM alerts page the cost. What was waived is
# <prefix>runner_live_young_waived_bytes and the trace's young_waived_mb.
young_of() { # listing [waived] -> MB still to come from runners younger than YOUNG_S (the refill re-decide too); "waived" = the MB the YOUNG WAIVER skipped instead
  local now_s yrepo yname yws yt ypk sum=0 wsum=0
  now_s=$(date +%s)
  while read -r yrepo yname yws; do
    [ -n "${yname}" ] || continue
    yt="${yname%-*}"; yt="${yt##*-}"
    [[ "${yt}" =~ ^[1-9][0-9]{0,11}$ ]] && [ $(( now_s - yt )) -lt "${YOUNG_S}" ] || continue
    ypk="$(young_charge "${yrepo}")"
    isnum "${ypk}" || continue
    isnum "${yws}" && ypk=$(( ypk - yws / 1048576 ))
    [ "${ypk}" -gt 0 ] || continue
    if [ -n "${YWAIVE[${yname}]:-}" ]; then wsum=$(( wsum + ypk )); else sum=$(( sum + ypk )); fi
  done <<<"$1"
  if [ "${2:-}" = waived ]; then echo "${wsum}"; else echo "${sum}"; fi
}
young_mb=0; young_waived_mb=0
isnum "${LIVE_FLOOR_MB}" && young_mb="$(young_of "${live}")" && young_waived_mb="$(young_of "${live}" waived)"
live_free=null
if isnum "${LIVE_FLOOR_MB}" && [ "${vm_read:-0}" = 1 ]; then
  live_free=$(( vm_avail / 1048576 - LIVE_FLOOR_MB - young_mb ))
  emit "${MP}runner_live_free_bytes $(( live_free * 1048576 ))"
  emit "${MP}runner_live_young_bytes $(( young_mb * 1048576 ))"
  emit "${MP}runner_live_young_waived_bytes $(( young_waived_mb * 1048576 ))"
fi
isnum "${LIVE_FLOOR_MB}" && emit "${MP}runner_live_cap_blind $([ "${live_free}" = null ] && echo 1 || echo 0)"
PEAKS_JSON="$(jq -c '[.pools[] | {(.repo): .peak_mb}] | add // {}' "${CONFIG}")"
# per-pool STARVE_S (the render's starve_s; the allocator's PER-POOL STARVE_S); absent = 300
STARVE_JSON="$(jq -c '[.pools[] | select(.starve_s != null) | {(.repo): .starve_s}] | add // {}' "${CONFIG}" 2>/dev/null)"
[ -n "${STARVE_JSON}" ] || STARVE_JSON='{}'
decide() { # state prev_head live_free -> the allocator's plan on stdout; nonzero = it failed
  jq -c --argjson t "${MAX_TOTAL}" --argjson m "${MEM_BUDGET_MB}" --arg ph "$2" \
     --argjson pk "${PEAKS_JSON}" --argjson bf "${BACKFILL}" --argjson base "${MEM_BASE_MB}" --argjson live "$3" \
     --argjson sv "${STARVE_JSON}" \
     '{max_total: $t, mem_budget_mb: $m, mem_base_mb: $base, live_free_mb: $live, peaks: $pk, starve_s: $sv, prev_head: (if $ph == "" then null else $ph end), backfill: $bf, pools: .}' \
     <<<"$1" | jq -c -f "${ALLOC}"
}
plan="$(decide "${state}" "${prev_head}" "${live_free}")" \
  || { echo "ci-runner-supervisor: allocator failed on ${state}" >&2; ok=0; publish; exit 2; }
# THE TRACE: every run's allocator input and answer, one JSON line, last 600 lines (ticks plus re-decides that started something; ~4h of ticks alone,
# ~1.5h when up to ~15 re-decides a run start something; at the
# timer) in ${RUN}. A start nobody logged could not be traced to the pass that granted it (a heavy pool
# re-admitted mid-drain); this is what a test is built from. tmpfs: a
# reboot drops it. A failed write only loses the trace, never the run.
TRACE="${RUN}/alloc_trace.jsonl"
# The memory inputs ride along: without live_free_mb a held tick could not say
# whether the BOOKING or the VM's real RAM bound it, so the trace could not rebuild the input.
# young_mb / young_waived_mb: the young charge inside live_free_mb, and what the YOUNG
# WAIVER skipped -- without them a held tick could not say how much of its shortfall was young.
{ jq -c --argjson ts "${now}" --arg ph "${prev_head}" --argjson in "${state}" \
    --argjson m "${MEM_BUDGET_MB}" --argjson base "${MEM_BASE_MB}" --argjson live "${live_free}" \
    --argjson y "${young_mb}" --argjson yw "${young_waived_mb}" \
    '{ts: $ts, prev_head: $ph, mem_budget_mb: $m, mem_base_mb: $base, live_free_mb: $live, young_mb: $y, young_waived_mb: $yw, in: $in, out: .}' \
    <<<"${plan}" >> "${TRACE}" \
  && tail -n 600 "${TRACE}" > "${TRACE}.tmp" && mv -f "${TRACE}.tmp" "${TRACE}"; } 2>/dev/null \
  || echo "ci-runner-supervisor: cannot write ${TRACE} (this run is not traced)" >&2
# A head whose pool this tick could not READ is booked blind (queued 0: not starved), so the
# allocator dropped it; one API hiccup must not hand its place to the pool it was draining for.
# Keep it: next run, when the reads work, it picks first again.
# BOUNDED: a head that stays unreadable (credential scope, a rename, one repo's outage)
# is carried at most BLIND_CARRY_MAX runs in a row, then dropped -- or it would hold every other
# pool at its floor for as long as it stays blind. The blind-supervisor alert pages on the blindness.
BLIND_CARRY_MAX=10
BLIND_FILE="${RUN}/drain_head_blind"
nblind="$(head -c 10 "${BLIND_FILE}" 2>/dev/null | tr -cd '0-9')"; nblind=${nblind:-0}
read -r new_head nblind < <(carry_head "${prev_head}" "$(jq -r '.head // ""' <<<"${plan}")" "${state}" "${nblind}")
[ "${new_head}" = - ] && new_head=""
printf '%s\n' "${nblind}" > "${BLIND_FILE}" 2>/dev/null || true
printf '%s\n' "${new_head}" > "${HEAD_FILE}" 2>/dev/null \
  || echo "ci-runner-supervisor: cannot write ${HEAD_FILE} (the drain head is not carried)" >&2
# what this tick holds + grants, counted at peaks: never over the budget unless runners of a pool
# that left the config (charged the largest peak) or a lowered budget put it there. Summed, not
# budget - free_mb: under the LIVE CAP free_mb is the live room, and the difference would read a
# tight VM as an overcommitted booking (a false over-commit alert). The allocator sums it
# (committed_mb); isnum, so a missing field drops the gauge instead of killing the tick (an
# empty $(( )) under set -u exited before any start).
committed_mb="$(jq -r '.committed_mb' <<<"${plan}")"
isnum "${committed_mb}" && emit "${MP}runner_mem_committed_bytes $(( committed_mb * 1024 * 1024 ))"
# 1 = a pool starved past STARVE_S and every other pool is held to its floor until it fits
emit "${MP}runner_draining $(jq -r 'if .drain then 1 else 0 end' <<<"${plan}")"

for repo in ${REPOS}; do
  [ "$(jq -r --arg r "${repo}" '.[] | select(.repo == $r) | .blind // false' <<<"${state}")" = false ] || continue
  read -r desired start reclaim held cap < <(jq -r --arg r "${repo}" '.pools[] | select(.repo == $r) | "\(.desired) \(.start) \(.reclaim) \(if .held then 1 else 0 end) \(.cap)"' <<<"${plan}")
  names="${NAMES[${repo}]}"; running=$(grep -c . <<<"${names}" || true)

  # RECLAIM: hand idle runners back to the budget for a pool that is short. GitHub refuses (422)
  # to delete a runner mid-job, but /runners has been seen reporting busy:false for one that is
  # working, so: re-read the ONE runner just before deleting it, and never take one younger than
  # 2 min (its job may be in flight). Delete at GitHub FIRST: once it is deregistered no job can
  # land on it, and only then is its container removed. A refused delete just leaves it be.
  reclaimed=0
  if [ "${reclaim}" -gt 0 ]; then
    while read -r id rname; do
      [ "${reclaimed}" -ge "${reclaim}" ] && break
      grep -qx "${rname}" <<<"${names}" || continue
      born="${rname#"${PREFIX}${repo}"-}"; born="${born%%-*}"
      [[ "${born}" =~ ^[0-9]+$ ]] && [ $((now - born)) -ge 120 ] || continue
      [ "$(gh_api GET "/repos/${OWNER}/${GHREPO[${repo}]}/actions/runners/${id}" | jq -r '.busy')" = false ] || continue
      gh_api DELETE "/repos/${OWNER}/${GHREPO[${repo}]}/actions/runners/${id}" >/dev/null 2>&1 || continue
      guest 30 docker rm -f "${rname}" >/dev/null || true
      reclaimed=$((reclaimed + 1)); running=$((running - 1))
      names="$(grep -vx "${rname}" <<<"${names}" || true)"
      echo "ci-runner-supervisor: ${repo}: reclaimed idle ${rname} for a pool that is short" >&2
    done < <(jq -r --arg p "${PREFIX}${repo}-" ".runners[] | select(${MINE} and .status==\"online\" and (.busy|not)) | \"\\(.id) \\(.name)\"" <<<"${RUNNERS[${repo}]}")
  fi

  # START what the allocator granted, then hold the pool there: never above desired, so a pool
  # with surplus shrinks as its runners finish jobs and their slots return to the budget.
  # cap < desired only while the allocator DRAINS for a starved pool (the allocator's
  # STARVATION rule): the refill must not replace this pool's finished runners, or the room never frees
  TARGET[${repo}]=$(( running + start < desired ? running + start : desired ))
  [ "${TARGET[${repo}]}" -gt "${cap}" ] && TARGET[${repo}]=${cap}
  # A failed start may still leave a container coming up unseen (a `guest 30` timeout), so the
  # refill loop leaves this pool alone for the run rather than start a second one for the slot.
  before=${running}
  [ "${start}" -gt 0 ] && echo "ci-runner-supervisor: start ${repo}: ${running} +${start} (desired ${desired}, cap ${cap}, head ${new_head:-none})" >&2
  [ "${start}" -gt 0 ] && { fill "${repo}" "$((running + start))" "${LABELS[${repo}]}" \
    || { ok=0; refill_failed="${refill_failed}${repo} "; }; }
  STARTED[${repo}]=$(( running - before ))
  [ "${running}" -gt "${before}" ] && mark_start "${repo}"

  # target = what the budget wants this pool to hold now (its floor when quiet, more in a burst).
  emit "${MP}runner_target{repo=\"${repo}\"} ${desired}"
  # 1 = this pool had queued work and its next runner did not fit (mem_budget_mb, or a starved
  # pool's slot); past STARVE_S it reserves and the others drain (the allocator's STARVATION rule).
  # an alert pages if it never ends
  emit "${MP}runner_mem_held{repo=\"${repo}\"} ${held}"
  emit "${MP}runner_containers{repo=\"${repo}\"} ${running}"
  # the three series a page templates its kill switch from carry the GitHub repo and the variable
  sw="repo=\"${repo}\",gh_repo=\"${GHREPO[${repo}]}\",switch=\"${SWITCH[${repo}]}\""
  emit "${MP}runner_online{${sw}} ${ONLINE[${repo}]}"
  emit "${MP}runner_busy{repo=\"${repo}\"} ${BUSYN[${repo}]}"
  emit "${MP}runner_queued_jobs{repo=\"${repo}\"} ${QUEUED[${repo}]}"
  # runs the list holds past runs_read's RUN_PAGES: demand this tick did not count (an alert on it)
  emit "${MP}runner_runs_unread{repo=\"${repo}\",gh_repo=\"${GHREPO[${repo}]}\"} ${UNREADN[${repo}]:-0}"
  # runs only the unfiltered newest page held (the status filter dropped them); -1 = unread (an alert on the lag)
  emit "${MP}runner_runs_index_lag{repo=\"${repo}\",gh_repo=\"${GHREPO[${repo}]}\"} ${LAGN[${repo}]:--1}"
  emit "${MP}runner_reclaimed{repo=\"${repo}\"} ${reclaimed}"
  # epoch of this pool's last runner start (START_DIR; first look = now): the room-idle-while-queued
  # alert's "no start for 15m" term. A refill start this run shows next run (<= one tick late)
  ls_t="$(head -c 20 "${START_DIR}/${repo}" 2>/dev/null | tr -cd '0-9')"
  isnum "${ls_t}" && emit "${MP}runner_last_start_timestamp_seconds{repo=\"${repo}\"} ${ls_t}"
  emit "${MP}queue_oldest_seconds{${sw}} ${OLDEST[${repo}]}"
  emit "${MP}job_wait_oldest_seconds{${sw}} ${JOBWAIT[${repo}]}"
done
# declared peak_mb beside the measured working set (the peak-over-declared alert), for EVERY pool --
# a blind one too: the number is config, not something this tick had to see
# ...and its max_runners: the room-idle-while-queued alert must not page a pool sitting at its own cap
# ...and its young charge (an alert on the measured p95 past what young runners are charged)
while read -r repo pk mx yc; do
  emit "${MP}runner_peak_declared_bytes{repo=\"${repo}\"} $(( pk * 1024 * 1024 ))"
  isnum "${yc}" && emit "${MP}runner_young_declared_bytes{repo=\"${repo}\"} $(( yc * 1024 * 1024 ))"
  isnum "${mx}" && emit "${MP}runner_max_runners{repo=\"${repo}\"} ${mx}"
done < <(jq -r '.pools[] | "\(.repo) \(.peak_mb) \(.max) \(.young_mb // .peak_mb)"' "${CONFIG}")
# each pool's STARVE_S as the allocator uses it (300 unless the render's starve_s), so a page about a
# held or draining pool can be read against the threshold that made it drain
while read -r repo sv; do
  isnum "${sv}" && emit "${MP}runner_starve_seconds{repo=\"${repo}\"} ${sv}"
done < <(jq -r '.pools[] | "\(.repo) \(.starve_s // 300)"' "${CONFIG}")

# before the first publish: a series absent through the ~45s refill window would read as a gap
REDECIDE="$(jq -r 'if .redecide == false then 0 else 1 end' "${CONFIG}" 2>/dev/null)"
[ "${REDECIDE}" = 0 ] || REDECIDE=1
emit "${MP}runner_redecide_enabled ${REDECIDE}"

stop_sampler
publish

# REFILL LOOP. A runner takes ONE job and exits, so a slot stays empty until something starts
# its replacement. Refilling only from the full tick above (~20s of reaping, API reads and
# metrics, then the timer's gap) left a slot empty ~50s after every job. A repo whose jobs are a
# median 8s on 3 runners ran ~3 jobs per 50s, and a 10-job PR fan-out queued a median 82s,
# p90 258s per job, while a new runner took its job a median 5s after
# it started -- the wait was the refill, never GitHub. So the same run stays up for
# REFILL_WINDOW_S and re-counts every pool every REFILL_EVERY_S: ONE `docker ps` for all of them
# (~0.85s over the guest agent) and a GitHub call only when it starts a runner. Same process,
# so it cannot race the full tick; a crash here leaves the full-tick refill as the floor, and
# an alert on <prefix>job_wait_oldest_seconds says when jobs wait minutes for a slot. A pool whose
# mint/start fails is skipped for the rest of the run (no 3s retry storm against a GitHub outage
# or rate limit), and the failure republishes collect_ok 0 so the blind-supervisor alert still sees it.
# It tops each pool up to TARGET[repo] -- what the allocator granted THIS run -- never a static
# size: the sum of the targets fits max_total, so a refill only replaces a slot the budget already
# counted, and growing a pool past it waits for the next full tick's decision. A blind pool has
# no target and is left alone.
# A ZERO-FLOOR pool (min 0) refills only for DEMAND the full
# tick saw: its queued jobs, less the idle runners already there to take them, less the runners
# this run has started. Topping it back up to TARGET would replace the runner that just served its
# only job with an idle one that holds a budget slot until a reclaim or the idle reaper -- a dozen
# such pools would sit on the whole budget doing nothing. Floored pools keep
# refilling to TARGET: their follow-on jobs (`needs:` stages) land inside the window.
REFILL_WINDOW_S=45
REFILL_EVERY_S=3
# THE WINDOW STRETCHES WHILE KNOWN DEMAND IS UNSTARTED. Under a backlog the full
# tick's reads grew to 115-175s (ticks of ~3 min) while the
# window stayed 45s, so a runner was started in ~25% of the wall clock. A runner takes ONE job
# (often a median 8s) and exits, so for the other ~75% every slot that finished stayed
# empty: dozens of jobs queued with most of the VM's memory available, some
# jobs waited up to 26 min and each GitHub sample read 0-1 runners online (an ephemeral runner
# between two windows, not a failed registration). So past REFILL_WINDOW_S the window keeps
# going, to REFILL_WINDOW_MAX_S, while refill_more says some pool still has jobs the tick SAW
# that this run has not started a runner for. It ends as soon as none is left, so a quiet tick
# is as short as before and new demand is read as soon as before. The cost, decided: a pool
# whose demand appeared after the tick waits up to REFILL_WINDOW_MAX_S - REFILL_WINDOW_S longer
# for the next read, and a job cancelled after the tick can start one idle runner (the next
# tick reclaims it), as the re-decide already could. REFILL_WINDOW_MAX_S=REFILL_WINDOW_S restores
# the fixed window. The file is republished every REFILL_PUBLISH_S inside the window, so
# the staleness alert (mtime > 300s) still reads a live supervisor; each run's window length is
# <prefix>runner_refill_window_seconds.
REFILL_WINDOW_MAX_S=150
REFILL_PUBLISH_S=60
refill_more() { # -> 0 while a pool the tick could see has queued jobs this run started no runner for
  local repo
  for repo in ${REPOS}; do
    [ -n "${TARGET[${repo}]:-}" ] || continue
    case "${refill_failed}" in *" ${repo} "*) continue ;; esac
    [ $(( QUEUED[${repo}] - IDLEN[${repo}] - STARTED[${repo}] )) -gt 0 ] && return 0
  done
  return 1
}
#
# RE-DECIDE. The top-up above only restores TARGET, and while the allocator DRAINS
# every pool but the head has TARGET = its floor (0 for most): the refill started NOTHING
# (zero refill lines in 15 min of journal), so a 10 s job's runner ended and its
# room sat empty until the next full tick ~137 s later -- 68-78 s of reads, the 45 s window, the
# timer gap. Over a hundred jobs queued at ~2 starts/min on a VM mostly idle: decision latency, not
# memory, was the limit. So every pass also re-runs the allocator on what it can see without GitHub:
#   running = live containers now; busy = running, idle = 0 (idleness needs GitHub, and a reclaim
#   is never acted on here); queued = the tick's jobs less its idle runners less EVERY start since
#   (the zero-floor refill's own demand rule); oldest = the tick's job wait + elapsed (over-ages,
#   never under); since_start from ${RUN}; prev_head = this tick's head; live room = a fresh
#   MemAvailable less live_floor_mb less the young runners, exactly as the tick computes it.
# It acts on START only -- never a reclaim, never a cap or TARGET change, never the head file:
# the full tick still owns those. A start beyond TARGET is not topped up when it ends (the
# top-up compares against TARGET); the next re-decide decides again. Memory is the live read, so
# nothing is admitted the tick would not admit; queued only falls, so it cannot over-start past
# the jobs the tick saw (a job GitHub gave another runner meanwhile costs one idle runner, which
# the next tick reclaims). A blind live read (cap configured, read failed) skips the pass: the
# tick's decision stands. Kill switch: the render's redecide: false. Each re-decide that
# starts anything is traced (alloc_trace.jsonl, "refill": true); starts per pool in the last run are
# <prefix>runner_redecide_starts (skipped passes: <prefix>runner_redecide_skipped); an alert pages when room sits free while jobs
# wait, whatever the cause.
declare -A PASSN RDSTARTS
RDSKIP=0
redecide() { # live listing (PASSN = this pass's top-up starts) -> starts what the allocator grants
  local mem lf over rstate rplan el now_r repo n left o b rstart rdesired pk want=0
  [ "${REDECIDE}" = 1 ] || return 0
  # nothing left that the tick saw queued: no decision to make (and no guest call to pay for)
  for repo in ${REPOS}; do
    [ -n "${TARGET[${repo}]:-}" ] && [ $(( QUEUED[${repo}] - IDLEN[${repo}] - STARTED[${repo}] )) -gt 0 ] && want=1
  done
  [ "${want}" = 1 ] || return 0
  lf=null
  if isnum "${LIVE_FLOOR_MB}"; then
    # shellcheck disable=SC2016  # expands in the guest
    mem="$(guest 10 awk '/^MemAvailable:/ {print $2 * 1024}' /proc/meminfo)" || { RDSKIP=$(( RDSKIP + 1 )); return 0; }
    isnum "${mem}" || { RDSKIP=$(( RDSKIP + 1 )); return 0; }
    lf=$(( mem / 1048576 - LIVE_FLOOR_MB - $(young_of "$1") ))
    # the top-up's starts THIS pass are in neither the listing (taken before them) nor
    # MemAvailable yet (a container milliseconds old): charge each its pool's YOUNG CHARGE (the
    # runner is young: young_of charges it the same from the next listing on), or the re-decide
    # admits the same room twice (a reviewer's finding, reproduced in a stub harness)
    for repo in ${REPOS}; do
      [ "${PASSN[${repo}]:-0}" -gt 0 ] || continue
      pk="$(young_charge "${repo}")"
      lf=$(( lf - PASSN[${repo}] * pk ))
    done
  fi
  now_r=$(date +%s); el=$(( now_r - now ))
  over="{}"
  for repo in ${REPOS}; do
    [ -n "${TARGET[${repo}]:-}" ] || continue          # blind this tick: left as the tick saw it
    n=$(( $(names_of "${repo}" "$1" | grep -c . || true) + ${PASSN[${repo}]:-0} ))
    left=$(( QUEUED[${repo}] - IDLEN[${repo}] - STARTED[${repo}] )); [ "${left}" -gt 0 ] || left=0
    o=0; [ "${left}" -gt 0 ] && o=$(( JOBWAIT[${repo}] + el ))
    over="$(jq -c --arg r "${repo}" --argjson n "${n}" --argjson q "${left}" --argjson o "${o}" \
            --argjson ss "$(now=${now_r} since_start "${repo}")" \
            '. + {($r): {running: $n, busy: $n, idle: 0, queued: $q, oldest: $o, since_start: $ss}}' <<<"${over}")"
  done
  rstate="$(jq -c --argjson ov "${over}" 'map(. + ($ov[.repo] // {}))' <<<"${state}")" || return 0
  rplan="$(decide "${rstate}" "${new_head}" "${lf}")" \
    || { echo "ci-runner-supervisor: re-decide: allocator failed; re-decide off for this run" >&2
         REDECIDE=0; ok=0; return 0; }
  b=0
  while read -r repo rstart rdesired; do
    [ "${rstart}" -gt 0 ] || continue
    case "${refill_failed}" in *" ${repo} "*) continue ;; esac
    names="$(names_of "${repo}" "$1")"; running=$(( $(grep -c . <<<"${names}" || true) + ${PASSN[${repo}]:-0} ))
    before=${running}
    echo "ci-runner-supervisor: re-decide start ${repo}: ${running} +${rstart} (desired ${rdesired}, head ${new_head:-none})" >&2
    fill "${repo}" "$(( running + rstart ))" "${LABELS[${repo}]}" || refill_failed="${refill_failed}${repo} "
    STARTED[${repo}]=$(( STARTED[${repo}] + running - before ))
    PASSN[${repo}]=$(( ${PASSN[${repo}]:-0} + running - before ))
    RDSTARTS[${repo}]=$(( ${RDSTARTS[${repo}]:-0} + running - before ))
    [ "${running}" -gt "${before}" ] && { mark_start "${repo}"; b=1; }
  done < <(jq -r '.pools[] | "\(.repo) \(.start) \(.desired)"' <<<"${rplan}")
  # live_free_mb rides along: a heavy pool's starts are mostly re-decides, and without the live
  # room this line was handed the trace could not say what held the passes before one
  [ "${b}" = 1 ] && { jq -c --argjson ts "${now_r}" --arg ph "${new_head}" --argjson in "${rstate}" --argjson live "${lf}" \
      '{ts: $ts, refill: true, prev_head: $ph, live_free_mb: $live, in: $in, out: .}' <<<"${rplan}" >> "${TRACE}"; } 2>/dev/null
  return 0
}
refill_t0=$(date +%s)
refill_until=$(( refill_t0 + REFILL_WINDOW_S )); refill_max=$(( refill_t0 + REFILL_WINDOW_MAX_S ))
refill_pub=${refill_t0}
while refill_t=$(date +%s); [ "${refill_t}" -lt "${refill_until}" ] \
    || { [ "${refill_t}" -lt "${refill_max}" ] && refill_more; }; do
  # the tick's file again, so a stretched window never reads as a stopped supervisor
  [ $(( refill_t - refill_pub )) -ge "${REFILL_PUBLISH_S}" ] && { publish; refill_pub=${refill_t}; }
  sleep "${REFILL_EVERY_S}"
  raw="$(list_runners)" || continue
  note_sib "${raw}"
  live="$(runners_of "${raw}")"
  note_peaks "${live}"
  PASSN=()
  for repo in ${REPOS}; do
    target="${TARGET[${repo}]:-}"
    [ -n "${target}" ] || continue
    case "${refill_failed}" in *" ${repo} "*) continue ;; esac
    names="$(names_of "${repo}" "${live}")"
    running=$(grep -c . <<<"${names}" || true)
    if [ "${MINP[${repo}]}" = 0 ]; then
      left=$(( QUEUED[${repo}] - IDLEN[${repo}] - STARTED[${repo}] ))
      [ "${left}" -gt 0 ] || continue
      [ $(( running + left )) -lt "${target}" ] && target=$(( running + left ))
    fi
    [ "${running}" -lt "${target}" ] || continue
    before=${running}
    fill "${repo}" "${target}" "${LABELS[${repo}]}" || refill_failed="${refill_failed}${repo} "
    STARTED[${repo}]=$(( STARTED[${repo}] + running - before ))
    PASSN[${repo}]=$(( running - before ))
    [ "${running}" -gt "${before}" ] && mark_start "${repo}"
    echo "ci-runner-supervisor: refill ${repo}: ${before} -> ${running} of ${target}" >&2
  done
  redecide "${live}"
done
for repo in ${REPOS}; do
  [ -n "${TARGET[${repo}]:-}" ] && emit "${MP}runner_redecide_starts{repo=\"${repo}\"} ${RDSTARTS[${repo}]:-0}"
done
# how long this run's refill window stayed up (45s .. REFILL_WINDOW_MAX_S, longer = known demand left)
emit "${MP}runner_refill_window_seconds $(( $(date +%s) - refill_t0 ))"
# per-RUN gauges, published with the run's final file (absent during its refill window): starts the
# re-decide made, and passes it skipped because the live MemAvailable read failed
emit "${MP}runner_redecide_skipped ${RDSKIP}"
[ "${refill_failed}" != " " ] && ok=0
# again, always: the refill samples raised MEMPEAK, and a failed refill must read collect_ok 0
publish

[ "${ok}" = 1 ] || exit 2
exit 0
