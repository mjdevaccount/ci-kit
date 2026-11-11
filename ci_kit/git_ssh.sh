# git_ssh.sh -- install a project whose lockfile names a PRIVATE GitHub repo by https URL, on a
# runner, with a read-only (deploy) key. Three phases, three steps of the review gate's job:
#
#   bash git_ssh.sh load OUT    # env CI_KIT_GIT_SSH_KEY + CI_KIT_GIT_SSH_REPOS; no PR code runs here.
#                               # Starts an ssh-agent, adds the key from stdin, writes the pinned
#                               # host keys; appends sock= pid= known-hosts= to OUT ($GITHUB_OUTPUT).
#   . git_ssh.sh route          # SOURCED by the install step, which holds NO key: env
#                               # CI_KIT_GIT_SSH_REPOS + CI_KIT_GIT_SSH_KNOWN_HOSTS + SSH_AUTH_SOCK;
#                               # exports GIT_SSH_COMMAND and an env-scoped https->ssh rewrite.
#   bash git_ssh.sh kill PID    # `if: always()`, before any step that runs the PR's code.
#
# WHY THREE STEPS. The install runs the caller's build (backends, hooks, sdists): code the PR
# controls. A key in that step's env is readable from /proc/<step shell>/environ even after
# `unset` (review 2026-11-11), so the key lives only in `load`, where nothing of the PR runs. The
# install can USE the agent (the fetch needs it); the agent is started WITHOUT the key in its
# environment (`env -u`), so no /proc/<pid>/environ on the runner holds it, and it is killed
# before the Review step. webfactory/ssh-agent does `load` too, but its agent lives until the job's
# post step -- through Review, which runs the PR's tests -- so OpenSSH's own ssh-agent/ssh-add are
# called directly instead.
#
# ROOT IS NOT FENCED. While Install runs, the key is in the agent's MEMORY. Code with root can dump
# it (ptrace, /proc/<pid>/mem), and a hosted runner gives the `runner` user passwordless sudo. So
# "Install can use the key, never read it" holds only for code WITHOUT root. The fence is the
# key's scope: read-only, one repo. Never pass a key that can write, or that reads anything else.
#
# FAILS LOUD. Repos named with an EMPTY key (a fork or Dependabot PR gets no secrets; a caller
# secret never mapped) would otherwise surface three steps later as uv's "Permission denied
# (publickey)", which names nothing. A key with no repo named is a key nothing uses: also red.
#
# THE REWRITE IS A PREFIX MATCH (git insteadOf cannot anchor): `o/r` also routes `o/r-extra` to
# ssh, where the deploy key cannot read it -- red, never silent. Case must match the lockfile's URL.
#
# HOST KEYS are GitHub's PUBLISHED keys (`gh api meta --jq .ssh_keys`, re-read 2026-11-11),
# verbatim, in a file of our own with StrictHostKeyChecking=yes -- never ssh-keyscan, which trusts
# whatever answers, and never ~/.ssh (a self-hosted runner's HOME is not the job's). A rotation
# fails loud and is a one-line change here.
#
# A PUBLIC caller: setup-uv's cache then holds the private checkout, and a fork PR can restore the
# default branch's cache. A caller with a private dependency should itself be private.

_ci_kit_git_ssh_die() { echo "::error::git-ssh-key: $*"; exit 1; }

# the named repos as an array, never an unquoted word list: an entry like `*` is refused, not globbed
_ci_kit_git_ssh_parse() {
  mapfile -t _ci_kit_git_ssh_repos < <(printf '%s' "${CI_KIT_GIT_SSH_REPOS:-}" | tr -s ' \t\r\n,' '\n' | sed '/^$/d')
  _ci_kit_git_ssh_list="${_ci_kit_git_ssh_repos[*]}"
  local r
  for r in "${_ci_kit_git_ssh_repos[@]}"; do
    [[ "$r" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || _ci_kit_git_ssh_die "git-ssh-repos entry '$r' is not owner/name"
  done
}

_ci_kit_git_ssh_load() {
  local out="$1" kh err
  [ -n "$out" ] || _ci_kit_git_ssh_die "load needs the output file (\$GITHUB_OUTPUT)"
  _ci_kit_git_ssh_parse
  if [ -z "${CI_KIT_GIT_SSH_KEY:-}" ]; then
    [ -n "$_ci_kit_git_ssh_list" ] || _ci_kit_git_ssh_die "neither the key nor git-ssh-repos is set (the caller should not run this)"
    _ci_kit_git_ssh_die "the secret is EMPTY, so this run cannot install $_ci_kit_git_ssh_list (private). Secrets are withheld from fork and Dependabot PRs; on a same-repo branch the caller's secret is missing or not mapped onto git-ssh-key."
  fi
  [ -n "$_ci_kit_git_ssh_list" ] || _ci_kit_git_ssh_die "a key was passed but git-ssh-repos names no repo: nothing would use it. Name the owner/repo the lockfile installs."
  if ! command -v ssh-agent >/dev/null || ! command -v ssh-add >/dev/null; then
    _ci_kit_git_ssh_die "ssh-agent/ssh-add not on PATH (openssh-client): the runner image cannot load a key"
  fi
  kh="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/ci-kit-github-known_hosts"
  # OpenSSH reads UserKnownHostsFile as a whitespace-separated LIST: quoting cannot carry a space
  if [[ "$kh" =~ [[:space:]] ]]; then _ci_kit_git_ssh_die "the temp path '$kh' holds whitespace, which ssh would split"; fi
  cat > "$kh" <<'EOF'
github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=
github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk=
EOF
  # the agent must not inherit the key: its /proc/<pid>/environ outlives this step
  eval "$(env -u CI_KIT_GIT_SSH_KEY ssh-agent -s)" >/dev/null
  # a secret pasted on Windows carries CRs, which ssh-add rejects as an invalid format
  if ! err=$(printf '%s\n' "$CI_KIT_GIT_SSH_KEY" | tr -d '\r' | ssh-add -q - 2>&1); then
    ssh-agent -k >/dev/null 2>&1 || true
    _ci_kit_git_ssh_die "ssh-add refused the key: ${err:-no reason given} (an unencrypted OpenSSH/PEM private key is expected)"
  fi
  # an agent whose pid is not published is one the stop step cannot kill: it would live into Review
  if ! { echo "sock=$SSH_AUTH_SOCK"; echo "pid=$SSH_AGENT_PID"; echo "known-hosts=$kh"; } >> "$out"; then
    ssh-agent -k >/dev/null 2>&1 || true
    _ci_kit_git_ssh_die "could not write the agent's outputs to $out; the agent was stopped"
  fi
  echo "git-ssh-key: key loaded (agent $SSH_AGENT_PID) for: $_ci_kit_git_ssh_list"
}

_ci_kit_git_ssh_route() {
  local r n
  _ci_kit_git_ssh_parse
  [ -n "$_ci_kit_git_ssh_list" ] || _ci_kit_git_ssh_die "route: git-ssh-repos names no repo"
  if [ -z "${SSH_AUTH_SOCK:-}" ] || [ ! -S "$SSH_AUTH_SOCK" ]; then
    _ci_kit_git_ssh_die "route: no agent socket (SSH_AUTH_SOCK='${SSH_AUTH_SOCK:-}'): the load step did not run or its agent died"
  fi
  [ -s "${CI_KIT_GIT_SSH_KNOWN_HOSTS:-}" ] || _ci_kit_git_ssh_die "route: the pinned known_hosts file is missing"
  export GIT_SSH_COMMAND="ssh -o UserKnownHostsFile=$CI_KIT_GIT_SSH_KNOWN_HOSTS -o StrictHostKeyChecking=yes -o BatchMode=yes"
  # env-scoped git config (git >= 2.31), appended after any the caller already set: no --global,
  # so no later step inherits the rewrite
  n=${GIT_CONFIG_COUNT:-0}
  for r in "${_ci_kit_git_ssh_repos[@]}"; do
    export "GIT_CONFIG_KEY_$n=url.ssh://git@github.com/$r.insteadOf"
    export "GIT_CONFIG_VALUE_$n=https://github.com/$r"
    n=$((n + 1))
  done
  export GIT_CONFIG_COUNT=$n
  echo "git-ssh-key: https -> ssh for: $_ci_kit_git_ssh_list"
}

# alive = signalable AND not a zombie (kill -0 succeeds on a Z state, which holds no memory)
_ci_kit_git_ssh_alive() {
  local st=""
  kill -0 "$1" 2>/dev/null || return 1
  if [ -r "/proc/$1/stat" ]; then
    st=$(sed 's/.*) //' "/proc/$1/stat" 2>/dev/null | cut -d' ' -f1)
  elif command -v ps >/dev/null; then
    st=$(ps -o stat= -p "$1" 2>/dev/null || true)
  fi
  case "$st" in Z*) return 1 ;; esac
  return 0
}

_ci_kit_git_ssh_kill() {
  [[ "${1:-}" =~ ^[0-9]+$ ]] || _ci_kit_git_ssh_die "kill needs the agent pid"
  local i
  SSH_AGENT_PID="$1" ssh-agent -k >/dev/null 2>&1 || true
  # a signalled process is not gone the instant kill returns: give it 2s, then SIGKILL, 2s more
  for i in $(seq 40); do
    _ci_kit_git_ssh_alive "$1" || break
    [ "$i" = 20 ] && { kill -9 "$1" 2>/dev/null || true; }
    sleep 0.1
  done
  if _ci_kit_git_ssh_alive "$1"; then _ci_kit_git_ssh_die "agent $1 is still alive: the next step runs the PR's code"; fi
  echo "git-ssh-key: agent $1 stopped"
}

case "${1:-}" in
  load) _ci_kit_git_ssh_load "${2:-}" ;;
  route) _ci_kit_git_ssh_route ;;
  kill) _ci_kit_git_ssh_kill "${2:-}" ;;
  *) _ci_kit_git_ssh_die "usage: git_ssh.sh load OUT | . git_ssh.sh route | git_ssh.sh kill PID" ;;
esac
