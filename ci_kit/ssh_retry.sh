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
# read a byte of git's stdin and sent nothing upstream (measured with OpenSSH 10.3: a listener that
# never sends a banner, an unresolvable host and a dead port each exit 255 with stdin unread).
# Re-running is then the same as git calling ssh again. Matched are only OpenSSH's own
# pre-session lines, anchored (a mid-session "Read from remote host ...: Connection timed out" or
# an earlier "Control socket connect(...): Connection refused" warning does not match), and only
# when ssh exits 255, its "the connection failed" code. Auth refused, a host key mismatch, a
# session that dropped mid-transfer and the remote command's own exit are returned on the first
# try: those are not ours to retry.
#
# SEEN FROM OUTSIDE. uv swallows git's stderr when the fetch finally succeeds, so a retry that
# worked would otherwise leave no trace (review, 2026-11-28). Each retry and each give-up is
# written to stderr AND appended to $GITHUB_STEP_SUMMARY when the runner sets it, so a run that
# needed one says so on its summary page. Never stdout: that is git's protocol stream.
#
# BOUNDS. CI_KIT_SSH_TRIES (default 3) attempts; the pause before retry n is n x CI_KIT_SSH_PAUSE
# seconds (default 2: 2 s, then 4 s). With git_ssh.sh's ConnectTimeout=20, a host that is really
# down now costs a connection up to 66 s instead of 20 s.
set -u
tries="${CI_KIT_SSH_TRIES:-3}"
pause="${CI_KIT_SSH_PAUSE:-2}"
[[ "${tries}" =~ ^[1-9][0-9]?$ ]] || { echo "ssh-retry: CI_KIT_SSH_TRIES='${tries}' is not 1-99" >&2; exit 2; }
[[ "${pause}" =~ ^(0|[1-9][0-9]?)$ ]] || { echo "ssh-retry: CI_KIT_SSH_PAUSE='${pause}' is not 0-99" >&2; exit 2; }
# OpenSSH's connect-phase lines (sshconnect.c, ssh.c), anchored to the start of a line. ssh ends its
# stderr lines with CR LF (measured, OpenSSH 10.0p2: "exchange\r\n"), so the CRs are stripped before
# matching -- without that `$` never matched the banner line and the 11-28 failure was not retried.
NEVER_CONNECTED='^(ssh: connect to host .* port [0-9]+: .*|ssh: Could not resolve hostname .*|Connection timed out during banner exchange|kex_exchange_identification: .*)$'
err="$(mktemp)" || { echo "ssh-retry: mktemp failed" >&2; exit 2; }
trap 'rm -f "${err}"' EXIT
note() {
  echo "$1" >&2
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then printf -- '- %s\n' "$1" >> "${GITHUB_STEP_SUMMARY}" 2>/dev/null || true; fi
}
n=1
while :; do
  # ssh in the background with stdin passed EXPLICITLY (a bare `&` would give it /dev/null), so a
  # signal to this wrapper reaches ssh and ssh's stderr is still printed, never lost with the file
  # the trap is set BEFORE ssh starts: a signal in between still ends with its stderr printed
  pid=""
  trap 'if [ -n "${pid}" ]; then kill -TERM "${pid}" 2>/dev/null; wait "${pid}"; fi; cat "${err}" >&2; exit 143' TERM INT HUP
  ssh "$@" <&0 2>"${err}" &
  pid=$!
  wait "${pid}"
  rc=$?
  trap - TERM INT HUP
  cat "${err}" >&2
  [ "${rc}" = 255 ] || exit "${rc}"
  why="$(tr -d '\r' < "${err}" | grep -E "${NEVER_CONNECTED}" | head -n1)"
  [ -n "${why}" ] || exit "${rc}"
  if [ "${n}" -ge "${tries}" ]; then
    note "ssh-retry: gave up after ${n}/${tries} attempts, none connected (${why})"
    exit "${rc}"
  fi
  note "ssh-retry: attempt ${n}/${tries} never connected (${why}); retrying in $((pause * n))s"
  sleep $((pause * n))
  n=$((n + 1))
done
