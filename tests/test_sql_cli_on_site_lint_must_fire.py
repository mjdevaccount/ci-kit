"""The five SQL gates' CLIs run on `site_lint.check` -- and what that buys, proved per gate.

The per-gate must-fire files prove 0 / 1 / 2 on real repos. This one proves what moving the CLI
onto the kit's scaffold changed:
  - a scan that CRASHES exits 2 (DID NOT FINISH LOOKING). Before, it was a traceback: exit 1, the
    findings code, so a broken gate and a dirty tree looked the same;
  - `--selftest` works when a repo wraps the gate with its own `Config` (it was standalone-only:
    a wrapper's `--selftest` was an argparse error);
  - a flag argparse accepts by abbreviation still selects its mode (`check` reads argv by membership);
  - and, structurally, each module's `main` reaches a `site_lint.check` call -- the same question a
    consumer's scaffold-coverage gate asks of a wrapper that delegates here.
"""
from __future__ import annotations

import ast
import json
import pathlib

import pytest
from _sqlrepo import tracked_repo
from conftest import NL

from ci_kit.sql import _cli, casts, column_comments, decimal_reads, phantom_columns, pit_reads

GATES = (casts, column_comments, decimal_reads, phantom_columns, pit_reads)
READ = 'q("SELECT pnl_usd FROM t WHERE ts < now()")' + NL
TABLE = NL.join(("CREATE TABLE s.x (", "    id bigint,", "    mae double precision -- max adverse excursion",
                 ");", ""))


def _boom(*_a, **_k):
    raise RuntimeError("the parser fell over")


def _repo(new_repo) -> pathlib.Path:
    return tracked_repo(new_repo, {"app/repo.py": READ, "numeric.txt": "s.t.pnl_usd" + NL,
                                   "migrations/010_x.sql": TABLE,
                                   "schema.json": json.dumps({"s.t": ["pnl_usd"]})})


def _cfgs(repo: pathlib.Path) -> dict:
    return {
        casts: casts.Config(root=repo, scan_dirs=("app",), required_dirs=("app",)),
        column_comments: column_comments.Config(root=repo),
        decimal_reads: decimal_reads.Config(root=repo, scan_dirs=("app",), cols=pathlib.Path("numeric.txt")),
        phantom_columns: phantom_columns.Config(root=repo),
        pit_reads: pit_reads.Config(root=repo, scan_dirs=("app",)),
    }


def _main(mod, argv: list[str], cfg) -> int:
    if mod is phantom_columns:
        return mod.main([*argv, "--schema", str(cfg.root / "schema.json")], cfg, scan_dir="app")
    return mod.main(argv, cfg)


#: the module-level function each gate's scan reaches, patched to crash
CRASH_AT = {casts: "scan", column_comments: "offenders", decimal_reads: "scan",
            phantom_columns: "scan", pit_reads: "scan_text"}


@pytest.mark.parametrize("mod", GATES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_a_crashing_scan_exits_2_not_1(mod, new_repo, monkeypatch, capsys) -> None:
    cfg = _cfgs(_repo(new_repo))[mod]
    monkeypatch.setattr(mod, CRASH_AT[mod], _boom)
    assert _main(mod, [], cfg) == 2
    assert "DID NOT FINISH LOOKING - RuntimeError: the parser fell over" in capsys.readouterr().out


@pytest.mark.parametrize("mod", GATES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_selftest_runs_under_a_wrapping_config(mod, new_repo, capsys) -> None:
    cfg = _cfgs(_repo(new_repo))[mod]
    assert _main(mod, ["--selftest"], cfg) == 0
    assert "selftest" in capsys.readouterr().out


@pytest.mark.parametrize("mod", GATES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_a_failing_selftest_exits_1(mod, new_repo, monkeypatch, capsys) -> None:
    cfg = _cfgs(_repo(new_repo))[mod]
    monkeypatch.setattr(mod, "selftest", lambda _cfg: ["planted failure"])
    assert _main(mod, ["--selftest"], cfg) == 1


def test_an_abbreviated_write_flag_still_writes(new_repo, capsys) -> None:
    repo = _repo(new_repo)
    cfg = _cfgs(repo)[pit_reads]
    assert pit_reads.main(["--write"], cfg) == 0, "argparse took --write; check must see the mode"
    assert "app/repo.py|now()|1" in (repo / cfg.baseline).read_text(encoding="utf-8")
    assert pit_reads.main([], cfg) == 0


def test_the_findings_and_clean_codes_are_unchanged(new_repo) -> None:
    repo = _repo(new_repo)
    cfg = _cfgs(repo)[pit_reads]
    assert pit_reads.main([], cfg) == 1
    assert pit_reads.main(["--write-baseline"], cfg) == 0
    assert pit_reads.main([], cfg) == 0


def _reaches_check(source: str) -> bool:
    """`main`, or a module function it calls (transitively), calls `site_lint.check`."""
    tree = ast.parse(source)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    todo, seen = ["main"], set()
    while todo:
        name = todo.pop()
        if name in seen or name not in funcs:
            continue
        seen.add(name)
        for n in ast.walk(funcs[name]):
            if isinstance(n, ast.Call):
                if ast.unparse(n.func) == "site_lint.check":
                    return True
                if isinstance(n.func, ast.Name):
                    todo.append(n.func.id)
    return False


@pytest.mark.parametrize("mod", GATES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_main_is_on_the_scaffold(mod) -> None:
    src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    assert _reaches_check(src), f"{mod.__name__}.main hand-rolls its run again"


def test_the_structural_check_can_fail() -> None:
    # both directions: argparse deciding the exit, or check called only from a helper main never
    # reaches, must read False; a helper main DOES reach must read True
    hand = "def main():" + NL + "    return argparse.ArgumentParser().parse_args() and 0"
    stray = "def _unused():" + NL + "    site_lint.check([])" + NL + hand
    helper = "def _go():" + NL + "    return site_lint.check([])" + NL + "def main():" + NL + "    return _go()"
    assert not _reaches_check(hand)
    assert not _reaches_check(stray)
    assert _reaches_check(helper)


@pytest.mark.parametrize("mod", GATES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_a_passing_selftest_never_says_it_went_blind(mod, new_repo, capsys) -> None:
    # Each selftest PROBES its blind path (an unparseable SQL value must exit 2). That probe's
    # "DID NOT FINISH LOOKING" line is the mechanism working; printed on a green selftest it reads
    # as a blind gate to anyone grepping the job log for the phrase.
    cfg = _cfgs(_repo(new_repo))[mod]
    assert _main(mod, ["--selftest"], cfg) == 0
    out = capsys.readouterr()
    assert "DID NOT FINISH LOOKING" not in out.out + out.err


def _exit(code: int) -> None:
    raise SystemExit(code)


def test_blind_probe_both_directions(capsys) -> None:
    kw = {"missed": "missed", "wrong": "exited {code}"}
    assert _cli.blind_probe(lambda: (print("x: DID NOT FINISH LOOKING", file=__import__("sys").stderr),
                                     _exit(2)), **kw) is None
    assert "DID NOT FINISH LOOKING" not in capsys.readouterr().err, "the probe's own line leaked"
    assert _cli.blind_probe(lambda: None, **kw) == "missed", "a probe that returns did not go blind"
    assert _cli.blind_probe(lambda: _exit(1), **kw) == "exited 1", "exit 1 is the findings code, not 2"


@pytest.mark.parametrize(("flag", "metavar"), [("--scan-dir", "--scan-dir DIR"), ("--root", "--root ROOT")])
def test_phantom_usage_keeps_the_wrappers_flag_spelling(flag, metavar, new_repo, capsys) -> None:
    cfg = _cfgs(_repo(new_repo))[phantom_columns]
    with pytest.raises(SystemExit):
        phantom_columns.main(["--help"], cfg, scan_dir_flag=flag)
    assert metavar in capsys.readouterr().out
