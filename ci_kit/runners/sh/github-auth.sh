# shellcheck shell=bash
# github-auth.sh -- ci-kit runners: the ONE GitHub credential a runner supervisor uses. SOURCED,
# never run. Ships as ci-kit package data (ci_kit/runners/sh/); `ci-runners --write` copies it into
# a kit's scripts_dir, and every supervisor on a host sources the same copy.
#
# WHY AN APP. Supervisors that share one personal access token share its 5,000/h, which is also
# everything else its owner spends: one busy night and every pool goes blind at once. A GitHub App
# (actions:read, administration:write, metadata:read) has its OWN installation rate-limit bucket,
# scaling with the installation, separate from anything a person or another tool spends.
#
# WHAT WAS CHECKED FIRST (integrate before you write): `gh` cannot mint an installation token (no
# such subcommand, and a runner host need not carry gh); gh-token / go-gh-app would be one more
# binary to pin on a root host; PyJWT needs an interpreter and a package there. openssl (3.x)
# signs RS256 natively, so the JWT is three openssl pipes; the rest is two curl calls.
#
# INPUTS (environment, read once when sourced; the caller sets them before sourcing):
#   CI_RUNNER_CONF   host config dir: github-app.pem, github-app.id, github-app.installation,
#                    github.token                                   (default /etc/ci-runners)
#   CI_RUNNER_RUN    volatile dir: the token cache, the mint lock   (default /run/ci-runners)
#   OWNER            the account whose installation is used (else CI_RUNNER_OWNER; no default --
#                    an unset owner finds no installation and says so)
#   CI_RUNNER_METRIC_PREFIX   every gauge's prefix                  (default ci_)
#   CI_RUNNER_GH_API, CI_RUNNER_GH_AUTH_REFRESH_S                    (api.github.com, 600)
#   CI_RUNNER_AUTH_HINT  optional: the command that re-places the installation id; a stale id's
#                    log line ends "re-place it: <hint> <id>"
#
# gh_auth -> sets TOKEN + GH_AUTH_MODE (app|pat|none), GH_AUTH_CONFIGURED (1 = the App's key and id
# are on disk), GH_AUTH_EXPIRES (epoch, app only). Call it in the MAIN shell, never in a $( ).
#   APP when github-app.pem + github-app.id exist: the cached installation token while more than
#     the refresh margin (600 s) of it is left, else a fresh one -- JWT (RS256, iat now-30, exp
#     now+540, iss = App ID) -> POST /app/installations/<id>/access_tokens. The installation id is
#     github-app.installation, else GET /app/installations (the OWNER's), cached in the run dir.
#   PAT (github.token) on ANY app failure -- LOUDLY: one stderr line naming the reason, and
#     <prefix>github_auth{mode="pat"} 1, for an alert on the PAT while <prefix>github_app_configured
#     is 1.
# gh_auth_fresh [retry] -> re-runs gh_auth only when an app token is inside its refresh margin,
#   keeping the old token if that fails: for a caller that outlives a token (see the function).
# gh_auth_emit -> the auth gauges, through the caller's emit.
# SECRETS: no key, JWT or token is ever printed, logged or put on argv (the bearer rides a process
# substitution). Logs carry the MODE and a sha256 prefix of a minted token only. The cache file is
# 0600 under a 077 umask; the mint is serialised by flock across supervisors.

GH_AUTH_CONF="${CI_RUNNER_CONF:-/etc/ci-runners}"
GH_AUTH_RUN="${CI_RUNNER_RUN:-/run/ci-runners}"
GH_AUTH_API="${CI_RUNNER_GH_API:-https://api.github.com}"
GH_AUTH_REFRESH_S="${CI_RUNNER_GH_AUTH_REFRESH_S:-600}"
GH_AUTH_OWNER="${OWNER:-${CI_RUNNER_OWNER:-}}"
GH_AUTH_PREFIX="${CI_RUNNER_METRIC_PREFIX:-ci_}"
GH_AUTH_HINT="${CI_RUNNER_AUTH_HINT:-}"
GH_AUTH_MODE=none; GH_AUTH_CONFIGURED=0; GH_AUTH_EXPIRES=0; GH_AUTH_REASON=""

_gh_auth_b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

_gh_auth_jwt() { # app_id pem -> the App's JWT on stdout; nonzero = could not sign
  local now h p s
  now=$(date +%s)
  h=$(printf '%s' '{"alg":"RS256","typ":"JWT"}' | _gh_auth_b64url)
  p=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' $(( now - 30 )) $(( now + 540 )) "$1" | _gh_auth_b64url)
  s=$(printf '%s.%s' "${h}" "${p}" | openssl dgst -sha256 -sign "$2" -binary 2>/dev/null | _gh_auth_b64url)
  [ -n "${s}" ] || return 1
  printf '%s.%s.%s' "${h}" "${p}" "${s}"
}

_gh_auth_call() { # method path bearer out -> HTTP code on stdout ("000" = no response)
  local code
  code=$(curl -sS --connect-timeout 5 --max-time 20 -X "$1" -o "$4" -w '%{http_code}' \
    -H @<(printf 'Authorization: Bearer %s\n' "$3") -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" "${GH_AUTH_API}$2" 2>/dev/null) || code=000
  printf '%s' "${code:-000}"
}

_gh_auth_cached() { # -> 0 and TOKEN/GH_AUTH_EXPIRES set when the cache holds a token past the margin
  local exp tok
  { IFS= read -r exp; IFS= read -r tok; } 2>/dev/null < "${GH_AUTH_RUN}/github-app.token" || return 1
  [[ "${exp}" =~ ^[0-9]+$ ]] && [ -n "${tok}" ] && [ $(( exp - $(date +%s) )) -gt "${GH_AUTH_REFRESH_S}" ] || return 1
  TOKEN="${tok}"; GH_AUTH_EXPIRES="${exp}"
}

_gh_auth_lookup() { # jwt out -> the OWNER's installation id on stdout (cached in the run dir); nonzero = none
  local code inst
  code=$( umask 077; _gh_auth_call GET /app/installations "$1" "$2" )
  inst=$(jq -r --arg o "${GH_AUTH_OWNER}" '[.[]? | select(.account.login == $o) | .id][0] // empty' "$2" 2>/dev/null)
  rm -f "$2"
  [[ "${inst}" =~ ^[0-9]+$ ]] || { echo "ci-github-auth: GET /app/installations: HTTP ${code}, no installation for ${GH_AUTH_OWNER}" >&2; return 1; }
  ( umask 077; printf '%s\n' "${inst}" > "${GH_AUTH_RUN}/github-app.installation" )
  printf '%s' "${inst}"
}

_gh_auth_mint() { # jwt inst out -> 0 with TOKEN/GH_AUTH_EXPIRES set and cached; else GH_AUTH_CODE + nonzero
  local tok exp
  GH_AUTH_CODE=$( umask 077; _gh_auth_call POST "/app/installations/$2/access_tokens" "$1" "$3" )
  tok=$(jq -r '.token // empty' "$3" 2>/dev/null)
  exp=$(jq -r '.expires_at // empty' "$3" 2>/dev/null)
  rm -f "$3"
  [ -n "${exp}" ] && exp=$(date -u -d "${exp}" +%s 2>/dev/null)
  [ "${GH_AUTH_CODE}" = 201 ] && [ -n "${tok}" ] && [[ "${exp}" =~ ^[0-9]+$ ]] || return 1
  ( umask 077; printf '%s\n%s\n' "${exp}" "${tok}" > "${GH_AUTH_RUN}/github-app.token.new" ) \
    && mv -f "${GH_AUTH_RUN}/github-app.token.new" "${GH_AUTH_RUN}/github-app.token"
  echo "ci-github-auth: minted installation $2 token sha256:$(printf '%s' "${tok}" | sha256sum | cut -c1-8), $(( exp - $(date +%s) ))s left" >&2
  TOKEN="${tok}"; GH_AUTH_EXPIRES="${exp}"
}

_gh_auth_app() { # -> 0 with TOKEN set from the App, else nonzero with GH_AUTH_REASON
  local id pem inst jwt out fd rc=1 looked=0
  pem="${GH_AUTH_CONF}/github-app.pem"
  id=$(cat "${GH_AUTH_CONF}/github-app.id" 2>/dev/null)
  [[ "${id}" =~ ^[0-9]+$ ]] || { GH_AUTH_REASON="github-app.id is not a number"; return 1; }
  [ -r "${pem}" ] || { GH_AUTH_REASON="github-app.pem unreadable"; return 1; }
  ( umask 077; mkdir -p "${GH_AUTH_RUN}" ) || { GH_AUTH_REASON="cannot create ${GH_AUTH_RUN}"; return 1; }
  _gh_auth_cached && return 0
  # ONE mint at a time across every supervisor on the host; the loser re-reads the winner's cache
  exec {fd}>"${GH_AUTH_RUN}/github-app.lock" || { GH_AUTH_REASON="cannot open the mint lock"; return 1; }
  if ! flock -w 25 "${fd}"; then exec {fd}>&-; GH_AUTH_REASON="mint lock held 25s"; return 1; fi
  if _gh_auth_cached; then exec {fd}>&-; return 0; fi
  out="${GH_AUTH_RUN}/github-app.resp.$$"
  if ! jwt=$(_gh_auth_jwt "${id}" "${pem}"); then
    GH_AUTH_REASON="JWT signing failed (openssl, github-app.pem)"
  else
    # the installation: the file the installer placed, else the run dir's last lookup, else a lookup
    inst=$(cat "${GH_AUTH_CONF}/github-app.installation" 2>/dev/null)
    [[ "${inst}" =~ ^[0-9]+$ ]] || inst=$(cat "${GH_AUTH_RUN}/github-app.installation" 2>/dev/null)
    [[ "${inst}" =~ ^[0-9]+$ ]] || { inst=$(_gh_auth_lookup "${jwt}" "${out}") && looked=1; }
    # (a $( ) loses the lookup's own reason: name it here)
    [[ "${inst}" =~ ^[0-9]+$ ]] || GH_AUTH_REASON="no installation for ${GH_AUTH_OWNER} (GET /app/installations)"
    if [[ "${inst}" =~ ^[0-9]+$ ]]; then
      if _gh_auth_mint "${jwt}" "${inst}" "${out}"; then rc=0
      elif [ "${GH_AUTH_CODE}" = 404 ] && [ "${looked}" = 0 ]; then
        # the App was reinstalled under a new id (or removed): the id on disk is stale. Ask GitHub
        # which installation is the OWNER's now, and SAY the file is wrong -- it stays wrong until
        # it is re-placed (each re-mint pays this one lookup).
        rm -f "${GH_AUTH_RUN}/github-app.installation"
        local was="${inst}"
        if inst=$(_gh_auth_lookup "${jwt}" "${out}"); then
          echo "ci-github-auth: installation ${was} is gone (404); GitHub says ${GH_AUTH_OWNER}'s is ${inst} -- re-place it${GH_AUTH_HINT:+: ${GH_AUTH_HINT} ${inst}}" >&2
          _gh_auth_mint "${jwt}" "${inst}" "${out}" && rc=0
        fi
      fi
      [ "${rc}" = 0 ] || [ -n "${GH_AUTH_REASON}" ] || GH_AUTH_REASON="installation ${inst} token mint refused (HTTP ${GH_AUTH_CODE})"
      [ "${rc}" = 0 ] || [ "${GH_AUTH_CODE:-}" != 404 ] || rm -f "${GH_AUTH_RUN}/github-app.installation"
    fi
  fi
  exec {fd}>&-
  return "${rc}"
}

gh_auth() {
  GH_AUTH_EXPIRES=0; GH_AUTH_REASON=""; GH_AUTH_CODE=""
  GH_AUTH_CONFIGURED=0
  [ -e "${GH_AUTH_CONF}/github-app.pem" ] && [ -e "${GH_AUTH_CONF}/github-app.id" ] && GH_AUTH_CONFIGURED=1
  if [ "${GH_AUTH_CONFIGURED}" = 1 ] && _gh_auth_app; then GH_AUTH_MODE=app; return 0; fi
  [ "${GH_AUTH_CONFIGURED}" = 1 ] || GH_AUTH_REASON="no GitHub App configured (${GH_AUTH_CONF}/github-app.pem + .id)"
  TOKEN=$(cat "${GH_AUTH_CONF}/github.token" 2>/dev/null)
  if [ -n "${TOKEN}" ]; then GH_AUTH_MODE=pat; else GH_AUTH_MODE=none; fi
  echo "ci-github-auth: mode=${GH_AUTH_MODE} -- ${GH_AUTH_REASON}" >&2
  [ -n "${TOKEN}" ]
}

# gh_auth_fresh [retry]: before a call in a caller that may outlive its token. An app token inside
# its margin is re-read/re-minted; a FAILED refresh keeps the old token while it is still valid (a
# lock wait or a network blip must not drop a working token for the PAT -- or, once the PAT is
# revoked, for nothing). `retry` (a main-shell caller only, e.g. one job's long-lived client) also
# retries the App once a minute after a fallback, so a long job is not stuck on the PAT by one bad
# mint.
gh_auth_fresh() {
  local now old_tok old_exp
  now=$(date +%s)
  if [ "${GH_AUTH_MODE}" = app ]; then
    [ $(( GH_AUTH_EXPIRES - now )) -le "${GH_AUTH_REFRESH_S}" ] || return 0
    old_tok="${TOKEN}"; old_exp="${GH_AUTH_EXPIRES}"
    gh_auth && [ "${GH_AUTH_MODE}" = app ] && return 0
    if [ $(( old_exp - now )) -gt 60 ]; then
      TOKEN="${old_tok}"; GH_AUTH_EXPIRES="${old_exp}"; GH_AUTH_MODE=app
      echo "ci-github-auth: refresh failed, keeping the app token ($(( old_exp - now ))s left)" >&2
    fi
  elif [ "${1:-}" = retry ] && [ "${GH_AUTH_CONFIGURED}" = 1 ] && [ $(( now - ${GH_AUTH_TRIED:-0} )) -ge 60 ]; then
    GH_AUTH_TRIED="${now}"
    old_tok="${TOKEN}"
    gh_auth || TOKEN="${old_tok}"
    [ -n "${TOKEN}" ] && [ "${GH_AUTH_MODE}" = none ] && GH_AUTH_MODE=pat
  fi
  return 0
}

gh_auth_emit() {
  local m
  for m in app pat; do emit "${GH_AUTH_PREFIX}github_auth{mode=\"${m}\"} $([ "${GH_AUTH_MODE}" = "${m}" ] && echo 1 || echo 0)"; done
  emit "${GH_AUTH_PREFIX}github_app_configured ${GH_AUTH_CONFIGURED}"
  [ "${GH_AUTH_MODE}" = app ] && emit "${GH_AUTH_PREFIX}github_app_token_expiry_timestamp_seconds ${GH_AUTH_EXPIRES}"
  return 0
}
