"""ci-test-standins must fire: a lambda / local def over a project callable -> 1, a reasonless
waiver -> 1, create_autospec / a reasoned waiver / a baselined file -> 0; no --package or a dir
under its floor -> 2."""
from __future__ import annotations

from _pylint_repo import again, pads, run
from conftest import write

ARGS = ("--dir", "tests:2", "--package", "app")
LAMBDA = ("from app import feed as src\n\n\nasync def test_it(monkeypatch):\n"
          '    monkeypatch.setattr(src, "run", lambda symbols, period: {"rows": 1})\n')


def _files(**extra: str) -> dict[str, str]:
    return {**pads("tests", 2), **extra}


def test_a_lambda_over_a_project_callable_fires(new_repo) -> None:
    _, r = run(new_repo, "standins", _files(**{"tests/test_feed.py": LAMBDA}), *ARGS)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "tests/test_feed.py: 1 site(s), baseline 0" in r.stdout


def test_a_dotted_string_target_and_a_local_def_fire(new_repo) -> None:
    conf = ("import pytest\n\n\nasync def _admit(c):\n    return True\n\n\n@pytest.fixture(autouse=True)\n"
            'def _wired(monkeypatch):\n    monkeypatch.setattr("app.db.maker", lambda: None)\n'
            '    monkeypatch.setattr("app.gate.admit", _admit)\n')
    _, r = run(new_repo, "standins", _files(**{"tests/conftest.py": conf}), *ARGS)
    assert r.returncode == 1 and "tests/conftest.py: 2 site(s), baseline 0" in r.stdout, r.stdout + r.stderr


def test_a_bare_waiver_is_reported_and_a_reasoned_one_honoured(new_repo) -> None:
    repo, r = run(new_repo, "standins", _files(**{"tests/test_feed.py": LAMBDA.rstrip("\n") + "  # standin-ok:\n"}), *ARGS)
    assert r.returncode == 1 and "bare `# standin-ok:`" in r.stdout + r.stderr, r.stdout + r.stderr
    write(repo / "tests/test_feed.py", LAMBDA.rstrip("\n") + "  # standin-ok: the fake must not see the real args\n")
    assert again("standins", repo, *ARGS).returncode == 0


def test_create_autospec_and_a_foreign_target_pass(new_repo) -> None:
    good = ("from unittest import mock\n\nimport os\nfrom app import feed as src\n\n\nasync def test_it(monkeypatch):\n"
            '    monkeypatch.setattr(src, "run", mock.create_autospec(src.run, return_value={}))\n'
            '    monkeypatch.setattr(os, "getcwd", lambda: "/")\n')
    _, r = run(new_repo, "standins", _files(**{"tests/test_feed.py": good}), *ARGS)
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_baselined_file_passes_and_growth_fails(new_repo) -> None:
    repo, r = run(new_repo, "standins", _files(**{"tests/test_feed.py": LAMBDA}), *ARGS, "--write-baseline")
    assert r.returncode == 0 and again("standins", repo, *ARGS).returncode == 0
    write(repo / "tests/test_feed.py", LAMBDA + '    monkeypatch.setattr(src, "stop", lambda: None)\n')
    r = again("standins", repo, *ARGS)
    assert r.returncode == 1 and "2 site(s), baseline 1" in r.stdout, r.stdout + r.stderr


def test_no_package_and_an_empty_dir_are_2(new_repo) -> None:
    repo, r = run(new_repo, "standins", _files(), "--dir", "tests:2")
    assert r.returncode == 2 and "--package" in r.stderr, r.stdout + r.stderr
    assert again("standins", repo, "--dir", "tests:50", "--package", "app").returncode == 2
