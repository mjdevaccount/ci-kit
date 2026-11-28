"""ci-untested-modules must fire: a module named only in test prose, or carrying a reasonless waiver
-> 1; imported / named in a string / mirrored / baselined -> 0; no --tests or a dir under its
floor -> 2."""
from __future__ import annotations

from _pylint_repo import again, pads, run

ARGS = ("--dir", "app:2", "--tests", "tests:1")
WAIVED = "# untested-ok: a fixture pad\n"


def _files(**extra: str) -> dict[str, str]:
    return {**{f"app/pad_{i}.py": WAIVED for i in range(2)}, **pads("tests", 1), **extra}


def test_a_module_named_only_in_prose_or_waived_bare_fires(new_repo) -> None:
    prose = ('"""Mentions app.reader and app/reader.py in prose only."""\n\n\ndef test_it():\n'
             '    part = "reader"\n    assert f"app.{part}"\n')
    _, r = run(new_repo, "untested_modules", _files(**{"app/reader.py": "x = 1\n", "app/half.py": "# untested-ok:\nx = 1\n",
                                                       "tests/test_prose.py": prose}), *ARGS)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "NEW: app/reader.py" in r.stdout and "NEW: app/half.py" in r.stdout


def test_imported_named_mirrored_and_baselined_pass(new_repo) -> None:
    files = _files(**{
        "app/reader.py": "x = 1\n", "tests/test_reader.py": "import app.reader\n",
        "app/adapters/patched.py": "class C:\n    pass\n",
        "tests/test_patched.py": 'def test_it(monkeypatch):\n    monkeypatch.setattr("app.adapters.patched.C", object)\n',
        "app/usecases/mirrored.py": "x = 1\n", "tests/usecases/test_mirrored.py": "x = 1\n",
        "app/old_debt.py": "x = 1\n", "ci/untested_modules_baseline.txt": "# debt\napp/old_debt.py\n",
        "app/__init__.py": "", "app/migrations/m.py": "x = 1\n"})
    _, r = run(new_repo, "untested_modules", files, *ARGS, "--exclude-part", "migrations")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 untested (all baselined)" in r.stdout


def test_no_tests_flag_is_2(new_repo) -> None:
    _, r = run(new_repo, "untested_modules", _files(), "--dir", "app:2")
    assert r.returncode == 2 and "no --tests" in r.stderr, r.stdout + r.stderr


def test_an_empty_tests_dir_is_2(new_repo) -> None:
    _, r = run(new_repo, "untested_modules", {f"app/pad_{i}.py": WAIVED for i in range(2)}, *ARGS)
    assert r.returncode == 2, r.stdout + r.stderr


def test_a_source_dir_under_its_floor_is_2(new_repo) -> None:
    repo, _ = run(new_repo, "untested_modules", _files(), *ARGS)
    assert again("untested_modules", repo, "--dir", "app:50", "--tests", "tests:1").returncode == 2
