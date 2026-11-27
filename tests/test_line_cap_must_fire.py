"""ci-line-cap must fire: a module over the cap -> 1, a grandfathered one at its baseline -> 0, a
ruled dir that read nothing or an unexcused python dir -> 2."""
from __future__ import annotations

from _pylint_repo import again, pads, run
from conftest import git, write

BIG = "# line\n" * 12
DIRS = ("--cap", "10", "--dir", "src:2", "--dir", "lib:1")


def _files(**extra: str) -> dict[str, str]:
    return {**pads("src", 2), **pads("lib", 1), **extra}


def test_a_new_module_over_the_cap_fires(new_repo) -> None:
    _, r = run(new_repo, "line_cap", _files(**{"lib/big.py": BIG}), *DIRS)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "NEW over the cap: lib/big.py is 12 lines" in r.stdout


def test_a_grandfathered_module_passes_and_growth_fails(new_repo) -> None:
    repo, r = run(new_repo, "line_cap", _files(**{"src/big.py": BIG}), *DIRS, "--write-baseline")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "src/big.py|12" in (repo / "ci/line_cap_baseline.txt").read_text(encoding="utf-8")
    assert again("line_cap", repo, *DIRS).returncode == 0
    write(repo / "src/big.py", BIG + "# one more\n")
    r = again("line_cap", repo, *DIRS)
    assert r.returncode == 1 and "GREW: src/big.py is 13 lines, baseline 12" in r.stdout, r.stdout


def test_a_rewrite_keeps_the_callers_header(new_repo) -> None:
    repo, _ = run(new_repo, "line_cap", _files(**{"src/big.py": BIG}), *DIRS)
    write(repo / "ci/line_cap_baseline.txt", "# armed 2026-09-18 with 47 files\nsrc/big.py|20\n")
    assert again("line_cap", repo, *DIRS, "--write").returncode == 0
    assert (repo / "ci/line_cap_baseline.txt").read_text(encoding="utf-8") == \
        "# armed 2026-09-18 with 47 files\nsrc/big.py|12\n"


def test_a_ruled_dir_that_read_nothing_is_2(new_repo) -> None:
    _, r = run(new_repo, "line_cap", pads("src", 2), *DIRS)
    assert r.returncode == 2 and "line-cap[lib]" in r.stderr, r.stdout + r.stderr


def test_an_unruled_python_dir_is_2_and_an_excused_one_passes(new_repo) -> None:
    repo, r = run(new_repo, "line_cap", _files(**{"flows/h.py": PADX, "tests/t.py": PADX}), *DIRS,
                  "--unruled", "tests:a test's length tracks its cases")
    assert r.returncode == 2 and "['flows']" in r.stderr, r.stdout + r.stderr
    git(repo, "rm", "-q", "--cached", "flows/h.py")
    r = again("line_cap", repo, *DIRS, "--unruled", "tests:a test's length tracks its cases")
    assert r.returncode == 0, r.stdout + r.stderr


def test_no_dir_and_a_reasonless_excuse_are_2(new_repo) -> None:
    repo, r = run(new_repo, "line_cap", _files())
    assert r.returncode == 2 and "no --dir" in r.stderr
    assert again("line_cap", repo, *DIRS, "--unruled", "tests:").returncode == 2


def test_an_args_file_is_read(new_repo) -> None:
    repo, _ = run(new_repo, "line_cap", _files(**{"lib/big.py": BIG}))
    write(repo / "ci/line_cap.args", "--cap=10\n--dir=src:2\n--dir=lib:1\n")
    assert again("line_cap", repo, "@ci/line_cap.args").returncode == 1


PADX = "y = 2\n"


def test_an_args_file_may_carry_blank_and_comment_lines(new_repo) -> None:
    repo, _ = run(new_repo, "line_cap", _files(**{"lib/big.py": BIG}))
    write(repo / "ci/line_cap.args", "# why: the daemons\n--cap=10\n\n--dir=./src:2\n--dir=lib:1\n")
    r = again("line_cap", repo, "@ci/line_cap.args")
    assert r.returncode == 1 and "lib/big.py" in r.stdout, r.stdout + r.stderr


def test_a_floor_of_zero_is_refused(new_repo) -> None:
    _, r = run(new_repo, "line_cap", _files(), "--dir", "src:0")
    assert r.returncode == 2 and ">= 1" in r.stderr, r.stdout + r.stderr


def test_a_ruled_root_covers_every_directory(new_repo) -> None:
    _, r = run(new_repo, "line_cap", _files(**{"flows/h.py": PADX}), "--cap", "10", "--dir", ".",
               "--unruled", "tests:a test's length tracks its cases")
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_bomd_baseline_keeps_its_header_and_a_utf16_one_is_2(new_repo) -> None:
    repo, _ = run(new_repo, "line_cap", _files(**{"src/big.py": BIG}), *DIRS)
    base = repo / "ci/line_cap_baseline.txt"
    base.parent.mkdir(exist_ok=True)
    base.write_bytes(b"\xef\xbb\xbf# armed with a reason\nsrc/big.py|12\n")
    assert again("line_cap", repo, *DIRS, "--write").returncode == 0
    assert base.read_text(encoding="utf-8").startswith("# armed with a reason\n")
    base.write_bytes("# hdr\nsrc/big.py|12\n".encode("utf-16"))
    r = again("line_cap", repo, *DIRS)
    assert r.returncode == 2 and "cannot read baseline" in r.stderr, r.stdout + r.stderr
