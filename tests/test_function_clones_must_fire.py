"""ci-function-clones must fire: a renamed twin in another file -> 1 (both files named), a lone
function or a same-file pair -> 0, a dir under its floor or an unparseable file -> 2."""
from __future__ import annotations

from _pylint_repo import again, pads, run
from conftest import write

BODY = "".join(f"    t{i} = x * {i} + y.attr - z\n" for i in range(6)) + "    return t0\n"
A = "def f(x, y, z):\n    '''the original'''\n" + BODY
B = "def g(p, q, r):\n" + BODY.replace("x", "p").replace("y.", "q.").replace("z", "r")
DIRS = ("--dir", "src:1", "--dir", "lib:1")


def test_a_renamed_twin_in_another_file_fires(new_repo) -> None:
    _, r = run(new_repo, "function_clones", {"src/a.py": A, "lib/b.py": B}, *DIRS)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "lib/b.py: 1 twinned function(s), baseline 0" in r.stdout
    assert "lib/b.py:1 g == src/a.py:1 f" in r.stdout


def test_one_copy_and_a_same_file_pair_are_clean(new_repo) -> None:
    _, r = run(new_repo, "function_clones", {"src/a.py": A + "\n" + B, **pads("lib", 1)}, *DIRS)
    assert r.returncode == 0 and "0 cross-file group(s)" in r.stdout, r.stdout + r.stderr


def test_a_baselined_twin_passes_and_a_third_copy_fails(new_repo) -> None:
    repo, r = run(new_repo, "function_clones", {"src/a.py": A, "lib/b.py": B}, *DIRS, "--write-baseline")
    assert r.returncode == 0, r.stdout + r.stderr
    assert again("function_clones", repo, *DIRS).returncode == 0
    write(repo / "lib/c.py", A.replace("def f", "def h"))
    from conftest import git
    git(repo, "add", "-A")
    r = again("function_clones", repo, *DIRS)
    assert r.returncode == 1 and "lib/c.py: 1 twinned" in r.stdout, r.stdout


def test_a_dir_that_read_nothing_is_2(new_repo) -> None:
    _, r = run(new_repo, "function_clones", {"src/a.py": A}, *DIRS)
    assert r.returncode == 2 and "function-clones[lib]" in r.stderr, r.stdout + r.stderr


def test_an_unparseable_file_is_2_not_clean(new_repo) -> None:
    _, r = run(new_repo, "function_clones", {"src/a.py": A, "lib/broken.py": "def (:\n"}, *DIRS)
    assert r.returncode == 2 and "lib/broken.py" in r.stdout + r.stderr, r.stdout + r.stderr
