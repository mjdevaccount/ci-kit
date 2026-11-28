"""ci-sql-placement must fire: SQL outside the owners (a router INSERT through text(, a multi-line
f-string DELETE, a TRUNCATE chain) -> 1, growth past the baseline -> 1, owners / exempt paths /
prose -> 0; no --owner, a dir under its floor, unparseable python or SQL -> 2."""
from __future__ import annotations

import pytest

pytest.importorskip("sqlglot")

from _pylint_repo import again, pads, run  # noqa: E402
from conftest import git, write  # noqa: E402

ARGS = ("--dir", "app:2", "--owner", "app/repositories/", "--owner", "app/content/*/repositories/",
        "--exempt", "app/lint_")
MOD = "sql.placement"


def _files(**extra: str) -> dict[str, str]:
    return {**pads("app", 2), **extra}


def test_sql_outside_the_owners_fires(new_repo) -> None:
    router = ('from sqlalchemy import text\n\n\nasync def probe(s):\n'
              '    await s.execute(text("INSERT INTO ops.probe (a) VALUES (:a)"), {"a": 1})\n')
    fstr = ('S = "q"\n\n\ndef purge(cur, t, d):\n    cur.execute(f"""\n        DELETE\n'
            '          FROM {S}.{t}\n         WHERE d = %s\n    """, (d,))\n')
    trunc = 'T = ("q.a", "q.b")\n\n\ndef reset(c):\n    c.execute("TRUNCATE " + ", ".join(T) + " RESTART IDENTITY CASCADE")\n'
    _, r = run(new_repo, MOD, _files(**{"app/admin/router.py": router, "app/feed.py": fstr, "app/reset.py": trunc}), *ARGS)
    assert r.returncode == 1, r.stdout + r.stderr
    for want in ("NEW: app/admin/router.py holds 2 SQL site(s)", "NEW: app/feed.py holds 1", "NEW: app/reset.py holds 1"):
        assert want in r.stdout, r.stdout
    assert "app/repositories/" in r.stdout + r.stderr


def test_owners_exempt_prose_and_a_baselined_file_pass_and_growth_fails(new_repo) -> None:
    watch = ('"""DELETE FROM q.x in a docstring is prose."""\nfrom sqlalchemy import text\n\n'
             '# UPDATE q.x SET a = 1 -- a comment\nFRESH = text("SELECT max(ts) FROM q.x")\nLABEL = "truncate disabled"\n\n\n'
             'def merge(d):\n    d.update({"a": 1})\n    return d\n')
    files = _files(**{"app/repositories/x.py": 'U = "INSERT INTO q.x (a) VALUES (1)"\nP = "DELETE FROM q.x"\n',
                      "app/content/mail/repositories/y.py": 'Q = "UPDATE q.y SET a = 1"\n',
                      "app/lint_selftest.py": 'Q = "TRUNCATE q.z"\n', "app/watch.py": watch,
                      "ci/sql_placement_baseline.txt": "# debt\napp/watch.py|1\n"})
    repo, r = run(new_repo, MOD, files, *ARGS)
    assert r.returncode == 0, r.stdout + r.stderr
    write(repo / "app/watch.py", watch + 'STALE = text("SELECT min(ts) FROM q.x")\n')
    r = again(MOD, repo, *ARGS)
    assert r.returncode == 1 and "GREW: app/watch.py now 2 SQL site(s) outside the repositories, baseline 1" in r.stdout


def test_no_owner_and_a_dir_under_its_floor_are_2(new_repo) -> None:
    repo, r = run(new_repo, MOD, _files(), "--dir", "app:2")
    assert r.returncode == 2 and "no --owner" in r.stderr, r.stdout + r.stderr
    assert again(MOD, repo, "--dir", "app:50", "--owner", "app/repositories/").returncode == 2


@pytest.mark.parametrize("body", ['def broken(:\n    "DELETE FROM q.x"\n', 'Q = "INSERT INTO q.x (a VALUES (1)"\n'])
def test_unparseable_python_or_sql_is_2(new_repo, body) -> None:
    repo, _ = run(new_repo, MOD, _files(), *ARGS)
    write(repo / "app/broken.py", body)
    git(repo, "add", "-A")
    r = again(MOD, repo, *ARGS)
    assert r.returncode == 2 and "app/broken.py" in r.stdout + r.stderr, r.stdout + r.stderr
