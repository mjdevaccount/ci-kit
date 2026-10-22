"""ci_kit.astread -- both directions on every claim: good input parses, bad input exits 2 and says so.

Exit 2 is the point. A parse failure answered with `continue` / `return []` is byte-identical to
"read and clean"; one answered through the findings channel (exit 1) reads as a violation. Only 2
says "this gate did not look".
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from conftest import NL

from ci_kit import astread

BOM = bytes([0xEF, 0xBB, 0xBF])


def _exit_code(fn) -> int | None:
    try:
        fn()
    except SystemExit as e:
        return e.code
    return None


def test_valid_source_parses() -> None:
    assert isinstance(astread.parse(f"x = 1{NL}", "probe.py", gate="probe"), ast.Module)


def test_a_bomd_file_parses_not_exit_2(tmp_path: Path) -> None:
    """The opposite failure: calling a healthy file unreadable. The BOM is what started this."""
    p = tmp_path / "bomd.py"
    p.write_bytes(BOM + f"x = 1{NL}".encode())
    assert isinstance(astread.parse_file(p, gate="probe"), ast.Module)
    assert astread.read_text(p, gate="probe") == f"x = 1{NL}"


def test_unparseable_file_exits_2_naming_file_and_gate(tmp_path: Path, capsys) -> None:
    p = tmp_path / "broken.py"
    p.write_text(f"def f(:{NL}", encoding="utf-8")
    assert _exit_code(lambda: astread.parse_file(p, gate="probe-gate")) == 2
    err = capsys.readouterr().err
    assert "broken.py" in err, "the exit-2 message must name the file it could not read"
    assert "probe-gate" in err, "the exit-2 message must name the gate that went blind"
    assert "DID NOT FINISH LOOKING" in err


def test_missing_file_exits_2_not_a_traceback(tmp_path: Path) -> None:
    """An unhandled OSError exits 1 -- the FINDINGS code."""
    assert _exit_code(lambda: astread.parse_file(tmp_path / "nope.py", gate="probe")) == 2


def test_undecodable_bytes_exit_2_never_replaced(tmp_path: Path) -> None:
    """`errors='replace'` would hand back text that parses into a tree the file does not contain."""
    p = tmp_path / "binary.py"
    p.write_bytes(bytes([120, 32, 61, 32, 34, 255, 254, 34, 10]))
    assert _exit_code(lambda: astread.read_text(p, gate="probe")) == 2


def test_a_nul_byte_is_exit_2_not_a_valueerror_traceback() -> None:
    assert _exit_code(lambda: astread.parse("x = 1" + chr(0), "probe.py", gate="probe")) == 2


def test_blind_never_returns_and_exits_2() -> None:
    assert _exit_code(lambda: astread.blind("probe.py", SyntaxError("nope"), gate="probe")) == 2


def test_origin_overrides_the_path_in_the_message(tmp_path: Path, capsys) -> None:
    p = tmp_path / "x.py"
    p.write_text(f"def (:{NL}", encoding="utf-8")
    assert _exit_code(lambda: astread.parse_file(p, gate="g", origin="flows/a.yaml::rawscript")) == 2
    assert "flows/a.yaml::rawscript" in capsys.readouterr().err


def test_rel_is_relative_inside_root_and_never_raises(tmp_path: Path) -> None:
    inside = tmp_path / "svc" / "a.py"
    assert astread.rel(inside, root=tmp_path) == "svc/a.py"
    outside = Path("/elsewhere/b.py")
    assert astread.rel(outside, root=tmp_path) == outside.as_posix()


def test_encoding_agrees_with_site_lint() -> None:
    from ci_kit import site_lint
    assert astread.ENCODING == site_lint.ENCODING == "utf-8-sig"


@pytest.mark.parametrize("bad", [f"def f(:{NL}", f"  x = 1{NL}", f"if x:{NL}y = 1{NL}"])
def test_every_syntax_error_subclass_is_exit_2(bad: str) -> None:
    """IndentationError / TabError are SyntaxError subclasses: one handler covers them."""
    assert _exit_code(lambda: astread.parse(bad, "p.py", gate="g")) == 2
