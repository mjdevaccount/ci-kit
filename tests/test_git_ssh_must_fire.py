"""ci_kit/git_ssh.sh -- the review gate's private-git-dependency loader -- must fire, both directions.

Driven for REAL, phase by phase as the workflow's three steps run it: `load` in its own bash with a
throwaway ed25519 key from ssh-keygen and a real ssh-agent; `. route` sourced by a SEPARATE bash
that never had the key (the install); `kill`. RED: repos named with an EMPTY key (a fork or
Dependabot PR, a secret never mapped) stops the load step loudly, so the install never runs --
never a silent skip into uv's "Permission denied (publickey)"; a key with no repo, a malformed repo,
a key ssh-add refuses, a route with no live agent: all red. GREEN: the install shell can use the
agent but holds no key, only the named repos route to ssh through the pinned host keys, and kill
leaves no agent for the PR's tests. Plus the workflow wiring: the secret reaches the load step and
nothing else, and a caller passing neither runs Install as before.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import time

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "ci_kit" / "git_ssh.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "review-gate.yml"
TOOLS = ("bash", "git", "ssh-agent", "ssh-add", "ssh-keygen")
MISSING = [t for t in TOOLS if shutil.which(t) is None]
# by full path: on Windows, CreateProcess finds System32\bash.exe (WSL) before PATH
BASH = shutil.which("bash") or "bash"
# CI must never skip this suite into a blind pass: the hosted image has every tool
needs_tools = pytest.mark.skipif(bool(MISSING) and not os.environ.get("CI"),
                                 reason=f"not on PATH: {MISSING}")
GITHUB_KEYS = {  # `gh api meta --jq .ssh_keys`, 2026-11-11: the script's pins, checked by VALUE
    "ssh-ed25519": "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl",
    "ecdsa-sha2-nistp256": ("AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5"
                            "wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg="),
}


def _keypair(tmp: pathlib.Path) -> tuple[str, str]:
    key = tmp / "id_ed25519"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ci-kit-test", "-f", str(key)],
                   check=True)
    fp = subprocess.run(["ssh-keygen", "-lf", str(key) + ".pub"], check=True, capture_output=True,
                        text=True).stdout.split()[1]
    return key.read_text(), fp


def _env(tmp: pathlib.Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_CONFIG_") and k not in ("SSH_AUTH_SOCK", "SSH_AGENT_PID")}
    return {**env, "RUNNER_TEMP": tmp.as_posix(), "GIT_TERMINAL_PROMPT": "0", **extra}


def _bash(tmp: pathlib.Path, body: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([BASH, "-c", "set -euo pipefail\n" + body], env=_env(tmp, **env), cwd=tmp,
                          capture_output=True, text=True, timeout=60)


def _load(tmp: pathlib.Path, key: str, repos: str) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    out = tmp / "gh-output"
    out.write_text("")
    r = _bash(tmp, f'bash "{SCRIPT.as_posix()}" load "{out.as_posix()}"',
              CI_KIT_GIT_SSH_KEY=key, CI_KIT_GIT_SSH_REPOS=repos)
    outputs = dict(ln.split("=", 1) for ln in out.read_text().splitlines() if "=" in ln)
    return r, outputs


def _install(tmp: pathlib.Path, outputs: dict[str, str], repos: str, after: str) -> subprocess.CompletedProcess[str]:
    """The Install step's shape: a fresh shell with no key, the socket, `. route`, the install."""
    return _bash(tmp, f'. "{SCRIPT.as_posix()}" route\n{after}\n', SSH_AUTH_SOCK=outputs.get("sock", ""),
                 CI_KIT_GIT_SSH_KNOWN_HOSTS=outputs.get("known-hosts", ""), CI_KIT_GIT_SSH_REPOS=repos)


def _kill(tmp: pathlib.Path, pid: str) -> subprocess.CompletedProcess[str]:
    return _bash(tmp, f'bash "{SCRIPT.as_posix()}" kill "{pid}"')


def _alive(pid: str) -> bool:
    return subprocess.run([BASH, "-c", f"kill -0 {pid} 2>/dev/null"]).returncode == 0


# ---- RED: each one stops the load step, so the install never runs --------------------------

@needs_tools
def test_empty_key_with_repos_named_fails_loud(tmp_path: pathlib.Path) -> None:
    r, outputs = _load(tmp_path, key="", repos="o/private")
    assert r.returncode == 1
    assert "::error::git-ssh-key: the secret is EMPTY" in r.stdout and "o/private" in r.stdout
    assert outputs == {}, "an empty key published an agent: Install would route to nothing"


@needs_tools
def test_whitespace_key_fails(tmp_path: pathlib.Path) -> None:
    r, outputs = _load(tmp_path, key="  \n", repos="  \n o/private \n")
    assert r.returncode == 1 and "::error::git-ssh-key:" in r.stdout and outputs == {}


@needs_tools
def test_key_with_no_repo_fails(tmp_path: pathlib.Path) -> None:
    key, _ = _keypair(tmp_path)
    r, outputs = _load(tmp_path, key=key, repos="")
    assert r.returncode == 1 and "names no repo" in r.stdout and outputs == {}


@needs_tools
def test_malformed_repo_fails(tmp_path: pathlib.Path) -> None:
    key, _ = _keypair(tmp_path)
    for bad in ("market-ports", "https://github.com/o/r", "o/r/x", "o/r;id", "*", "o/*"):
        r, outputs = _load(tmp_path, key=key, repos=bad)
        assert r.returncode == 1 and "is not owner/name" in r.stdout and outputs == {}, bad


@needs_tools
def test_key_ssh_add_refuses_fails(tmp_path: pathlib.Path) -> None:
    r, outputs = _load(tmp_path, key="not a key", repos="o/r")
    assert r.returncode == 1 and "ssh-add refused the key:" in r.stdout and outputs == {}


@needs_tools
def test_route_with_no_live_agent_fails(tmp_path: pathlib.Path) -> None:
    kh = tmp_path / "kh"
    kh.write_text("github.com x y\n")
    r = _install(tmp_path, {"sock": (tmp_path / "gone.sock").as_posix(), "known-hosts": kh.as_posix()},
                 "o/r", "touch installed")
    assert r.returncode == 1 and "no agent socket" in r.stdout
    assert not (tmp_path / "installed").exists()


# ---- GREEN: the install uses the agent, holds no key, and the agent dies before Review -------

@needs_tools
def test_the_three_phases(tmp_path: pathlib.Path) -> None:
    key, fp = _keypair(tmp_path)
    # a Windows-pasted secret (CRLF) must load too
    r, outputs = _load(tmp_path, key=key.replace("\n", "\r\n"), repos="o/private\no/second")
    assert r.returncode == 0, r.stdout + r.stderr
    assert set(outputs) == {"sock", "pid", "known-hosts"}
    assert "PRIVATE KEY" not in r.stdout + r.stderr
    try:
        i = _install(tmp_path, outputs, "o/private\no/second", "\n".join([
            "ssh-add -l > agent.txt",
            "env > install-env.txt",
            "git ls-remote --get-url https://github.com/o/private > named.txt",
            "git ls-remote --get-url https://github.com/o/second.git > second.txt",
            "git ls-remote --get-url https://github.com/o/public > unnamed.txt",
            'printf "%s" "$GIT_SSH_COMMAND" > sshcmd.txt',
        ]))
        assert i.returncode == 0, i.stdout + i.stderr
        assert fp in (tmp_path / "agent.txt").read_text(), "the install cannot use the key"
        seen = (tmp_path / "install-env.txt").read_text(errors="replace")
        assert "PRIVATE KEY" not in seen and "CI_KIT_GIT_SSH_KEY" not in seen
        assert (tmp_path / "named.txt").read_text().strip() == "ssh://git@github.com/o/private"
        assert (tmp_path / "second.txt").read_text().strip() == "ssh://git@github.com/o/second.git"
        assert (tmp_path / "unnamed.txt").read_text().strip() == "https://github.com/o/public"
        cmd = (tmp_path / "sshcmd.txt").read_text()
        assert f"UserKnownHostsFile={outputs['known-hosts']} " in cmd
        assert "StrictHostKeyChecking=yes" in cmd and "BatchMode=yes" in cmd
        lines = pathlib.Path(outputs["known-hosts"]).read_text().splitlines()
        pinned = {ln.split()[1]: ln.split()[2] for ln in lines}
        assert all(pinned.get(t) == v for t, v in GITHUB_KEYS.items()) and "ssh-rsa" in pinned
        assert all(ln.startswith("github.com ") for ln in lines)
    finally:
        k = _kill(tmp_path, outputs["pid"])
    assert k.returncode == 0, k.stdout + k.stderr
    assert not _alive(outputs["pid"]), "the agent outlived the kill step: the PR's tests could use the key"


def _proc_environ(pid: str) -> bytes | None:
    """The agent's ORIGINAL environment, as root sees it. ssh-agent makes itself non-dumpable, so
    its own user cannot read it: direct read, else `sudo -n` (a hosted runner has it), else None."""
    path = pathlib.Path(f"/proc/{pid}/environ")
    if not path.parent.is_dir():
        return None
    try:
        return path.read_bytes()
    except PermissionError:
        if shutil.which("sudo") is None:
            return None
        r = subprocess.run(["sudo", "-n", "cat", str(path)], capture_output=True)
        return r.stdout if r.returncode == 0 else None


@needs_tools
def test_the_agent_does_not_inherit_the_key(tmp_path: pathlib.Path) -> None:
    # the load step's env holds the key; an agent started from it would keep it in its own
    # /proc/<pid>/environ, readable by root (= `sudo` on a hosted runner) for the whole Install
    key, _ = _keypair(tmp_path)
    r, outputs = _load(tmp_path, key=key, repos="o/private")
    assert r.returncode == 0, r.stdout + r.stderr
    try:
        if not pathlib.Path(f"/proc/{outputs['pid']}").is_dir():
            pytest.skip("no /proc for the agent's pid (not Linux)")
        seen = _proc_environ(outputs["pid"])
        if seen is None:
            pytest.skip("the agent's environ needs root (ssh-agent is non-dumpable) and `sudo -n` is unavailable")
        body = key.splitlines()[1].encode()
        assert b"CI_KIT_GIT_SSH_KEY" not in seen and body not in seen, "the agent inherited the key"
    finally:
        _kill(tmp_path, outputs["pid"])


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="a zombie's state is read from /proc")
def test_kill_counts_a_zombie_as_dead(tmp_path: pathlib.Path) -> None:
    # kill -0 succeeds on a zombie; one that is never reaped (its parent never waits) must not
    # fail the stop step forever. This child stays a zombie of pytest until wait() below.
    child = subprocess.Popen(["true"])
    try:
        deadline = time.monotonic() + 5
        while pathlib.Path(f"/proc/{child.pid}/stat").read_text().rsplit(") ", 1)[1][0] != "Z":
            assert time.monotonic() < deadline, "the child never became a zombie"
            time.sleep(0.05)
        k = _kill(tmp_path, str(child.pid))
        assert k.returncode == 0, k.stdout + k.stderr
    finally:
        child.wait()


@needs_tools
def test_git_fetch_goes_over_ssh_to_github(tmp_path: pathlib.Path) -> None:
    # git's real transport: an https URL of a named repo is fetched with ssh as git@github.com
    key, _ = _keypair(tmp_path)
    r, outputs = _load(tmp_path, key=key, repos="o/private")
    assert r.returncode == 0, r.stdout + r.stderr
    rec = tmp_path / "ssh-recorder"
    rec.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$(dirname "$0")/ssh-argv.txt"\nexit 1\n')
    rec.chmod(0o755)
    try:
        i = _install(tmp_path, outputs, "o/private",
                     f'GIT_SSH_COMMAND="{rec.as_posix()}" git ls-remote https://github.com/o/private || true')
        assert i.returncode == 0, i.stdout + i.stderr
        argv = (tmp_path / "ssh-argv.txt").read_text()
        assert "git@github.com" in argv and "o/private" in argv
    finally:
        _kill(tmp_path, outputs["pid"])


# ---- the workflow wiring --------------------------------------------------------------------

LOAD, INSTALL, STOP = "Load the git ssh key", "Install", "Stop the git ssh agent"


def _review_steps() -> list[dict]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["review"]["steps"]


def _named(steps: list[dict], name: str) -> dict:
    return next((s for s in steps if s.get("name") == name), {})


def wiring_faults(steps: list[dict]) -> list[str]:
    out = []
    holders = [s.get("name", s.get("uses", "?")) for s in steps if "secrets.git-ssh-key" in yaml.safe_dump(s)]
    if holders != [LOAD]:
        out.append(f"secrets.git-ssh-key reaches {holders}, not the load step alone")
    names = [s.get("name") for s in steps]
    missing = [n for n in (LOAD, INSTALL, STOP, "Review") if n not in names]
    if missing:
        return [*out, f"steps missing: {missing}"]
    if not names.index(LOAD) < names.index(INSTALL) < names.index(STOP) < names.index("Review"):
        out.append("the order is not load -> Install -> stop -> Review")
    load = _named(steps, LOAD)
    if "$INSTALL" in load.get("run", ""):
        out.append("the load step (which holds the key) runs the caller's install")
    if 'if [ -z "$CI_KIT_GIT_SSH_REPOS" ] && [ -z "$CI_KIT_GIT_SSH_KEY" ]; then exit 0; fi' not in load.get("run", ""):
        out.append("the load step does not run when only one of the repos or the key is set")
    if load.get("if") != _named(steps, INSTALL).get("if"):
        out.append("the load step's `if` differs from Install's")
    run = _named(steps, INSTALL).get("run", "")
    if '. "$KIT/ci_kit/git_ssh.sh" route' not in run or run.index("git_ssh.sh") > run.index('eval "$INSTALL"'):
        out.append("Install does not source `route` before the install")
    if not str(_named(steps, STOP).get("if", "")).startswith("always()"):
        out.append("the stop step is not always(): a failed install leaves the agent to Review")
    if _named(steps, INSTALL).get("env", {}).get("CI_KIT_GIT_SSH_CONFIG") != "${{ steps.gitssh.outputs.ssh-config }}":
        out.append("Install does not get the load step's ssh-config: one key per repo would route nothing")
    return out


def test_shipped_wiring_is_clean() -> None:
    assert wiring_faults(_review_steps()) == []
    call = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))[True]["workflow_call"]  # `on:` reads as True
    assert call["inputs"]["git-ssh-repos"]["default"] == ""
    assert call["secrets"]["git-ssh-key"]["required"] is False


def test_fires_when_the_key_reaches_install() -> None:
    steps = _review_steps()
    _named(steps, INSTALL)["env"]["CI_KIT_GIT_SSH_KEY"] = "${{ secrets.git-ssh-key }}"
    assert any("not the load step alone" in f for f in wiring_faults(steps))


def test_fires_when_install_lacks_the_ssh_config() -> None:
    steps = _review_steps()
    del _named(steps, INSTALL)["env"]["CI_KIT_GIT_SSH_CONFIG"]
    assert any("ssh-config" in f for f in wiring_faults(steps))


def test_fires_when_the_stop_step_is_not_always() -> None:
    steps = _review_steps()
    _named(steps, STOP)["if"] = "steps.gitssh.outputs.pid != ''"
    assert any("not always()" in f for f in wiring_faults(steps))


def test_fires_when_the_load_ignores_a_key_without_repos() -> None:
    steps = _review_steps()
    load = _named(steps, LOAD)
    load["run"] = load["run"].replace(' && [ -z "$CI_KIT_GIT_SSH_KEY" ]', "")
    assert wiring_faults(steps)


@needs_tools
def test_unset_caller_runs_load_and_install_unchanged(tmp_path: pathlib.Path) -> None:
    # neither input, and NO git_ssh.sh in the kit (a kit-ref older than 1.24.0): the load step
    # exits 0 at its first line and Install runs the install exactly as before
    steps = _review_steps()
    kit = tmp_path / "kit"
    (kit / "ci_kit").mkdir(parents=True)
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "uv").write_text("#!/bin/sh\nexit 0\n")
    (stub / "uv").chmod(0o755)
    out = tmp_path / "gh-output"
    out.write_text("")
    env = {**os.environ, "KIT": kit.as_posix(), "INSTALL": "touch installed", "GITHUB_OUTPUT": out.as_posix(),
           "CI_KIT_GIT_SSH_REPOS": "", "CI_KIT_GIT_SSH_KEY": "", "CI_KIT_GIT_SSH_SOCK": "",
           "CI_KIT_GIT_SSH_KNOWN_HOSTS": "", "PATH": stub.as_posix() + os.pathsep + os.environ["PATH"]}
    for name in (LOAD, INSTALL):
        r = subprocess.run([BASH, "-c", _named(steps, name)["run"]], env=env, cwd=tmp_path,
                           capture_output=True, text=True)
        assert r.returncode == 0, name + ": " + r.stdout + r.stderr
    assert (tmp_path / "installed").exists() and out.read_text() == ""
    # and with a key alone, the load step DOES run the script (no repos -> red)
    shutil.copy(SCRIPT, kit / "ci_kit" / "git_ssh.sh")
    env["CI_KIT_GIT_SSH_KEY"] = "x"
    r = subprocess.run([BASH, "-c", _named(steps, LOAD)["run"]], env=env, cwd=tmp_path,
                       capture_output=True, text=True)
    assert r.returncode == 1 and "names no repo" in r.stdout
