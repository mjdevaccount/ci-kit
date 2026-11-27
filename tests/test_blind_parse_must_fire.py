"""ci-blind-parse must fire: a handler that answers a parse failure with `continue` -> 1, a loud
one -> 0, no handler at all (the rule cannot fire), a dir under its floor or an unparseable file -> 2."""
from __future__ import annotations

from _pylint_repo import pads, run

QUIET = ("import ast\n\ndef scan(srcs):\n    for s in srcs:\n        try:\n            ast.parse(s)\n"
         "        except SyntaxError:\n            continue\n")
LOUD = QUIET.replace("continue", "raise")
OWN = QUIET.replace("continue", "die(s)")


def test_a_quiet_parse_handler_fires(new_repo) -> None:
    _, r = run(new_repo, "blind_parse", {"scripts/q.py": QUIET, "scripts/l.py": LOUD}, "--dir", "scripts:2")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "scripts/q.py:7: a parse failure is swallowed -- `continue`" in r.stdout


def test_loud_handlers_pass(new_repo) -> None:
    _, r = run(new_repo, "blind_parse", {"scripts/l.py": LOUD, **pads("scripts", 1)}, "--dir", "scripts:2")
    assert r.returncode == 0 and "1 parse handler(s) across 2 file(s)" in r.stdout, r.stdout + r.stderr


def test_a_callers_own_exit_helper_counts_with_noreturn(new_repo) -> None:
    repo, r = run(new_repo, "blind_parse", {"scripts/o.py": OWN}, "--dir", "scripts")
    assert r.returncode == 1, r.stdout + r.stderr
    from _pylint_repo import again
    assert again("blind_parse", repo, "--dir", "scripts", "--noreturn", "die").returncode == 0


def test_no_parse_handler_is_2_the_rule_cannot_fire(new_repo) -> None:
    _, r = run(new_repo, "blind_parse", pads("scripts", 3), "--dir", "scripts:2")
    assert r.returncode == 2 and "parse handlers" in r.stderr, r.stdout + r.stderr


def test_a_dir_under_its_floor_is_2(new_repo) -> None:
    _, r = run(new_repo, "blind_parse", {"scripts/l.py": LOUD}, "--dir", "scripts:20")
    assert r.returncode == 2, r.stdout + r.stderr


def test_an_unparseable_file_is_2(new_repo) -> None:
    _, r = run(new_repo, "blind_parse", {"scripts/l.py": LOUD, "scripts/x.py": "def (:\n"}, "--dir", "scripts")
    assert r.returncode == 2 and "scripts/x.py" in r.stdout + r.stderr, r.stdout + r.stderr
