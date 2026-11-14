"""ci_kit/git_ssh.sh with ONE KEY PER REPO (v1.28.0) -- must fire, both directions.

A GitHub deploy key reads one repo, and over a shared agent GitHub takes the first key offered and
refuses every other repo. So N keys for N repos load into N agents, each repo is routed to its own
host alias (`ci-kit.<owner>.<name>`, IdentityAgent = its agent). GREEN: each alias resolves to
github.com through the pinned host keys and ITS agent holds exactly ITS key; the outputs a BuildKit
`ssh:` input needs (`agents`, JSON repo -> socket) are published; kill stops every agent. RED: a
count that pairs nothing (2 keys, 3 repos), the same key twice, a second key ssh-add refuses, a
route for a repo the config never named, an agent that died after load.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

from test_git_ssh_must_fire import BASH, SCRIPT, _alive, _bash, _kill, _load, needs_tools


def _pair(tmp: pathlib.Path, name: str) -> tuple[str, str]:
    key = tmp / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(key)], check=True)
    fp = subprocess.run(["ssh-keygen", "-lf", str(key) + ".pub"], check=True, capture_output=True,
                        text=True).stdout.split()[1]
    return key.read_text(), fp


def _route(tmp: pathlib.Path, outputs: dict[str, str], repos: str, after: str) -> subprocess.CompletedProcess[str]:
    return _bash(tmp, f'. "{SCRIPT.as_posix()}" route\n{after}\n', SSH_AUTH_SOCK=outputs.get("sock", ""),
                 CI_KIT_GIT_SSH_KNOWN_HOSTS=outputs.get("known-hosts", ""),
                 CI_KIT_GIT_SSH_CONFIG=outputs.get("ssh-config", ""), CI_KIT_GIT_SSH_REPOS=repos)


def _agent_fps(tmp: pathlib.Path, sock: str) -> str:
    return _bash(tmp, "ssh-add -l || true", SSH_AUTH_SOCK=sock).stdout


# ---- GREEN ---------------------------------------------------------------------------------

@needs_tools
def test_one_key_per_repo_routes_each_repo_to_its_own_agent(tmp_path: pathlib.Path) -> None:
    k1, fp1 = _pair(tmp_path, "k_a")
    k2, fp2 = _pair(tmp_path, "k_b")
    # the caller's shape: a `|` block, each secret on its own (indented) line, a Windows-pasted CRLF
    key = "  " + k1.replace("\n", "\r\n") + "\n  " + k2
    r, outputs = _load(tmp_path, key=key, repos="o/a-repo\nOwn/B.repo")
    assert r.returncode == 0, r.stdout + r.stderr
    assert set(outputs) == {"sock", "pid", "known-hosts", "ssh-config", "agents"}
    assert "PRIVATE KEY" not in r.stdout + r.stderr
    pids = outputs["pid"].split()
    try:
        assert len(pids) == 2 and len(set(pids)) == 2
        agents = json.loads(outputs["agents"])
        assert set(agents) == {"o/a-repo", "Own/B.repo"} and outputs["sock"] == agents["o/a-repo"]
        held_a, held_b = _agent_fps(tmp_path, agents["o/a-repo"]), _agent_fps(tmp_path, agents["Own/B.repo"])
        assert fp1 in held_a and fp2 not in held_a, "repo a's agent holds the wrong key"
        assert fp2 in held_b and fp1 not in held_b, "repo b's agent holds the wrong key"
        cfg = pathlib.Path(outputs["ssh-config"]).read_text()
        assert "Host ci-kit.o.a-repo\n" in cfg and "Host ci-kit.own.b.repo\n" in cfg  # lower case
        assert cfg.count("HostKeyAlias github.com") == 2 and "PRIVATE KEY" not in cfg
        i = _route(tmp_path, outputs, "o/a-repo Own/B.repo", "\n".join([
            "git ls-remote --get-url https://github.com/o/a-repo > a.txt",
            "git ls-remote --get-url https://github.com/Own/B.repo.git > b.txt",
            "git ls-remote --get-url https://github.com/o/public > unnamed.txt",
            'printf "%s" "$GIT_SSH_COMMAND" > sshcmd.txt',
            "env > install-env.txt",
        ]))
        assert i.returncode == 0, i.stdout + i.stderr
        assert (tmp_path / "a.txt").read_text().strip() == "ssh://git@ci-kit.o.a-repo/o/a-repo"
        assert (tmp_path / "b.txt").read_text().strip() == "ssh://git@ci-kit.own.b.repo/Own/B.repo.git"
        assert (tmp_path / "unnamed.txt").read_text().strip() == "https://github.com/o/public"
        cmd = (tmp_path / "sshcmd.txt").read_text()
        assert cmd.startswith(f"ssh -F {outputs['ssh-config']} ") and "StrictHostKeyChecking=yes" in cmd
        assert "PRIVATE KEY" not in (tmp_path / "install-env.txt").read_text(errors="replace")
        # what ssh itself makes of an alias: github.com, the pinned host key's name, ITS agent
        g = subprocess.run(["ssh", "-G", "-F", outputs["ssh-config"], "ci-kit.own.b.repo"],
                           capture_output=True, text=True)
        if g.returncode == 0:  # an ssh without -G (old Windows builds) cannot say; Linux CI can
            opts = dict(ln.split(" ", 1) for ln in g.stdout.splitlines() if " " in ln)
            assert opts["hostname"] == "github.com" and opts["hostkeyalias"] == "github.com"
            assert opts["identityagent"] == agents["Own/B.repo"] and opts["user"] == "git"
    finally:
        k = _kill(tmp_path, outputs["pid"])
    assert k.returncode == 0, k.stdout + k.stderr
    assert not any(_alive(p) for p in pids), "an agent outlived the kill step"


@needs_tools
def test_git_fetch_goes_to_each_repos_alias(tmp_path: pathlib.Path) -> None:
    k1, _ = _pair(tmp_path, "k_a")
    k2, _ = _pair(tmp_path, "k_b")
    r, outputs = _load(tmp_path, key=k1 + k2, repos="o/a,o/b")  # no newline between the two blocks
    assert r.returncode == 0, r.stdout + r.stderr
    rec = tmp_path / "ssh-recorder"
    rec.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$(dirname "$0")/ssh-argv.txt"\nexit 1\n')
    rec.chmod(0o755)
    try:
        i = _route(tmp_path, outputs, "o/a o/b", "\n".join([
            f'GIT_SSH_COMMAND="{rec.as_posix()}" git ls-remote https://github.com/o/b || true',
        ]))
        assert i.returncode == 0, i.stdout + i.stderr
        argv = (tmp_path / "ssh-argv.txt").read_text()
        assert "git@ci-kit.o.b" in argv and "o/b" in argv and "ci-kit.o.a" not in argv
    finally:
        _kill(tmp_path, outputs["pid"])


@needs_tools
def test_one_key_for_several_repos_keeps_the_old_shape(tmp_path: pathlib.Path) -> None:
    k1, _ = _pair(tmp_path, "k_a")
    r, outputs = _load(tmp_path, key=k1, repos="o/a o/b")
    try:
        assert r.returncode == 0 and set(outputs) == {"sock", "pid", "known-hosts"}, r.stdout + r.stderr
    finally:
        _kill(tmp_path, outputs.get("pid", "0"))


# ---- RED -----------------------------------------------------------------------------------

@needs_tools
def test_key_count_that_pairs_nothing_fails(tmp_path: pathlib.Path) -> None:
    k1, _ = _pair(tmp_path, "k_a")
    k2, _ = _pair(tmp_path, "k_b")
    r, outputs = _load(tmp_path, key=k1 + "\n" + k2, repos="o/a o/b o/c")
    assert r.returncode == 1 and "2 keys for 3 repos" in r.stdout and outputs == {}


@needs_tools
def test_the_same_key_twice_fails(tmp_path: pathlib.Path) -> None:
    k1, _ = _pair(tmp_path, "k_a")
    r, outputs = _load(tmp_path, key=k1 + "\n" + k1, repos="o/a o/b")
    assert r.returncode == 1 and "repeats an earlier key" in r.stdout and "o/b" in r.stdout
    assert outputs == {}


@needs_tools
def test_a_second_key_ssh_add_refuses_fails_naming_its_repo(tmp_path: pathlib.Path) -> None:
    k1, _ = _pair(tmp_path, "k_a")
    bad = "-----BEGIN OPENSSH PRIVATE KEY-----\nbm90IGEga2V5\n-----END OPENSSH PRIVATE KEY-----"
    r, outputs = _load(tmp_path, key=k1 + "\n" + bad, repos="o/a o/b")
    assert r.returncode == 1 and "ssh-add refused the key for o/b (key 2 of 2)" in r.stdout
    assert outputs == {}


@needs_tools
def test_route_for_a_repo_the_config_never_named_fails(tmp_path: pathlib.Path) -> None:
    k1, _ = _pair(tmp_path, "k_a")
    k2, _ = _pair(tmp_path, "k_b")
    r, outputs = _load(tmp_path, key=k1 + k2, repos="o/a o/b")
    assert r.returncode == 0, r.stdout + r.stderr
    try:
        i = _route(tmp_path, outputs, "o/a o/c", "touch installed")
        assert i.returncode == 1 and "o/c has no key" in i.stdout
        assert not (tmp_path / "installed").exists()
    finally:
        _kill(tmp_path, outputs["pid"])


@needs_tools
def test_route_with_a_dead_second_agent_fails(tmp_path: pathlib.Path) -> None:
    k1, _ = _pair(tmp_path, "k_a")
    k2, _ = _pair(tmp_path, "k_b")
    r, outputs = _load(tmp_path, key=k1 + k2, repos="o/a o/b")
    assert r.returncode == 0, r.stdout + r.stderr
    first, second = outputs["pid"].split()
    try:
        _kill(tmp_path, second)
        i = _route(tmp_path, outputs, "o/a o/b", "touch installed")
        assert i.returncode == 1 and "is gone" in i.stdout
        assert not (tmp_path / "installed").exists()
    finally:
        _kill(tmp_path, first)


def test_kill_refuses_a_non_pid() -> None:
    if shutil.which("bash") is None:
        return  # zero-ok: no bash, nothing to run the script with
    r = subprocess.run([BASH, str(SCRIPT), "kill", "12 x"], capture_output=True, text=True)
    assert r.returncode == 1 and "is not one" in r.stdout
