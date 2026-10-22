"""ci_kit.manual_lint -- the agent-manual resolve-today gate, driven like a caller drives it.

Every check both ways on throwaway real git repos: a pointer that does not resolve -> 1, the
same manual with it resolving -> 0, no manual / no git / an empty index / a flag aimed at nothing
-> 2. Plus the shape rules that keep it quiet (fences, ``double`` spans, routes, flags, prose
with slashes), the line pin, gitignored paths, and siblings present vs absent.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from conftest import git, run_tool, write

from ci_kit import manual_lint

BASE = {"service/app.py": "x = 1\n", "service/admin/router.py": "x = 1\n",
        "docs/README.md": "# index\nRUNBOOK\n", "docs/RUNBOOK.md": "# rb\n",
        ".gitignore": "secrets/\n"}


def _repo(new_repo, manual: str, extra: dict[str, str] | None = None, name: str = "r") -> Path:
    r = new_repo(name)
    for rel, text in {**BASE, **(extra or {}), "CLAUDE.md": manual}.items():
        write(r / rel, text)
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


def _lint(r: Path, *args: str):
    return run_tool("manual_lint", r, *args)


def test_good_every_pointer_resolves_is_0(new_repo) -> None:
    r = _repo(new_repo, "See `service/app.py`, `service/admin/`, `router.py` and `docs/RUNBOOK.md:12`.\n")
    p = _lint(r)
    assert p.returncode == 0, p.stdout
    assert "CLAUDE.md lint: clean (0 warning(s))" in p.stdout


@pytest.mark.parametrize("ptr,needle", [
    ("service/gone_router.py", "`service/gone_router.py` referenced in CLAUDE.md but not found"),
    ("gone_router.py", "`gone_router.py` is named in CLAUDE.md but no file or directory"),
    ("service/gone/", "`service/gone/` referenced in CLAUDE.md but not found"),
    ("service/app.py:96-204", None),          # the line pin is stripped, so this one resolves
    ("service/gone.py:96-204", "`service/gone.py` referenced"),
])
def test_bad_pointer_is_1(new_repo, ptr: str, needle: str | None) -> None:
    r = _repo(new_repo, f"The router lives in `{ptr}`.\n")
    p = _lint(r)
    if needle is None:
        assert p.returncode == 0, p.stdout
    else:
        assert p.returncode == 1 and needle in p.stdout, p.stdout


@pytest.mark.parametrize("span", [
    "/admin/run/snapshot",        # an HTTP route
    "#/work",                     # a cockpit hash route
    "--scan/--write-baseline",    # a flag list
    "tier1/tier2/tier3",          # prose: first segment is not a tracked name
    "America/New_York",           # a timezone, by the same rule
    "timescale/timescaledb-ha:pg17",  # an image tag
    "make lint-docs",             # a command
    "tests/X.py",                 # the placeholder convention
    "0.5/365",                    # a ratio
    ".py",                        # a bare extension
    "(exit-entry)/risk.py",       # a formula
    "https://example.com/a.md",   # a URL
    "ops.jobs",                   # a schema-qualified table
    "../sibling/x.py",            # a relative sibling spelling
])
def test_shapes_that_are_not_pointers_are_silent(new_repo, span: str) -> None:
    r = _repo(new_repo, f"Use `{span}` here.\n")
    p = _lint(r)
    assert p.returncode == 0, p.stdout


def test_fences_and_double_backtick_spans_do_not_shift_the_pairing(new_repo) -> None:
    """Both pairing bugs left real pointers unseen while the gate reported clean."""
    manual = ("```\nservice/fenced_example_only.py\n```\n"
              "Quote it as ``a `tick` inside``, then `service/gone_after.py` is a claim.\n")
    p = _lint(_repo(new_repo, manual))
    assert p.returncode == 1
    assert "service/gone_after.py" in p.stdout and "fenced_example_only" not in p.stdout


def test_a_gitignored_path_is_never_broken(new_repo) -> None:
    assert _lint(_repo(new_repo, "Creds live in `secrets/api.json`.\n")).returncode == 0


def test_resolution_is_the_index_not_the_disk(new_repo) -> None:
    """An untracked file on this disk is absent from CI's clean checkout."""
    r = _repo(new_repo, "See `service/local_only.py`.\n")
    write(r / "service/local_only.py", "x = 1\n")
    assert _lint(r).returncode == 1
    git(r, "add", "-A")
    assert _lint(r).returncode == 0


def test_suffix_match_accepts_a_path_tail_only_when_asked(new_repo) -> None:
    r = _repo(new_repo, "See `admin/router.py`.\n")
    assert _lint(r).returncode == 1
    assert _lint(r, "--suffix-match").returncode == 0


def test_exempt_prefix(new_repo) -> None:
    r = _repo(new_repo, "Flow `service/flows/nightly.py` runs.\n")
    assert _lint(r).returncode == 1
    assert _lint(r, "--exempt-prefix", "service/flows/").returncode == 0


def test_sibling_present_is_checked_absent_is_silent(new_repo, tmp_path: Path) -> None:
    manual = "Schema card: `other/docs/CARD.md`.\n"
    r = _repo(new_repo, manual)
    assert _lint(r).returncode == 1                           # undeclared: a claim about THIS repo
    assert _lint(r, "--sibling", "other").returncode == 0     # declared, absent: cannot see it
    write(tmp_path / "other/README.md", "x\n")                # present, but the file is not
    p = _lint(r, "--sibling", "other")
    assert p.returncode == 1 and "names a sibling-repo file that does not exist" in p.stdout
    write(tmp_path / "other/docs/CARD.md", "x\n")
    assert _lint(r, "--sibling", "other").returncode == 0
    # a two-segment pointer: its grandparent always exists, the sibling ROOT must be what is tested
    r2 = _repo(new_repo, "See `ghost/README.md`.\n", name="r2")
    assert _lint(r2, "--sibling", "ghost").returncode == 0


def test_migration_range_both_ways(new_repo) -> None:
    extra = {f"migrations/{n:03d}_m.sql": "select 1;\n" for n in (1, 2, 150)}
    stale = _repo(new_repo, "Migrations 001-149 are the schema.\n", extra)
    p = _lint(stale, "--migrations", "migrations")
    assert p.returncode == 1 and "newest on disk is 150" in p.stdout
    ok = _repo(new_repo, "Migrations 001-150, ports 3000-3004, 30-40 percent.\n", extra, name="r2")
    assert _lint(ok, "--migrations", "migrations").returncode == 0
    p = _lint(ok, "--migrations", "nowhere")
    assert p.returncode == 2


def test_line_budget_is_a_warning_never_a_failure(new_repo) -> None:
    r = _repo(new_repo, "line\n" * 12)
    p = _lint(r, "--line-budget", "10")
    assert p.returncode == 0 and "CLAUDE.md is 12 lines (> 10" in p.stdout
    assert "(0 warning(s))" in _lint(r, "--line-budget", "12").stdout


def test_empty_package_both_ways_and_blind(new_repo) -> None:
    r = _repo(new_repo, "x\n", {"service/llm/__init__.py": ""})
    assert _lint(r, "--empty-package", "service/llm").returncode == 0
    write(r / "service/llm/creep.py", "x = 1\n")
    git(r, "add", "-A")
    p = _lint(r, "--empty-package", "service/llm")
    assert p.returncode == 1 and "['creep.py']" in p.stdout
    assert _lint(r, "--empty-package", "service/nope").returncode == 2


def test_ref_dir_pointers_and_stamp_warn(new_repo) -> None:
    ref = {"docs/reference/MATH.md": "LAST-VERIFIED: 2026-10-22\nsee `admin/router.py:3`\n",
           "docs/reference/FLOW.md": "see `service/gone.py`\n"}
    r = _repo(new_repo, "x\n", ref)
    p = _lint(r, "--ref-dir", "docs/reference", "--stamp", "LAST-VERIFIED:")
    assert p.returncode == 0, p.stdout                       # warnings never fail
    assert "docs/reference/FLOW.md has no `LAST-VERIFIED:` stamp" in p.stdout
    assert "docs/reference/FLOW.md: broken pointer `service/gone.py`" in p.stdout
    assert "MATH.md" not in p.stdout                          # suffix-matched, stamped
    assert _lint(r, "--ref-dir", "docs/empty").returncode == 2


def test_index_must_be_total(new_repo) -> None:
    r = _repo(new_repo, "x\n")
    assert _lint(r, "--index", "docs/README.md").returncode == 0
    write(r / "docs/NEW-THING.md", "x\n")
    git(r, "add", "-A")
    p = _lint(r, "--index", "docs/README.md")
    assert p.returncode == 1 and "docs/NEW-THING.md not in docs/README.md index" in p.stdout
    assert _lint(r, "--index", "docs/NOPE.md").returncode == 2


def test_args_file(new_repo) -> None:
    r = _repo(new_repo, "See `admin/router.py`.\n", {"lint.args": "--suffix-match\n"})
    assert _lint(r).returncode == 1
    assert _lint(r, "@lint.args").returncode == 0


def test_blind_no_manual_is_2(new_repo) -> None:
    r = _repo(new_repo, "x\n")
    p = _lint(r, "--manual", "AGENTS.md")
    assert p.returncode == 2 and "no AGENTS.md" in p.stderr


def test_blind_outside_git_is_2(tmp_path: Path) -> None:
    write(tmp_path / "CLAUDE.md", "See `a.py`.\n")
    assert run_tool("manual_lint", tmp_path).returncode == 2


def test_blind_empty_index_is_2(new_repo, capsys) -> None:
    r = new_repo()
    write(r / "CLAUDE.md", "See `a.py`.\n")
    assert manual_lint.main(["--root", str(r)]) == 2
    assert "EMPTY" in capsys.readouterr().err
