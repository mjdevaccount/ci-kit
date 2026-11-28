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

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "ci_kit" / "ssh_retry.sh"
BASH = shutil.which("bash") or "bash"  # by full path: on Windows, System32\bash.exe is WSL
needs_bash = pytest.mark.skipif(shutil.which("bash") is None and not os.environ.get("CI"), reason="no bash")

BANNER = "Connection timed out during banner exchange\nConnection to 140.82.114.3 port 22 timed out"
FAKE = r"""#!/usr/bin/env bash
# fake ssh: call N plays line N of $FAKE_SEQ ("<rc>|<stderr>"); records argv and stdin
d="$FAKE_DIR"; n=$(( $(cat "$d/n" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$d/n"
printf '%s\n' "$*" > "$d/argv.$n"
line=$(sed -n "${n}p" "$d/seq"); rc=${line%%|*}; msg=${line#*|}
if [ "$rc" = 0 ]; then cat > "$d/stdin.$n"; echo "fetched"; else printf '%b\n' "$msg" >&2; fi
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
    assert "ssh-retry: attempt 1/3 never connected (Connection timed out); retrying in 0s" in r.stderr
    assert (tmp_path / "argv.1").read_text() == (tmp_path / "argv.2").read_text()


@needs_bash
@pytest.mark.parametrize("msg", [
    "git@github.com: Permission denied (publickey).",
    "@@@@@@@@@@@\n@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\nHost key verification failed.",
    "Connection to github.com closed by remote host.",
])
def test_a_failure_after_the_connection_is_never_retried(tmp_path: pathlib.Path, msg: str) -> None:
    r, calls = _run(tmp_path, [(255, msg), (0, "")])
    assert (r.returncode, calls) == (255, 1), r.stderr
    assert "ssh-retry" not in r.stderr


@needs_bash
def test_the_remote_commands_own_exit_is_returned_as_is(tmp_path: pathlib.Path) -> None:
    # a nonzero that is not 255 is git-upload-pack's answer (e.g. repo not found), even if the text matches
    r, calls = _run(tmp_path, [(128, "ERROR: Repository not found. Connection refused"), (0, "")])
    assert (r.returncode, calls) == (128, 1), r.stderr


@needs_bash
def test_attempts_are_bounded(tmp_path: pathlib.Path) -> None:
    r, calls = _run(tmp_path, [(255, BANNER)] * 5, CI_KIT_SSH_TRIES="2")
    assert (r.returncode, calls) == (255, 2), r.stderr
    assert r.stderr.count("ssh-retry:") == 1


@needs_bash
@pytest.mark.parametrize("env", [{"CI_KIT_SSH_TRIES": v} for v in ("0", "x", "100")] + [{"CI_KIT_SSH_PAUSE": "-1"}])
def test_a_bad_knob_is_exit_2_before_any_ssh(tmp_path: pathlib.Path, env: dict[str, str]) -> None:
    r, calls = _run(tmp_path, [(0, "")], **env)
    assert (r.returncode, calls) == (2, 0), r.stderr
