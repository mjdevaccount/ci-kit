#!/usr/bin/env bash
# ssh_retry.sh -- `ssh "$@"`, re-run ONLY when the CONNECTION never came up. git_ssh.sh's route
# points GIT_SSH_COMMAND here.
#
# WHY. 2026-11-28 00:19Z, trade-data#3260's review gate: one ssh to github.com:22 got its TCP
# connection but no banner within ConnectTimeout ("Connection timed out during banner exchange").
# uv's git fetch died, and the gate went BLIND. A rerun six minutes later passed, and probes from
# the same runner host found 135 of 135 banners. One flaky connection should not blind a review.
#
# SAFE TO RE-RUN. A connect-phase failure ends before ssh opens a session channel, so it has not
# read a byte of git's stdin and sent nothing upstream. Re-running is then the same as git calling
# ssh again. Anything else (auth refused, a host key mismatch, a session that dropped mid-transfer,
# the remote command's own exit) is returned as-is on the first try: those are not ours to retry.
# The matched texts are OpenSSH's own pre-session errors. They are checked only when ssh exits 255,
# its "the connection failed" code.
#
# CI_KIT_SSH_TRIES (default 3) bounds the attempts; the pause before retry n is n x CI_KIT_SSH_PAUSE
# seconds (default 2: 2 s, then 4 s). Each retry prints one `ssh-retry:` line on stderr, so a run
# that needed one says so in its log.
set -u
tries="${CI_KIT_SSH_TRIES:-3}"
pause="${CI_KIT_SSH_PAUSE:-2}"
[[ "${tries}" =~ ^[1-9][0-9]?$ ]] || { echo "ssh-retry: CI_KIT_SSH_TRIES='${tries}' is not 1-99" >&2; exit 2; }
[[ "${pause}" =~ ^[0-9]{1,2}$ ]] || { echo "ssh-retry: CI_KIT_SSH_PAUSE='${pause}' is not 0-99" >&2; exit 2; }
# OpenSSH's connect-phase failures (ssh.c / sshconnect.c), never a session's: a dead peer mid-transfer
# is "Timeout, server not responding", a closed one "Connection to X closed by remote host".
NEVER_CONNECTED='banner exchange|kex_exchange_identification|Connection timed out|Connection refused|Network is unreachable|No route to host|Could not resolve hostname|Temporary failure in name resolution'
err="$(mktemp)" || { echo "ssh-retry: mktemp failed" >&2; exit 2; }
trap 'rm -f "${err}"' EXIT
n=1
while :; do
  ssh "$@" 2>"${err}"
  rc=$?
  cat "${err}" >&2
  [ "${rc}" = 255 ] && [ "${n}" -lt "${tries}" ] || exit "${rc}"
  grep -qE "${NEVER_CONNECTED}" "${err}" \
    || exit "${rc}"
  echo "ssh-retry: attempt ${n}/${tries} never connected ($(grep -oE "${NEVER_CONNECTED}" "${err}" | head -n1)); retrying in $((pause * n))s" >&2
  sleep $((pause * n))
  n=$((n + 1))
done
