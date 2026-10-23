"""ci-pin-drift must-fire: lagging pin -> 1, current pin -> 0, nothing to compare -> 2.

The remote is a REAL git repo with real tags (`git ls-remote` against a path), not a mocked list.
"""
from __future__ import annotations

from pathlib import Path

from conftest import git, run_tool, write

from ci_kit import pin_drift


def _remote(new_repo, *tags: str) -> Path:
    r = new_repo("ci-kit")
    write(r / "README.md", "x\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    for t in tags:
        git(r, "tag", t)
    return r


def _consumer(tmp_path: Path, text: str, name: str = "uv.lock") -> Path:
    p = tmp_path / "consumer" / name
    write(p, text)
    return p


LOCK = 'source = { git = "https://github.com/example-org/ci-kit.git?tag=v1.1.0#abc" }\n'


def test_pins_in_reads_every_spelling_and_skips_moving_refs() -> None:
    text = ("a ci-kit@v1.1.0\n"
            "b ci-kit?tag=v1.2.3\n"
            "c ci-kit.git?tag=v2.0.10#sha\n"
            "d uses: example-org/ci-kit@v1\n"                 # moving major: not a pin
            "e uses: example-org/ci-kit@06e8cf2a9a55\n"       # sha: not a pin
            "f ci-kit@v1.1.0.dev1\n"                           # not exact semver
            "g uses: example-org/ci-kit/.github/workflows/doc-refs.yml@v1.4.0\n"  # reusable wf
            "h my-ci-kit@v0.1.0\n"                             # a different name
            "i ci-kit@v1.1.0-rc1\n")                           # pre-release: not a release pin
    assert pin_drift.pins_in(text, "ci-kit") == [(1, "v1.1.0"), (2, "v1.2.3"), (3, "v2.0.10"), (7, "v1.4.0")]


def test_pins_in_reads_a_release_asset_url() -> None:
    url = "https://github.com/example-org/ci-kit/releases/download/{}/ci_kit-1-py3-none-any.whl"
    text = (f'a "ci-kit @ {url.format("v0.1.1")}",\n'                 # pyproject PEP 508
            f'b ci-kit = {{ url = "{url.format("v0.2.0")}" }}\n'        # [tool.uv.sources] / uv.lock
            f'c "{url.format("v0.1.1-rc1")}"\n'                         # pre-release: not a pin
            'd https://github.com/example-org/my-ci-kit/releases/download/v9.9.9/x.whl\n')
    assert pin_drift.pins_in(text, "ci-kit") == [(1, "v0.1.1"), (2, "v0.2.0")]


def test_lagging_release_asset_pin_is_exit_1(new_repo, tmp_path: Path) -> None:
    rem = _remote(new_repo, "v0.1.0", "v0.1.1", "v0.2.0")
    lock = _consumer(tmp_path, 'source = { url = "https://github.com/example-org/ci-kit/releases/'
                               'download/v0.1.1/ci_kit-0.1.1-py3-none-any.whl" }\n')
    r = run_tool("pin_drift", tmp_path, "--file", str(lock), "--remote", str(rem))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "behind ci-kit v0.2.0" in r.stdout and f"{lock}:1: v0.1.1" in r.stdout


def test_pin_naming_no_release_is_exit_1(new_repo, tmp_path: Path) -> None:
    # newer than the newest tag (typo / bump past a release) AND a hole between real tags
    rem = _remote(new_repo, "v1.0.0", "v1.1.0")
    for bad in ("v1.9.0", "v1.0.5"):
        f = _consumer(tmp_path, f"ci-kit@{bad}\nci-kit@v1.1.0\n", name=f"{bad}.yml")
        r = run_tool("pin_drift", tmp_path, "--file", str(f), "--remote", str(rem))
        assert r.returncode == 1, (bad, r.stdout, r.stderr)
        assert "name no ci-kit release tag" in r.stdout and f"{f}:1: {bad}" in r.stdout


def test_unreadable_file_is_exit_2(new_repo, tmp_path: Path) -> None:
    rem = _remote(new_repo, "v1.1.0")
    f = tmp_path / "consumer" / "bin.lock"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"ci-kit@v1.1.0\n\xff\xfe\xfa\n")
    r = run_tool("pin_drift", tmp_path, "--file", str(f), "--remote", str(rem))
    assert r.returncode == 2 and "could not read" in r.stderr


def test_lagging_pin_is_exit_1(new_repo, tmp_path: Path) -> None:
    rem = _remote(new_repo, "v1", "v1.0.0", "v1.1.0", "v1.10.0", "v1.2.0")  # numeric, not lexical
    lock = _consumer(tmp_path, LOCK)
    r = run_tool("pin_drift", tmp_path, "--file", str(lock), "--remote", str(rem))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "behind ci-kit v1.10.0" in r.stdout and f"{lock}:1: v1.1.0" in r.stdout


def test_current_pin_is_exit_0(new_repo, tmp_path: Path) -> None:
    rem = _remote(new_repo, "v1", "v1.0.0", "v1.1.0")
    lock = _consumer(tmp_path, LOCK)
    wf = _consumer(tmp_path, "      - uses: example-org/ci-kit@v1.1.0\n", name="ci.yml")
    r = run_tool("pin_drift", tmp_path, "--file", str(lock), "--file", str(wf), "--remote", str(rem))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "2 pin(s), all at the newest ci-kit tag v1.1.0" in r.stdout


def test_one_lagging_file_among_current_ones_is_1(new_repo, tmp_path: Path) -> None:
    rem = _remote(new_repo, "v1.1.0", "v1.2.0")
    cur = _consumer(tmp_path, LOCK.replace("v1.1.0", "v1.2.0"))
    old = _consumer(tmp_path, "ci-kit@v1.1.0\n", name="ci.yml")
    r = run_tool("pin_drift", tmp_path, "--file", str(cur), "--file", str(old), "--remote", str(rem))
    assert r.returncode == 1
    assert f"{old}:1: v1.1.0" in r.stdout and str(cur) not in r.stdout


def test_no_pin_found_is_exit_2(new_repo, tmp_path: Path) -> None:
    rem = _remote(new_repo, "v1.1.0")
    f = _consumer(tmp_path, "uses: example-org/ci-kit@v1\n")
    r = run_tool("pin_drift", tmp_path, "--file", str(f), "--remote", str(rem))
    assert r.returncode == 2 and "has verified nothing" in r.stderr


def test_missing_file_is_exit_2(tmp_path: Path) -> None:
    r = run_tool("pin_drift", tmp_path, "--file", str(tmp_path / "nope.lock"), "--remote", str(tmp_path))
    assert r.returncode == 2


def test_unreachable_remote_is_exit_2(tmp_path: Path) -> None:
    lock = _consumer(tmp_path, LOCK)
    r = run_tool("pin_drift", tmp_path, "--file", str(lock), "--remote", str(tmp_path / "no_such_remote"),
                 "--name", "ci-kit")
    assert r.returncode == 2 and "could not look" in r.stderr


def test_remote_without_semver_tags_is_exit_2(new_repo, tmp_path: Path) -> None:
    rem = _remote(new_repo, "v1", "latest")
    lock = _consumer(tmp_path, LOCK)
    r = run_tool("pin_drift", tmp_path, "--file", str(lock), "--remote", str(rem))
    assert r.returncode == 2 and "no vX.Y.Z tag" in r.stderr


def test_name_is_derived_from_the_remote_or_given(new_repo, tmp_path: Path) -> None:
    for remote, want in (("https://github.com/o/ci-kit.git", "ci-kit"), ("https://github.com/o/ci-kit", "ci-kit"),
                         ("git@github.com:o/my-kit.git", "my-kit"), ("/tmp/kits/ci-kit/", "ci-kit")):
        assert pin_drift.name_of(remote) == want, remote
    # the pin's name need not match the remote's path: --name overrides it
    rem = _remote(new_repo, "v1.1.0")
    f = _consumer(tmp_path, "other-kit@v1.1.0" + chr(10), name="ci.yml")
    assert run_tool("pin_drift", tmp_path, "--file", str(f), "--remote", str(rem)).returncode == 2
    r = run_tool("pin_drift", tmp_path, "--file", str(f), "--remote", str(rem), "--name", "other-kit")
    assert r.returncode == 0, r.stdout + r.stderr
