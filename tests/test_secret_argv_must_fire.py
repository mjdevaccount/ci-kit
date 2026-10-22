"""ci-secret-argv-lint must fire: known-bad exits 1, known-good exits 0, blind exits 2."""
from __future__ import annotations

from pathlib import Path

from conftest import NL, git, run_tool, write

HDR = "X-API-" + "Key"  # assembled so this file is not itself a finding


def _commit(r: Path) -> None:
    git(r, "add", "-A")
    git(r, "commit", "-qm", "c")


def test_host_expanded_key_in_exec_exits_1(new_repo) -> None:
    """The 2026-10-20 shape: the HOST expands $K, so the key lands in docker events."""
    r = new_repo()
    write(r / "scripts/auto-paper.sh",
          "K=$(docker exec \"$cn\" printenv APP_API_KEY)" + NL
          + f"docker exec \"$cn\" curl -sf -H \"{HDR}: $K\" \"http://x/?s=$s\"" + NL)
    _commit(r)
    res = run_tool("secret_argv", r)
    assert res.returncode == 1, res.stdout
    assert "scripts/auto-paper.sh:1: R2" in res.stdout
    assert "scripts/auto-paper.sh:2: R1" in res.stdout


def test_host_curl_header_exits_1(new_repo) -> None:
    r = new_repo()
    write(r / "a.sh", "curl -s -H \"Authorization: Bearer ${SVC_TOKEN}\" \"$SVC/api\"" + NL)
    _commit(r)
    res = run_tool("secret_argv", r)
    assert res.returncode == 1, res.stdout
    assert "a.sh:1: R3" in res.stdout


def test_container_side_expansion_exits_0(new_repo) -> None:
    """Single-quoted (and backslash-escaped) $VAR reaches the container unexpanded: the fix."""
    r = new_repo()
    write(r / "a.sh",
          f"docker exec \"$cn\" sh -c 'curl -sf -H \"{HDR}: $APP_API_KEY\" \"$1\"' _ \"$s\""
          + NL
          + f"docker exec \"$svc\" sh -c \"curl -H '{HDR}: \\$APP_API_KEY' u\"" + NL
          + f"curl -s -H @<(printf '{HDR}: %s\\n' \"$K\") http://127.0.0.1/x" + NL
          + "# history: K=$(docker exec c printenv APP_API_KEY)" + NL)
    write(r / "README.md", f"docker exec c curl -H \"{HDR}: $K\"" + NL)
    _commit(r)
    res = run_tool("secret_argv", r)
    assert res.returncode == 0, res.stdout


def test_multiline_container_sh_c_exits_0(new_repo) -> None:
    """A `sh -c '` quote left open across lines is one logical line: the curl is container-side."""
    r = new_repo()
    write(r / "a.sh",
          "docker exec \"$cn\" sh -c '" + NL
          + f"  curl -sf -H \"{HDR}: $APP_API_KEY\" \"$1\"" + NL
          + "' _ \"$u\"" + NL
          + f"docker exec c curl -s u  # was -H \"{HDR}: $K\"" + NL
          + f"docker exec c sh -c $'curl -H \"{HDR}: $APP_API_KEY\" u'" + NL
          + "f=$(docker exec c printenv APP_API_KEY | sha256sum)" + NL
          # an apostrophe in a comment opens no quote; `"$(... "$c" ...)"` is a fresh context
          # inside the double quotes, not a closing quote (a key-rotation status script)
          + "  # Don't stop at the fingerprint." + NL
          + "who=\"$(WMK=\"$wmv\" docker exec -e WMK \"$c\" sh -c \\" + NL
          + f"   'curl -s -H \"{HDR}: $WMK\" http://localhost:8080/me' 2>/dev/null \\" + NL
          + "   | sed -n 's/x/y/p')\"" + NL
          # the token reaches the docker client on an fd (a push script)
          + "out=\"$(docker run --rm --env-file <(docker exec \"$S\" sh -c "
          + "'printf \"SVC_TOKEN=%s\\n\" \"$SVC_TOKEN\"') img python x.py 2>&1)\"" + NL)
    write(r / "compose.yml",
          "      test: [\"CMD-SHELL\", \"curl -H 'Authorization: Bearer $$TOKEN' u\"]" + NL)
    _commit(r)
    res = run_tool("secret_argv", r)
    assert res.returncode == 0, res.stdout


def test_hoist_through_nested_subst_and_echo_exits_1(new_repo) -> None:
    r = new_repo()
    write(r / "a.sh",
          "K=$(docker exec \"$(active.sh)\" printenv APP_API_KEY)" + NL
          + "K=$(docker exec c sh -c 'echo \"$APP_API_KEY\"')" + NL)
    _commit(r)
    res = run_tool("secret_argv", r)
    assert res.returncode == 1, res.stdout
    assert "a.sh:1: R2 hoist (printenv" in res.stdout
    assert "a.sh:2: R2 hoist (echo/printf" in res.stdout


def test_compose_env_and_psub_body_exit_1(new_repo) -> None:
    r = new_repo()
    write(r / "a.sh",
          "docker-compose exec svc curl -H \"Authorization: Bearer $T\" u" + NL
          + "docker run --env APP_API_KEY=\"$k\" img" + NL
          + "docker run --env-file <(curl -s -H \"Authorization: Bearer $T\" u) img" + NL
          + f"curl -H @<(printf '{HDR}: %s' \"$K\" | cat) -H \"Cookie: s=$C\" u" + NL
          + "echo don\\'t" + NL
          + f"docker exec c curl -H \"{HDR}: $K\" u" + NL)
    _commit(r)
    res = run_tool("secret_argv", r)
    assert res.returncode == 1, res.stdout
    for want in ("a.sh:1: R1", "a.sh:2: R1", "a.sh:3: R3", "a.sh:4: R3", "a.sh:6: R1"):
        assert want in res.stdout, res.stdout


def test_blind_outside_any_repo_exits_2(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    write(plain / "a.sh", f"docker exec c curl -H \"{HDR}: $K\"" + NL)
    res = run_tool("secret_argv", plain)
    assert res.returncode == 2, res.stdout + res.stderr


def test_blind_empty_repo_exits_2(new_repo) -> None:
    res = run_tool("secret_argv", new_repo())
    assert res.returncode == 2, res.stdout


def test_selftest_passes() -> None:
    res = run_tool("secret_argv", Path.cwd(), "--selftest")
    assert res.returncode == 0, res.stdout
