"""ci-env-reads: bad -> 1, good -> 0, blind -> 2, driven as a caller runs it."""
from __future__ import annotations

from _sqlrepo import tracked_repo
from conftest import NL, git, run_tool, write

HAND = "slot=$(grep -E '^SVC_ACTIVE=' \"$ENV_FILE\" | tail -1 | cut -d= -f2-)" + NL
SED = "tok=$(sed -n 's/^WINDMILL_TOKEN=//p' \"$WM_ENV\" | sed -n 1p)" + NL
GOOD = ". scripts/lib-env.sh" + NL + "slot=$(env_get SVC_ACTIVE \"$ENV_FILE\")" + NL
PIPE = "sha=$(docker inspect c --format '{{.Config.Env}}' | sed -n 's/^GIT_SHA=//p')" + NL


def _run(repo, *extra):
    return run_tool("env_reads", repo, "--root", str(repo), *extra)


def test_a_hand_pipeline_fires(new_repo) -> None:
    r = _run(tracked_repo(new_repo, {"scripts/a.sh": HAND, "scripts/b.sh": SED}))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "scripts/a.sh: 1 site(s)" in r.stdout and "scripts/b.sh: 1 site(s)" in r.stdout


def test_an_extensionless_shebang_script_is_scanned(new_repo) -> None:
    r = _run(tracked_repo(new_repo, {"bin/tool": "#!/usr/bin/env bash" + NL + HAND}))
    assert r.returncode == 1, r.stdout + r.stderr


def test_the_reader_and_a_container_env_pipe_are_clean(new_repo) -> None:
    r = _run(tracked_repo(new_repo, {"scripts/ok.sh": GOOD + PIPE, "README.md": HAND}))
    assert r.returncode == 0, r.stdout + r.stderr


def test_baselined_sites_pass_and_a_new_one_fails(new_repo) -> None:
    repo = tracked_repo(new_repo, {"scripts/a.sh": HAND})
    assert _run(repo, "--write-baseline").returncode == 0
    base = (repo / "ci/env_reads_baseline.txt").read_text(encoding="utf-8")
    assert "scripts/a.sh|1" in base and base.startswith("# env-reads baseline")
    assert _run(repo).returncode == 0
    write(repo / "scripts/a.sh", HAND + SED)
    r = _run(repo)
    assert r.returncode == 1 and "2 site(s), baseline 1" in r.stdout, r.stdout


def test_a_migrated_site_shrinks_quietly(new_repo) -> None:
    repo = tracked_repo(new_repo, {"scripts/a.sh": HAND + SED})
    assert _run(repo, "--write-baseline").returncode == 0
    write(repo / "scripts/a.sh", GOOD + SED)
    r = _run(repo)
    assert r.returncode == 0 and "shrank" in r.stdout, r.stdout


def test_exempt_skips_the_reader_itself(new_repo) -> None:
    repo = tracked_repo(new_repo, {"scripts/lib-env.sh": HAND})
    assert _run(repo, "--exempt", "scripts/lib-env.sh").returncode == 2  # nothing left to scan
    write(repo / "scripts/x.sh", GOOD)
    git(repo, "add", "-A")
    assert _run(repo, "--exempt", "scripts/lib-env.sh").returncode == 0


def test_no_shell_file_is_2_not_clean(new_repo) -> None:
    r = _run(tracked_repo(new_repo, {"app/x.py": "print(1)" + NL}))
    assert r.returncode == 2, r.stdout + r.stderr


def test_an_undecodable_file_is_2(new_repo) -> None:
    repo = tracked_repo(new_repo, {"scripts/ok.sh": GOOD})
    (repo / "scripts/bad.sh").write_bytes(b"x=\xff\xfe\xfa" + NL.encode())
    git(repo, "add", "-A")
    r = _run(repo)
    assert r.returncode == 2 and "DID NOT FINISH LOOKING" in r.stdout + r.stderr, r.stdout + r.stderr


def test_selftest_passes(new_repo) -> None:
    r = run_tool("env_reads", new_repo(), "--selftest")
    assert r.returncode == 0 and "selftest OK" in r.stdout, r.stdout + r.stderr
