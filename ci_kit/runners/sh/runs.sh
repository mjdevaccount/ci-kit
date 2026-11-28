# shellcheck shell=bash disable=SC2034  # RUNS UNREAD LAG queued_runs active_runs are outputs the callers read
# runs.sh -- ci-kit runners: the ONE reader of CI DEMAND for a self-hosted runner supervisor: which
# workflow runs are open, and which of their jobs wait for a runner a pool can serve. SOURCED, never
# run. Ships as ci-kit package data (ci_kit/runners/sh/); `ci-runners --write` copies it into a
# kit's scripts_dir.
#
# The caller provides OWNER and gh_get <path> (body on stdout; nonzero = could not read). Each
# supervisor keeps its own gh_get (its ETag dir, its rate-limit log); everything that decides what
# demand IS lives here, so two pools can never read it two ways.
#
# WHY ONE READER. A run can sit in `pending` for an hour while one of its jobs is `queued` for a
# runner: a concurrency-group wait whose predecessor finished the moment the job was created, after
# which GitHub never moves the RUN off `pending`. A reader of only ?status=queued and
# ?status=in_progress sees demand 0, starts nothing, and every wait gauge reads 0, so no queued-job
# alert can fire: a guard where it cannot fire. A run's status is not demand; its JOBS are. Every
# non-terminal run status is read, and the job filter (status queued + label match) decides. The
# callers run under `set -uo pipefail`.

# Every status a run holds before it completes (GitHub REST "list workflow runs", `status` filter).
# The filter takes ONE value per request, so each is its own conditional GET (a 304 is free against
# the PAT's rate limit). in_progress goes to active_runs; the rest are runs nothing has started yet.
OPEN_STATUSES="queued in_progress pending waiting requested"

# ONE READ PER URL PER TICK. Sibling pools of one repository (a default pool and its heavy or
# main-branch class) read the same run lists and the same per-run jobs: each was a request, and a
# 304 is free against the rate limit but not against the tick's clock (~0.3s each). memo_get keeps
# the body in MEMO for the rest of the tick. Never call it in a $( ): a subshell's MEMO dies with it.
declare -A MEMO=() RECENT_SLIM=()
memo_get() { # path -> MEMO[path]; nonzero = could not read (nothing memoized)
  local b
  [ -n "${MEMO[$1]+set}" ] && return 0
  b="$(gh_get "$1")" || return 1
  MEMO[$1]="${b}"
}

# EVERY RUN, NOT THE OLDEST 20. Reading demand from the 20 OLDEST queued+active runs per pool means
# that with 70 runs queued, a young run (a required gate on a fresh PR) is never seen, so its pool
# reads queued 0 and nothing starts for it -- a guard that could not see. runs_read follows the
# list's pages (GitHub returns newest first, 100 a page) up to RUN_PAGES and leaves ONE object
# {total_count, workflow_runs} in RUNS; UNREAD = runs the list holds past the pages read, for the
# caller to publish per repository, never a silent cap.
RUN_PAGES=3
runs_read() { # ghrepo status -> RUNS, UNREAD; nonzero = a page could not be read
  local p=1 path all="" total=0 n=0 got=0
  while :; do
    path="/repos/${OWNER}/$1/actions/runs?status=$2&per_page=100"
    [ "${p}" -gt 1 ] && path="${path}&page=${p}"
    memo_get "${path}" || return 1
    read -r total n < <(jq -r '"\(.total_count // 0) \(.workflow_runs | length)"' <<<"${MEMO[${path}]}") || return 1
    [ -n "${n}" ] || return 1
    all="${all}${MEMO[${path}]}"
    got=$((got + n))
    # a short page is the list's end, whatever total_count says: only the RUN_PAGES cap leaves
    # runs unread
    [ "${n}" -lt 100 ] && { total=${got}; break; }
    [ "${got}" -ge "${total}" ] || [ "${p}" -ge "${RUN_PAGES}" ] && break
    p=$((p + 1))
  done
  # a run that moved between pages mid-read shows twice: one id, one entry
  RUNS="$(jq -cs '{total_count: (.[0].total_count // 0), workflow_runs: ([.[].workflow_runs[]] | unique_by(.id))}' <<<"${all}")" || return 1
  UNREAD=$(( total > got ? total - got : 0 ))
}

# THE STATUS FILTER CAN DROP A RUN. A run can read `queued` in the plain list and in its own GET
# while every ?status= filter returns total_count 0: its pool reads demand 0, starts no runner, and
# a serial PR queue stalls behind it -- and the queued-job alerts read the same blind demand, so
# none fires. recent_merge reads one more list, the newest page UNFILTERED (a conditional GET,
# memo'd), and adds each OPEN run there (any of OPEN_STATUSES) that NO filtered list holds --
# in_progress to active_runs, the rest to queued_runs -- never a run a filter already holds, so an
# oldest-queued gauge sees exactly what it saw before plus the dropped runs. LAG = runs merged;
# -1 = the page could not be read or parsed, never 0, and demand is left as the filters read it.
# The page (over 1 MB for a busy repository) is cut to {id,status,created_at} of open runs ONCE per
# URL per tick in RECENT_SLIM, so sibling pools share the parse too.
recent_merge() { # ghrepo; reads/updates queued_runs + active_runs -> LAG
  local path="/repos/${OWNER}/$1/actions/runs?per_page=100" out n q a
  LAG=-1
  if [ -z "${RECENT_SLIM[${path}]+set}" ]; then
    memo_get "${path}" || return 0
    RECENT_SLIM[${path}]="$(jq -c --arg open "${OPEN_STATUSES}" '($open | split(" ")) as $o
      | [.workflow_runs[] | select(.status as $s | $o | index($s)) | {id, status, created_at}]' \
      <<<"${MEMO[${path}]}")" || { unset "RECENT_SLIM[${path}]"; return 0; }
  fi
  out="$(jq -cs '(.[1].workflow_runs + .[2].workflow_runs | map(.id)) as $have
    | [.[0][] | select(.id as $i | $have | index($i) | not)] as $add
    | [($add | length),
       (.[1] + {workflow_runs: (.[1].workflow_runs + [$add[] | select(.status != "in_progress")])}),
       (.[2] + {workflow_runs: (.[2].workflow_runs + [$add[] | select(.status == "in_progress")])})]' \
    <<<"${RECENT_SLIM[${path}]}${queued_runs}${active_runs}")" || return 0
  { read -r n && read -r q && read -r a; } < <(jq -c '.[]' <<<"${out}") || return 0
  queued_runs="${q}"; active_runs="${a}"; LAG="${n}"
}

# open_runs: every open run of a repository, each ONCE. queued_runs = runs not started (queued,
# pending, waiting, requested); active_runs = in_progress. A run read in two lists (it changed
# status between the reads) is kept in ONE: active wins, the newer truth. UNREAD sums every list's
# pages left unread; LAG is recent_merge's. Nonzero = some list could not be read: the lists still
# hold what WAS read, and the caller decides whether a partial view is blind (book the pool blind,
# or publish collect_ok 0 and still serve what it saw).
open_runs() { # ghrepo -> queued_runs, active_runs, UNREAD, LAG; nonzero = a list could not be read
  local s rc=0 unread=0 notyet=""
  queued_runs='{"total_count":0,"workflow_runs":[]}'; active_runs="${queued_runs}"
  for s in ${OPEN_STATUSES}; do
    if ! runs_read "$1" "${s}"; then rc=1; continue; fi
    unread=$((unread + UNREAD))
    if [ "${s}" = in_progress ]; then active_runs="${RUNS}"; else notyet="${notyet}${RUNS}"; fi
  done
  if [ -n "${notyet}" ]; then
    queued_runs="$(jq -cs '(.[0].workflow_runs | map(.id)) as $act | .[1:]
      | {total_count: (map(.total_count) | add),
         workflow_runs: ([.[].workflow_runs[]] | unique_by(.id) | map(select(.id as $i | $act | index($i) | not)))}' \
      <<<"${active_runs}${notyet}")" || { queued_runs='{"total_count":0,"workflow_runs":[]}'; rc=1; }
  fi
  UNREAD=${unread}
  recent_merge "$1"
  return ${rc}
}

# job_demand: a run's jobs list -> "<n> <wait>": n = jobs `queued` that carry NEED and whose every
# label the pool's set covers (a job asking for a label no pool carries is a workflow bug, not a
# slow pool), wait = seconds the oldest of them has waited (0 when none). Nonzero = unparseable.
job_demand() { # jobs-json pool-labels-json need now
  jq -r --argjson pl "$2" --arg need "$3" --argjson now "$4" \
    '[.jobs[] | select(.status == "queued" and (.labels | index($need)) and ((.labels - $pl) == [])) | .created_at | fromdate]
     | "\(length) \(if length == 0 then 0 else ($now - min) end)"' <<<"$1"
}
