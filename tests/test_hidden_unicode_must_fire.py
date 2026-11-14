"""ci-hidden-unicode must fire: a planted invisible character exits 1, the escape exits 0, a file
it cannot read exits 2 naming the file and its fix. Text is built from code points (this file
holds none raw), on throwaway REAL git repos run the way a caller runs the tool."""
from __future__ import annotations

from pathlib import Path

import pytest
from conftest import NL, git, run_tool, write

from ci_kit import hidden_unicode as hu

BS = chr(92)


def _add(r: Path, files: dict[str, bytes | str]) -> Path:
    for rel, body in files.items():
        p = r / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            p.write_bytes(body)
        else:
            write(p, body)
    git(r, "add", "-A")
    return r


@pytest.mark.parametrize("cp", [0x01, 0x1F, 0x7F, 0x9F, 0xAD, 0x034F, 0x061C, 0x115F, 0x17B4, 0x17B5,
                                0x180B, 0x180E, 0x180F, 0x200B, 0x200F, 0x202A, 0x202E, 0x2060, 0x2064,
                                0x2066, 0x206F, 0x2800, 0x3164, 0xFE00, 0xFE0F, 0xFFA0, 0xFFF9, 0xFFFB,
                                0x1BCA0, 0x1D173, 0x1D17A, 0xE0000, 0xE0041, 0xE007F, 0xE0100, 0xE01EF])
def test_every_flagged_class_fires_mid_line(cp: int) -> None:
    assert hu.findings_in("ab" + chr(cp) + "cd") == [(1, 3, cp)]


@pytest.mark.parametrize("cp", [0x09, 0x0D, 0x20, 0x7E, 0xA0, 0xAC, 0xAE, 0x034E, 0x0350, 0x17B3,
                                0x17B6, 0x180A, 0x1810, 0x200A, 0x2010, 0x2028, 0x2029, 0x2065, 0x2070,
                                0x27FF, 0x2801, 0xFDFF, 0xFE10, 0xFFF8, 0xFFFC, 0x1BC9F, 0x1BCA4,
                                0x1D172, 0x1D17B, 0xE9, 0x2014, 0x26A0, 0x4E2D, 0xE0080, 0xE01F0])
def test_neighbours_and_ordinary_text_stay_quiet(cp: int) -> None:
    assert hu.findings_in("ab" + chr(cp) + "cd") == []


def test_a_bom_is_allowed_only_at_byte_zero() -> None:
    bom = chr(0xFEFF)
    assert hu.findings_in(bom + "x = 1" + NL) == []
    assert hu.findings_in("x" + bom) == [(1, 2, 0xFEFF)]
    assert hu.findings_in("x" + NL + bom + "y") == [(2, 1, 0xFEFF)]


def test_planted_character_exits_1_and_names_the_spot(new_repo) -> None:
    r = _add(new_repo(), {"docs/a.md": "ok" + NL + "soft" + chr(0xAD) + "hyphen" + NL,
                          "src/x.py": "x" + chr(0x3164) + " = 1" + NL})
    res = run_tool("hidden_unicode", r)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "docs/a.md:2:5: U+00AD SOFT HYPHEN (soft hyphen)" in res.stdout
    assert "src/x.py:1:2: U+3164" in res.stdout


def test_escapes_bom_and_binary_by_suffix_exit_0(new_repo) -> None:
    r = _add(new_repo(), {"a.py": chr(0xFEFF) + f"s = '{BS}u200b{BS}u00ad'" + NL,
                          "b.json": '{"k": "' + BS + 'ufe0f"}' + NL,
                          "logo.PNG": b"\x89PNG\x00\xff\xfe"})
    res = run_tool("hidden_unicode", r)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "2 tracked text file(s) clean (1 binary by suffix)" in res.stdout


def test_every_unreadable_file_is_named_with_its_fix_and_exits_2(new_repo) -> None:
    """Review gap (3): a binary under an unlisted suffix is exit 2, and the message says which
    file and which flag fixes it -- all of them in one run, not one per CI run."""
    r = _add(new_repo(), {"a.md": "fine" + NL, "model.blob": b"\x00\x01", "notes.md": b"caf\xe9" + b"\n",
                          "LICENSEDATA": b"\x00", "z.md": "x" + chr(0x200B) + NL})
    res = run_tool("hidden_unicode", r)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "model.blob holds a NUL byte" in res.stderr and "--binary-suffix .blob" in res.stderr
    assert "notes.md is not UTF-8" in res.stderr and "`.md` is a text format" in res.stderr
    assert "LICENSEDATA" in res.stderr and "has no suffix" in res.stderr
    assert "3 tracked file(s) it cannot read" in res.stderr
    assert "HIDDEN z.md:1:2" in res.stdout   # what it DID see is still shown


def test_the_named_fix_works(new_repo) -> None:
    r = _add(new_repo(), {"a.md": "fine" + NL, "model.blob": b"\x00\x01"})
    assert run_tool("hidden_unicode", r).returncode == 2
    res = run_tool("hidden_unicode", r, "--binary-suffix", ".blob")
    assert res.returncode == 0, res.stdout + res.stderr


def test_a_text_suffix_cannot_be_declared_binary(new_repo) -> None:
    r = _add(new_repo(), {"a.md": "x" + chr(0x202E) + NL})
    res = run_tool("hidden_unicode", r, "--binary-suffix", ".md")
    assert res.returncode == 2 and "text format" in res.stderr


def test_utf16_text_is_not_skipped_as_binary(new_repo) -> None:
    """Sniffing NUL to skip would let a UTF-16 .md through unread: with a BOM it is not UTF-8
    (exit 2); without one its NULs decode and are findings (exit 1). Never 0."""
    r = _add(new_repo(), {"a.md": "ok" + NL, "b.md": ("x" + chr(0x202E)).encode("utf-16")})
    res = run_tool("hidden_unicode", r)
    assert res.returncode == 2 and "b.md is not UTF-8" in res.stderr, res.stdout + res.stderr
    r2 = _add(new_repo("r2"), {"b.md": ("x" + chr(0x202E)).encode("utf-16-le")})
    res = run_tool("hidden_unicode", r2)
    assert res.returncode == 1 and "U+0000" in res.stdout, res.stdout + res.stderr


def test_a_raw_nul_in_a_text_format_is_a_finding(new_repo) -> None:
    """trade-ui / TradingEdge.Scraper, 2026-11-14: a NUL typed raw as a key separator in a .ts /
    .cs string. It decodes, the suffix says text, so it is a hidden U+0000 -- write the escape."""
    r = _add(new_repo(), {"src/k.ts": "const k = `${a}" + chr(0) + "${b}`" + NL})
    res = run_tool("hidden_unicode", r)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "src/k.ts:1:16: U+0000" in res.stdout


def test_nothing_readable_exits_2(new_repo) -> None:
    r = _add(new_repo(), {"logo.png": b"\x00"})
    res = run_tool("hidden_unicode", r)
    assert res.returncode == 2 and "read nothing" in res.stderr


def test_not_a_checkout_exits_2(tmp_path: Path) -> None:
    res = run_tool("hidden_unicode", tmp_path)
    assert res.returncode == 2, res.stdout + res.stderr


def test_selftest_passes_and_dies_to_a_dropped_range(monkeypatch) -> None:
    assert hu.selftest() == []
    monkeypatch.setattr(hu, "_HIDDEN", hu.re.compile("[" + chr(0x202A) + "-" + chr(0x202E) + "]"))
    fails = hu.selftest()
    assert any("MISSED Braille blank" in f for f in fails), fails
    assert any(f.startswith("bad:") for f in fails), fails   # the end-to-end case dies too


def test_selftest_dies_when_the_bom_exemption_spreads(monkeypatch) -> None:
    real = hu.findings_in
    monkeypatch.setattr(hu, "findings_in", lambda t: [f for f in real(t) if f[2] != 0xFEFF])
    assert any("BOM at line 2" in f for f in hu.selftest())


def test_selftest_dies_when_unreadable_files_are_skipped(monkeypatch) -> None:
    monkeypatch.setattr(hu, "_read_one", lambda path, rel, out: path.read_bytes().decode("utf-8", "replace"))
    fails = hu.selftest()
    assert any(f.startswith("blind-nul:") for f in fails) and any(f.startswith("blind-latin1:") for f in fails), fails


def test_cli_selftest_exit_codes(new_repo) -> None:
    res = run_tool("hidden_unicode", new_repo(), "--selftest")
    assert res.returncode == 0 and "selftest OK" in res.stdout, res.stdout + res.stderr
