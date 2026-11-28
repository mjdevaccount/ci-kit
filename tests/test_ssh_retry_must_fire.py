"""ci_kit/ssh_retry.sh -- git_ssh.sh's GIT_SSH_COMMAND -- re-runs ONLY a connection that never came up.

Driven for REAL against a fake `ssh` first on PATH, which plays a scripted sequence of
(exit code, stderr) per call and records each call's argv and stdin. RED (returned on the FIRST
call, never retried): auth refused, a host-key mismatch, a remote command's own nonzero exit. GREEN
(retried): the 2026-11-28 shape, "Connection timed out during banner exchange" then success. The
retried call still gets git's stdin whole, and the attempts are bounded by CI_KIT_SSH_TRIES.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "ci_kit" / "ssh_retry.sh"
BASH = shutil.which("bash") or "bash"  # by full path: on Windows, System32\bash.exe is WSL
needs_bash = pytest.mark.skipif(shutil.which("bash") is None and not os.environ.get("CI"), reason="no bash")

BANNER = "Connection timed out during banner exchange\nConnection to 140.82.114.3 port 22 timed out"
FAKE = r"""#!/usr/bin/env bash
# fake ssh (CR LF line ends, as OpenSSH writes them): call N plays line N of $FAKE_SEQ ("<rc>|<stderr>"); records argv and stdin
d="$FAKE_DIR"; n=$(( $(cat "$d/n" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$d/n"
printf '%s\n' "$*" > "$d/argv.$n"
line=$(sed -n "${n}p" "$d/seq"); rc=${line%%|*}; msg=${line#*|}
if [ "$rc" = 0 ]; then cat > "$d/stdin.$n"; echo "fetched"; else printf '%s\r\n' "${msg//\\n/$'\r\n'}" >&2; fi
exit "$rc"
"""


def _run(tmp: pathlib.Path, seq: list[tuple[int, str]], stdin: str = "want abc\n", **env: str):
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "ssh"
    fake.write_bytes(FAKE.encode())
    fake.chmod(0o755)
    (tmp / "seq").write_bytes("".join(f"{rc}|{msg.replace(chr(10), chr(92) + 'n')}\n" for rc, msg in seq).encode())
    e = {**os.environ, "PATH": f"{bindir.as_posix()}{os.pathsep}{os.environ['PATH']}",
         "FAKE_DIR": tmp.as_posix(), "CI_KIT_SSH_PAUSE": "0", **env}
    r = subprocess.run([BASH, SCRIPT.as_posix(), "-o", "BatchMode=yes", "git@github.com", "git-upload-pack 'o/r'"],
                       input=stdin, env=e, capture_output=True, text=True, timeout=60)
    calls = int((tmp / "n").read_text()) if (tmp / "n").exists() else 0
    return r, calls


@needs_bash
def test_a_banner_timeout_is_retried_and_the_retry_gets_stdin_whole(tmp_path: pathlib.Path) -> None:
    r, calls = _run(tmp_path, [(255, BANNER), (0, "")])
    assert (r.returncode, calls) == (0, 2), r.stderr
    assert r.stdout == "fetched\n"
    assert (tmp_path / "stdin.2").read_text() == "want abc\n"
    assert "banner exchange" in r.stderr  # ssh's own words still reach the log
    assert "ssh-retry: attempt 1/3 never connected (Connection timed out during banner exchange); retrying in 0s" in r.stderr
    assert (tmp_path / "argv.1").read_text() == (tmp_path / "argv.2").read_text()


@needs_bash
@pytest.mark.parametrize("msg", [
    "git@github.com: Permission denied (publickey).",
    "@@@@@@@@@@@\n@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\nHost key verification failed.",
    "Connection to github.com closed by remote host.",
    # mid-session, the same strerror words as a connect failure, NOT anchored as one
    "Read from remote host github.com: Connection timed out",
    # a harmless earlier warning must not make a later real failure retryable
    "Control socket connect(/tmp/cm): Connection refused\nHost key verification failed.",
])
def test_a_failure_after_the_connection_is_never_retried(tmp_path: pathlib.Path, msg: str) -> None:
    r, calls = _run(tmp_path, [(255, msg), (0, "")])
    assert (r.returncode, calls) == (255, 1), r.stderr
    assert "ssh-retry" not in r.stderr


@needs_bash
@pytest.mark.parametrize("msg", [
    "ssh: connect to host github.com port 22: Connection refused",
    "ssh: connect to host github.com port 22: Connection timed out",
    "ssh: Could not resolve hostname github.com: Temporary failure in name resolution",
    "kex_exchange_identification: read: Connection reset by peer",
])
def test_every_connect_phase_failure_is_retried(tmp_path: pathlib.Path, msg: str) -> None:
    r, calls = _run(tmp_path, [(255, msg), (0, "")])
    assert (r.returncode, calls) == (0, 2), r.stderr
    assert f"never connected ({msg})" in r.stderr


@needs_bash
def test_a_retry_lands_on_the_step_summary(tmp_path: pathlib.Path) -> None:
    # uv drops git's stderr when the fetch then succeeds: the summary is what still says it happened
    summary = tmp_path / "summary.md"
    r, calls = _run(tmp_path, [(255, BANNER), (0, "")], GITHUB_STEP_SUMMARY=summary.as_posix())
    assert (r.returncode, calls) == (0, 2), r.stderr
    assert summary.read_text().startswith("- ssh-retry: attempt 1/3 never connected (Connection timed out during banner exchange)")
    assert "ssh-retry" not in r.stdout  # stdout is git's protocol stream


@needs_bash
def test_the_remote_commands_own_exit_is_returned_as_is(tmp_path: pathlib.Path) -> None:
    # a nonzero that is not 255 is git-upload-pack's answer (e.g. repo not found), even if the text matches
    r, calls = _run(tmp_path, [(128, "ERROR: Repository not found. Connection refused"), (0, "")])
    assert (r.returncode, calls) == (128, 1), r.stderr


@needs_bash
def test_attempts_are_bounded(tmp_path: pathlib.Path) -> None:
    r, calls = _run(tmp_path, [(255, BANNER)] * 5, CI_KIT_SSH_TRIES="2")
    assert (r.returncode, calls) == (255, 2), r.stderr
    assert r.stderr.count("ssh-retry:") == 2  # one retry, then the give-up
    assert "ssh-retry: gave up after 2/2 attempts" in r.stderr


@needs_bash
@pytest.mark.parametrize("env", [{"CI_KIT_SSH_TRIES": v} for v in ("0", "x", "100")] + [{"CI_KIT_SSH_PAUSE": v} for v in ("-1", "08")])
def test_a_bad_knob_is_exit_2_before_any_ssh(tmp_path: pathlib.Path, env: dict[str, str]) -> None:
    r, calls = _run(tmp_path, [(0, "")], **env)
    assert (r.returncode, calls) == (2, 0), r.stderr


@needs_bash
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_a_signal_reaches_ssh_and_its_stderr_is_not_lost(tmp_path: pathlib.Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "ssh"
    fake.write_text('#!/usr/bin/env bash\necho $$ > "$FAKE_DIR/pid"\necho "debug1: connecting" >&2\nexec sleep 30\n')
    fake.chmod(0o755)
    e = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "FAKE_DIR": str(tmp_path)}
    p = subprocess.Popen([BASH, str(SCRIPT), "git@github.com"], env=e, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for _ in range(50):
        if (tmp_path / "pid").exists():
            break
        time.sleep(0.1)
    time.sleep(0.3)
    p.terminate()
    _, err = p.communicate(timeout=10)
    assert p.returncode == 143
    assert "debug1: connecting" in err
    pid = int((tmp_path / "pid").read_text())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # ssh did not outlive its wrapper


def _no_banner_server():
    """A listener that accepts every connection and never sends a byte: the 11-28 GitHub shape."""
    import socket
    import threading
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    held: list = []

    def accept() -> None:
        while True:
            try:
                held.append(srv.accept()[0])
            except OSError:
                return
    threading.Thread(target=accept, daemon=True).start()
    return srv, held


@needs_bash
@pytest.mark.skipif(shutil.which("ssh") is None and not os.environ.get("CI"), reason="no ssh")
@pytest.mark.skipif(sys.platform == "win32", reason="Win32-OpenSSH words its timeouts differently")
def test_real_ssh_against_a_server_that_never_sends_a_banner_is_retried(tmp_path: pathlib.Path) -> None:
    srv, held = _no_banner_server()
    try:
        e = {**os.environ, "CI_KIT_SSH_TRIES": "2", "CI_KIT_SSH_PAUSE": "0",
             "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md")}
        r = subprocess.run([BASH, str(SCRIPT), "-o", "BatchMode=yes", "-o", "ConnectTimeout=2",
                            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                            "-p", str(srv.getsockname()[1]), "127.0.0.1", "true"],
                           input="want abc\n", env=e, capture_output=True, text=True, timeout=60)
    finally:
        srv.close()  # ends the accept thread (accept raises OSError)
        for c in held:
            c.close()
    assert r.returncode == 255, r.stderr
    assert len(held) == 2, r.stderr  # the real OpenSSH line was matched and retried once
    assert "ssh-retry: gave up after 2/2 attempts, none connected (Connection timed out during banner exchange)" in r.stderr
    assert "ssh-retry: attempt 1/2" in (tmp_path / "summary.md").read_text()
