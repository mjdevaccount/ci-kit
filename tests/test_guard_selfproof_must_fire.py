"""ci_kit.guard_selfproof -- the guard-of-guards, driven like a caller drives it.

Bad -> 1 (a NEW unproven guard; a selftest that reports, raises, or returns an int; an
unimportable guard), good -> 0 (proved, or baselined), blind -> 2 (no scripts dir, zero guards,
node missing while a JS guard exists). Plus the two loader regressions that made the old copy
blame a guard for its own loader (a @dataclass at import time, and one built inside selftest()).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from conftest import git, run_tool, write

from ci_kit import guard_selfproof

PROVEN = "def selftest() -> list[str]:\n    return []\n"
UNPROVEN = "def check() -> int:\n    return 0\n"
REPORTS = "def selftest() -> list[str]:\n    return ['a known-bad input was accepted']\n"
RAISES = "def selftest() -> list[str]:\n    raise RuntimeError('boom')\n"
RETURNS_INT = "def selftest() -> int:\n    return 0\n"
UNIMPORTABLE = "import no_such_module_anywhere\n"
DATACLASS = (
    "from __future__ import annotations\n"
    "from dataclasses import dataclass\n\n"
    "@dataclass(frozen=True)\n"
    "class Finding:\n"
    "    where: str\n\n"
    "def selftest() -> list[str]:\n"
    "    return [] if Finding('x').where == 'x' else ['broken']\n"
)
DEFERRED_DATACLASS = (
    "from __future__ import annotations\n"
    "from dataclasses import dataclass\n\n"
    "def selftest() -> list[str]:\n"
    "    @dataclass(frozen=True)\n"
    "    class Finding:\n"
    "        where: str\n"
    "    return [] if Finding('x').where == 'x' else ['broken']\n"
)
SIBLING_IMPORT = "import _helper\n\ndef selftest() -> list[str]:\n    return [] if _helper.OK else ['no']\n"


def _repo(new_repo, files: dict[str, str]) -> Path:
    r = new_repo()
    for rel, text in files.items():
        write(r / rel, text)
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


def test_good_every_guard_proves_itself_is_0(new_repo) -> None:
    r = _repo(new_repo, {"scripts/lint_a.py": PROVEN, "scripts/lint_b.py": DATACLASS,
                         "scripts/lint_c.py": DEFERRED_DATACLASS,
                         "scripts/lint_d.py": SIBLING_IMPORT, "scripts/_helper.py": "OK = True\n"})
    p = run_tool("guard_selfproof", r)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "OK (4 guard(s) prove themselves, 0 baselined" in p.stdout


def test_bad_new_unproven_guard_is_1_and_baselining_it_is_0(new_repo) -> None:
    r = _repo(new_repo, {"scripts/lint_a.py": PROVEN, "scripts/lint_new.py": UNPROVEN})
    p = run_tool("guard_selfproof", r)
    assert p.returncode == 1
    assert "NEW guard(s) with no selftest: lint_new.py" in p.stderr
    assert run_tool("guard_selfproof", r, "--write-baseline").returncode == 0
    base = (r / "scripts/guard_selfproof_baseline.txt").read_text(encoding="utf-8")
    assert base.splitlines()[-1] == "lint_new.py" and base.startswith("# guard self-proof")
    p = run_tool("guard_selfproof", r)
    assert p.returncode == 0 and "1 baselined as unproven" in p.stdout


@pytest.mark.parametrize("body,needle", [
    (REPORTS, "lint_x.py: selftest reported:\n    a known-bad input was accepted"),
    (RAISES, "lint_x.py: selftest() raised RuntimeError('boom')"),
    (RETURNS_INT, "lint_x.py: selftest() returned 0, not the contract's list[str]"),
    (UNIMPORTABLE, "lint_x.py: could not be imported"),
])
def test_bad_selftest_shapes_are_1(new_repo, body: str, needle: str) -> None:
    """An int `0` is the dangerous one: falsy, so a truthiness read would count it PROVEN."""
    r = _repo(new_repo, {"scripts/lint_x.py": body})
    p = run_tool("guard_selfproof", r)
    assert p.returncode == 1, p.stdout + p.stderr
    assert needle in p.stderr


def test_a_baselined_guard_that_later_gains_a_selftest_says_the_baseline_can_shrink(new_repo) -> None:
    r = _repo(new_repo, {"scripts/lint_a.py": PROVEN,
                         "scripts/guard_selfproof_baseline.txt": "# h\nlint_a.py\nlint_gone.py\n"})
    p = run_tool("guard_selfproof", r)
    assert p.returncode == 0
    assert "baseline can shrink, no longer unproven: lint_a.py, lint_gone.py" in p.stdout


def test_discovery_reads_the_index_prefixes_and_skips_private_and_nested(new_repo) -> None:
    r = _repo(new_repo, {"scripts/lint_a.py": PROVEN, "scripts/_lint_private.py": UNPROVEN,
                         "scripts/check_b.py": UNPROVEN, "scripts/sub/lint_c.py": UNPROVEN})
    write(r / "scripts/lint_untracked.py", UNPROVEN)      # a walk would see it; CI would not
    py, js = guard_selfproof.find_guards(r, "scripts", ("lint_",), js=False)
    assert [p.name for p in py] == ["lint_a.py"] and js == []
    py, _ = guard_selfproof.find_guards(r, "scripts", ("lint", "check"), js=False)
    assert [p.name for p in py] == ["check_b.py", "lint_a.py"]
    assert run_tool("guard_selfproof", r).returncode == 0
    p = run_tool("guard_selfproof", r, "--prefix", "lint", "--prefix", "check")
    assert p.returncode == 1 and "check_b.py" in p.stderr


def test_args_file_and_custom_scripts_dir(new_repo) -> None:
    r = _repo(new_repo, {"tools/lint_a.py": PROVEN, "ci.args": "--scripts\ntools\n"})
    p = run_tool("guard_selfproof", r, "@ci.args")
    assert p.returncode == 0, p.stderr


def test_blind_no_scripts_dir_is_2(new_repo) -> None:
    r = _repo(new_repo, {"README.md": "x\n"})
    p = run_tool("guard_selfproof", r)
    assert p.returncode == 2 and "DID NOT FINISH LOOKING" in p.stderr


def test_blind_zero_guards_is_2(new_repo) -> None:
    r = _repo(new_repo, {"scripts/helper.py": PROVEN})
    p = run_tool("guard_selfproof", r)
    assert p.returncode == 2 and "ZERO guards" in p.stderr


def test_blind_outside_git_is_2(tmp_path: Path) -> None:
    write(tmp_path / "scripts/lint_a.py", PROVEN)
    assert run_tool("guard_selfproof", tmp_path).returncode == 2


JS_PROVED = "if (process.argv.includes('--selftest')) { process.exit(0) }\n"
JS_FAILS = "if (process.argv.includes('--selftest')) { console.log('missed a bad input'); process.exit(1) }\n"
JS_NONE = "console.log('scan')\n"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_guards_both_directions(new_repo) -> None:
    r = _repo(new_repo, {"scripts/lint-ok.mjs": JS_PROVED, "scripts/check_types.d.mts": JS_FAILS})
    p = run_tool("guard_selfproof", r, "--prefix", "lint", "--prefix", "check", "--js")
    assert p.returncode == 0 and "OK (1 guard(s)" in p.stdout, p.stdout + p.stderr
    write(r / "scripts/lint-bad.mjs", JS_FAILS)
    write(r / "scripts/lint-none.mjs", JS_NONE)
    git(r, "add", "-A")
    p = run_tool("guard_selfproof", r, "--prefix", "lint", "--js")
    assert p.returncode == 1
    assert "lint-bad.mjs: selftest reported:\n    missed a bad input" in p.stderr
    assert "NEW guard(s) with no selftest: lint-none.mjs" in p.stderr
    # without --js the same tree has no guard at all: blind, never a clean pass
    assert run_tool("guard_selfproof", r, "--prefix", "lint").returncode == 2


def test_js_guard_with_node_missing_is_2(new_repo, monkeypatch, capsys) -> None:
    r = _repo(new_repo, {"scripts/lint-ok.mjs": JS_PROVED})
    monkeypatch.setattr(guard_selfproof, "NODE", "no-such-node-binary-anywhere")
    rc = guard_selfproof.main(["--root", str(r), "--prefix", "lint", "--js"])
    assert rc == 2 and "not found" in capsys.readouterr().err


def test_loader_keeps_a_successful_import_registered_and_drops_a_failed_one(tmp_path: Path) -> None:
    ok = tmp_path / "lint_reg_ok.py"
    write(ok, DATACLASS)
    guard_selfproof.load(ok)
    assert "_guard_lint_reg_ok" in sys.modules
    del sys.modules["_guard_lint_reg_ok"]
    bad = tmp_path / "lint_reg_bad.py"
    write(bad, "raise ValueError('half-initialised')\n")
    with pytest.raises(ValueError):
        guard_selfproof.load(bad)
    assert "_guard_lint_reg_bad" not in sys.modules
