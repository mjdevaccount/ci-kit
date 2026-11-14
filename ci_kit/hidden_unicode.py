#!/usr/bin/env python3
"""hidden_unicode -- no invisible character in any tracked text file: write the escape, or drop it.

WHY (trade-data, 2026-11-14). Renovate's Dependency Dashboard flagged a repo for "hidden Unicode
characters ... could be an attempt to smuggle text". Fourteen files held them, every one benign:
zero-width spaces typed as LITERALS in regexes and test strings, BOMs inside test strings,
variation selector 16 after emoji in docs, zero-width spaces inside captured email text. Benign
is the problem: a reviewer cannot tell a harmless invisible character from a bidi override that
makes code read differently from how it runs (Trojan Source, CVE-2021-42574), because both are
invisible in the diff. So the rule is "none, written raw" -- `\\u200b` in a Python or JSON string,
the bare emoji in markdown. Nothing here is specific to one repo, so it lives in the kit: a copy
per repo drifts (the first copy's review found six classes it missed).

WHY NOT A LINTER RULE. ruff `PLE2502`/`PLE2510-2515` read `.py` only, and even there have no rule
for FEFF mid-file, variation selectors, ZWNJ/ZWJ, word joiners, Hangul fillers or the tag block.
This gate reads EVERY tracked file -- markdown, JSON, YAML, SQL, templates, shell, C#.

FLAGGED (`RANGES` below, kept as code points so this file holds none): C0/C1 controls other than
TAB/LF/CR (NUL included: a raw NUL in a `.ts` or `.cs` string is a hidden character); U+00AD
soft hyphen; U+034F combining grapheme joiner; U+061C Arabic letter mark;
Hangul fillers U+115F/1160/3164/FFA0 (U+3164 is a valid, invisible Python identifier); Khmer
inherent vowels U+17B4/17B5; Mongolian variation selectors and vowel separator U+180B-180F;
U+200B-200F zero-width and directional marks; U+202A-202E bidi embed/override; U+2060-2064 word
joiner and invisible operators; U+2066-206F bidi isolates and the deprecated format controls;
U+2800 Braille blank; variation selectors U+FE00-FE0F and U+E0100-E01EF (the second a known
data-smuggling channel); U+FEFF anywhere but byte 0 (a leading BOM is an encoding, not text);
interlinear annotation U+FFF9-FFFB; shorthand format controls U+1BCA0-1BCA3; musical format
controls U+1D173-1D17A; tag characters U+E0000-E007F ("ASCII smuggling"). No waiver: write the
escape. NBSP, U+2028/2029 and ordinary non-ASCII (accents, dashes, CJK, a bare emoji) are text.

INPUT: `git ls-files` (never a filesystem walk); the NAMES are checked too (a bidi override in a
filename spoofs every listing and diff). A file is skipped as binary only when BOTH its suffix is
declared binary (`BINARY_SUFFIXES` plus `--binary-suffix`) AND its bytes are not UTF-8. Bytes that
decode are text whatever the suffix says, NUL included -- a raw NUL separator in a `.ts`/`.cs`
string is a U+0000 finding -- so no suffix, declared or not, can hide UTF-8 text from this gate.
(Under a declared binary suffix only the C0/C1 controls are let pass: an empty zip is 22 bytes
below 0x80 and decodes, and its NULs are its format, not hidden text.)
Never skipped by sniffing alone: a UTF-16 or NUL-salted text file is "Binary files differ" to git
and to the reviewer too. A file under an undeclared suffix that is not UTF-8 is exit 2, and EVERY
such file is named with its fix (pass its suffix, or re-save it as UTF-8) -- this gate cannot say
what characters it holds, so it has not looked. So is a tracked path with no bytes on disk (deleted,
sparse, or a name git could not hand over as UTF-8); a symlink or gitlink is the only skip. A text
suffix (`TEXT_SUFFIXES`) is refused as `--binary-suffix`, so a UTF-16 `.md` cannot be waved through.

Exit 0 clean, 1 findings, 2 could not look (git failed, no text file read, a file it cannot decode).

  ci-hidden-unicode [--root DIR] [--binary-suffix .EXT ...] [--selftest]
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Sequence

from ci_kit import discover, site_lint

GATE = "hidden-unicode"

#: (first, last, why) -- the classes this gate refuses.
RANGES: tuple[tuple[int, int, str], ...] = (
    (0x00, 0x08, "C0 control character"),
    (0x0B, 0x0C, "C0 control character"),
    (0x0E, 0x1F, "C0 control character"),
    (0x7F, 0x9F, "DEL / C1 control character"),
    (0x00AD, 0x00AD, "soft hyphen"),
    (0x034F, 0x034F, "combining grapheme joiner"),
    (0x061C, 0x061C, "Arabic letter mark (bidi control)"),
    (0x115F, 0x1160, "Hangul filler"),
    (0x17B4, 0x17B5, "Khmer inherent vowel (invisible)"),
    (0x180B, 0x180F, "Mongolian variation selector / vowel separator"),
    (0x200B, 0x200F, "zero-width / directional mark"),
    (0x202A, 0x202E, "bidi embedding or override"),
    (0x2060, 0x2064, "word joiner / invisible operator"),
    (0x2066, 0x2069, "bidi isolate"),
    (0x206A, 0x206F, "deprecated format control"),
    (0x2800, 0x2800, "Braille pattern blank"),
    (0x3164, 0x3164, "Hangul filler"),
    (0xFE00, 0xFE0F, "variation selector"),
    (0xFEFF, 0xFEFF, "BOM / zero-width no-break space after byte 0"),
    (0xFFA0, 0xFFA0, "Hangul filler"),
    (0xFFF9, 0xFFFB, "interlinear annotation control"),
    (0x1BCA0, 0x1BCA3, "shorthand format control"),
    (0x1D173, 0x1D17A, "musical format control"),
    (0xE0000, 0xE007F, "tag character"),
    (0xE0100, 0xE01EF, "variation selector supplement"),
)
_HIDDEN = re.compile("[" + "".join(f"{re.escape(chr(a))}-{re.escape(chr(b))}" for a, b, _ in RANGES) + "]")
_BOM = chr(0xFEFF)

#: Skipped unread. A suffix earns a place here by being a binary format; a text format never does.
BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".icns", ".webp", ".bmp", ".tif", ".tiff", ".avif",
    ".heic", ".pdf", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".7z", ".tar", ".jar", ".whl",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".parquet", ".feather", ".arrow", ".db", ".sqlite",
    ".pyc", ".so", ".dll", ".exe", ".dylib", ".snk", ".pfx", ".p12", ".mp3", ".mp4", ".wav",
    ".ogg", ".webm", ".mov", ".xlsx", ".docx", ".pptx", ".npy", ".npz", ".pkl", ".onnx", ".age",
    ".gpg",
})

#: Never accepted as `--binary-suffix`. Defence in depth only: UTF-8 is read whatever its suffix,
#: so this list guards the non-UTF-8 case (a UTF-16 `.md`) and words the exit-2 advice.
TEXT_SUFFIXES = frozenset({
    ".md", ".txt", ".rst", ".adoc", ".py", ".pyi", ".pyx", ".ipynb", ".json", ".jsonl", ".json5",
    ".jsonc", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".config", ".env", ".example",
    ".lock", ".sql", ".sh", ".bash", ".bats", ".zsh", ".ps1", ".psm1", ".psd1", ".bat", ".cmd",
    ".vbs", ".cs", ".csproj", ".fs", ".fsproj", ".vbproj", ".props", ".targets", ".sln", ".slnf",
    ".razor", ".cshtml", ".resx", ".nuspec", ".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx",
    ".jsx", ".vue", ".svelte", ".html", ".htm", ".css", ".scss", ".less", ".svg", ".xml", ".xsd",
    ".xslt", ".webmanifest", ".csv", ".tsv", ".j2", ".jinja", ".tftpl", ".tf", ".tfvars", ".hcl",
    ".go", ".rs", ".java", ".kt", ".swift", ".c", ".h", ".cpp", ".hpp", ".rb", ".lua", ".r",
    ".graphql", ".proto", ".bicep", ".nix", ".dockerfile", ".service", ".timer", ".socket",
    ".fixture", ".patch", ".diff", ".log",
})


def _control(cp: int) -> bool:
    """C0 / DEL / C1 -- ordinary bytes in a binary format, hidden characters in text."""
    return cp <= 0x1F or 0x7F <= cp <= 0x9F


def _why(cp: int) -> str:
    return next(why for a, b, why in RANGES if a <= cp <= b)


def findings_in(text: str) -> list[tuple[int, int, int]]:
    """[(line, column, code point)] of every hidden character, both 1-based. A FEFF at offset 0
    is the file's BOM and is skipped."""
    out: list[tuple[int, int, int]] = []
    # split("\n"), not splitlines(): the latter also breaks on U+2028 / U+001C and would shift
    # every reported line number away from what an editor shows.
    for ln, line in enumerate(text.split("\n"), start=1):
        for m in _HIDDEN.finditer(line):
            if ln == 1 and m.start() == 0 and m.group() == _BOM:
                continue
            out.append((ln, m.start() + 1, ord(m.group())))
    return out


def suffix_of(rel: str) -> str:
    return pathlib.PurePosixPath(rel).suffix.lower()


def escaped(text: str) -> str:
    """`text` with every hidden character written as its escape -- how a path is PRINTED."""
    return _HIDDEN.sub(lambda m: f"\\u{ord(m.group()):04x}" if ord(m.group()) < 0x10000
                       else f"\\U{ord(m.group()):08x}", text)


def blind_line(shown: str, rel: str, why: str) -> str:
    """One unreadable file, named with the fix that makes the gate able to read it -- or skip it."""
    suffix = suffix_of(rel)
    if suffix and suffix not in TEXT_SUFFIXES:
        skip = f"if it IS binary, pass `--binary-suffix {suffix}` (the action/workflow `args`)"
    elif suffix:
        skip = f"`{suffix}` is a text format, so it cannot be skipped as binary"
    else:
        skip = "it has no suffix, so if it IS binary give it one and pass `--binary-suffix .<ext>`"
    return f"{shown} {why}: {skip}; if it is text, re-save it as UTF-8"


@dataclasses.dataclass
class Scan:
    findings: list[str] = dataclasses.field(default_factory=list)
    blind: list[str] = dataclasses.field(default_factory=list)
    text: int = 0
    binary: int = 0
    links: int = 0


def _hit(shown: str, where: str, cp: int) -> str:
    name = unicodedata.name(chr(cp), f"<control {cp:#04x}>")
    return f"{shown}:{where}: U+{cp:04X} {name} ({_why(cp)})"


def _read_one(path: pathlib.Path, rel: str, shown: str, binary: frozenset[str], out: Scan) -> str | None:
    """The file's text, or None after counting it binary or naming it unreadable."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        out.blind.append(f"{shown} could not be read ({exc})")
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        if suffix_of(rel) in binary:
            out.binary += 1
        else:
            out.blind.append(blind_line(shown, rel, f"is not UTF-8 ({exc.reason} at byte {exc.start})"))
    return None


def scan(root: pathlib.Path, binary: frozenset[str]) -> Scan:
    out = Scan()
    for rel in discover.tracked((".",), suffix="", root=root):
        shown = escaped(rel)
        for m in _HIDDEN.finditer(rel):
            out.findings.append(_hit(shown, f"name:{m.start() + 1}", ord(m.group())) + " -- rename the file")
        path = root / rel
        if path.is_symlink() or path.is_dir():
            out.links += 1   # a symlink's target is read under its own path; a gitlink is a repo
            continue
        if not path.is_file():
            out.blind.append(f"{shown} is tracked but not on disk (deleted or sparse in this checkout, "
                             f"or a name git could not hand over as UTF-8)")
            continue
        text = _read_one(path, rel, shown, binary, out)
        if text is None:
            continue
        out.text += 1
        hits = findings_in(text)
        if suffix_of(rel) in binary:   # a declared binary that happens to decode (an empty zip):
            hits = [h for h in hits if not _control(h[2])]   # controls are its content, not text
        out.findings.extend(_hit(shown, f"{ln}:{col}", cp) for ln, col, cp in hits)
    return out


def _checked(root: pathlib.Path, binary: frozenset[str], seen: Scan) -> list[str]:
    """The scan for `site_lint.check`: findings, or exit 2 naming EVERY unreadable file (one run
    lists them all, so a repo adopting the gate fixes them in one pass, not one per CI run)."""
    got = scan(root, binary)
    seen.text, seen.binary, seen.links = got.text, got.binary, got.links
    if got.blind:
        for f in got.findings:
            print(f"HIDDEN {f}")
        print(f"{GATE}: DID NOT FINISH LOOKING -- {len(got.blind)} tracked file(s) it cannot read "
              f"as text, so what characters they hold is unknown:", file=sys.stderr)
        for b in got.blind:
            print(f"  {b}", file=sys.stderr)
        raise SystemExit(2)
    return got.findings


def _report(found: Sequence[str], seen: Scan) -> None:
    for f in found:
        print(f"HIDDEN {f}")
    if found:
        print(f"{GATE}: {len(found)} hidden character(s) in tracked files. Write the escape "
              f"(`\\uXXXX` in a Python/JSON/JS string) or drop it (an emoji needs no U+FE0F).")
    else:
        tail = f", {seen.links} symlink/gitlink skipped" if seen.links else ""
        print(f"{GATE}: OK -- {seen.text} tracked text file(s) clean ({seen.binary} binary{tail})")


def _gate(argv: Sequence[str], root: pathlib.Path | None, binary: frozenset[str]) -> int:
    seen = Scan()
    return site_lint.check(
        list(argv), gate=GATE, selftest=selftest, scanned=lambda: seen.text, scan_dirs=(".",),
        scan=lambda: _checked(root or discover.repo_root(), binary, seen),
        report=lambda found: _report(found, seen))


def _predicate_fails() -> list[str]:
    fails: list[str] = []
    bad = {
        "zero-width space in a regex": "r'" + chr(0x200B) + "?Unsubscribe'",
        "ZWJ": "a" + chr(0x200D) + "b",
        "right-to-left override (Trojan Source)": "x = 1  # " + chr(0x202E) + "evil",
        "Hangul filler identifier": "x" + chr(0x3164) + " = 1",
        "emoji VS16": chr(0x26A0) + chr(0xFE0F) + " blind",
        "BOM at line 2 col 1": "x\n" + _BOM + "y",
        "tag smuggling": "ok" + chr(0xE0041) + chr(0xE007F),
        "a heredoc-eaten backslash-1": "a consumed `" + chr(1) + "`",
        "soft hyphen": "pass" + chr(0xAD) + "word",
        "combining grapheme joiner": "a" + chr(0x034F) + "b",
        "Khmer inherent vowel": "a" + chr(0x17B4) + chr(0x17B5),
        "Mongolian vowel separator": "a" + chr(0x180E) + "b",
        "Braille blank": "name" + chr(0x2800) + "= 1",
    }
    fails += [f"MISSED {label}" for label, text in bad.items() if not findings_in(text)]
    # every range fires at both ends, and its outside neighbours stay quiet unless another range
    # owns them -- the regex is built from the table, so this proves the build, not the table
    inside = [(a, b) for a, b, _ in RANGES]
    for a, b, why in RANGES:
        for cp in (a, b):
            if findings_in("ab" + chr(cp) + "cd") != [(1, 3, cp)]:
                fails.append(f"MISSED U+{cp:04X} ({why})")
        for cp in (n for n in (a - 1, b + 1) if n >= 0):
            owned = any(x <= cp <= y for x, y in inside) or cp in (0x0A, 0xFEFF)
            if not owned and findings_in("ab" + chr(cp) + "cd"):
                fails.append(f"FALSE POSITIVE on U+{cp:04X}, the neighbour of {why}")
    good = {
        "leading BOM": _BOM + "x = 1\n",
        "the escape written out": "r'\\u200b?Unsubscribe'  # \\ufe0f \\ufeff \\u00ad",
        "a bare emoji": chr(0x26A0) + " Risk breaker blind",
        "ordinary non-ASCII": "caf" + chr(0xE9) + " " + chr(0x2014) + " " + chr(0x4E2D) + chr(0x2801),
        "nbsp, tab, CRLF, line separator": "a" + chr(0xA0) + "b\tc\r\n" + chr(0x2028),
    }
    fails += [f"FALSE POSITIVE on {label}: {findings_in(t)}" for label, t in good.items() if findings_in(t)]
    if findings_in("a" + chr(0x2028) + "b\nc" + chr(0x200B)) != [(2, 2, 0x200B)]:
        fails.append("line/column mis-reported (U+2028 must not count as a line break)")
    return fails


def _run_quiet(root: pathlib.Path, binary: frozenset[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            rc = _gate([], root, binary)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 2
    return rc, buf.getvalue()


def _repo_fails(tmp: pathlib.Path) -> list[str]:
    """End to end on throwaway git repos: 1 on a planted character, 0 on the escape, 2 on a file
    it cannot read -- and the 2 names the file and the flag that fixes it."""
    cases = {  # name: (files, binary suffixes, want rc, text the output must hold)
        "bad": ({"docs/a.md": "ok\nzero" + chr(0x200B) + "width\n"}, BINARY_SUFFIXES, 1, "docs/a.md:2:5: U+200B"),
        "good": ({"a.py": _BOM + "s = '\\u200b'\n", "logo.png": b"\x89PNG\x00\xff"}, BINARY_SUFFIXES, 0,
                 "1 tracked text file(s) clean (1 binary)"),
        "blind-nul": ({"a.md": "x\n", "data.blob": b"\xff\x00\x01"}, BINARY_SUFFIXES, 2, "--binary-suffix .blob"),
        "blind-fixed": ({"a.md": "x\n", "data.blob": b"\xff\x00\x01"}, BINARY_SUFFIXES | {".blob"}, 0, "(1 binary)"),
        "blind-latin1": ({"a.md": b"caf\xe9\n"}, BINARY_SUFFIXES, 2, "re-save it as UTF-8"),
        "nul-in-text": ({"k.ts": "k = a + '\x00' + b\n"}, BINARY_SUFFIXES, 1, "k.ts:1:10: U+0000"),
        "utf8-under-a-binary-suffix": ({"a.svg": "<t>" + chr(0x202E) + "</t>\n"}, BINARY_SUFFIXES | {".svg"}, 1,
                                       "a.svg:1:4: U+202E"),
        "controls-in-a-decodable-binary": ({"a.md": "x\n", "e.zip": b"PK\x05\x06" + b"\x00" * 18}, BINARY_SUFFIXES, 0,
                                           "2 tracked text file(s) clean"),
        "hidden-name": ({"a" + chr(0x202E) + "dm.py": "x = 1\n"}, BINARY_SUFFIXES, 1, "a\\u202edm.py:name:2: U+202E"),
        "nothing-read": ({"logo.png": b"\xff\x00"}, BINARY_SUFFIXES, 2, "read nothing"),
    }
    fails: list[str] = []
    for name, (files, binary, want, needle) in cases.items():
        root = tmp / name
        root.mkdir()
        for rel, body in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))
        subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
        rc, out = _run_quiet(root, binary)
        if rc != want or needle not in out:
            fails.append(f"{name}: exit {rc} (want {want}), output lacks {needle!r}: {out.strip()[:300]}")
    return fails


def selftest() -> list[str]:
    """Both directions: the predicate on text built from code points, then the CLI's three exits."""
    fails = _predicate_fails()
    with tempfile.TemporaryDirectory() as d:
        try:
            fails += _repo_fails(pathlib.Path(d))
        except (OSError, subprocess.CalledProcessError) as exc:
            fails.append(f"could not build the selftest repos: {type(exc).__name__}: {exc}")
    return fails


def _suffix(value: str) -> str:
    s = value.strip().lower()
    if not re.fullmatch(r"\.[a-z0-9_+-]+", s):
        raise argparse.ArgumentTypeError(f"{value!r} is not a suffix like .bin")
    if s in TEXT_SUFFIXES:
        raise argparse.ArgumentTypeError(f"{s} is a text format; skipping it would blind the gate")
    return s


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(prog="ci-hidden-unicode", description=__doc__.split("\n", 1)[0])
    ap.add_argument("--root", type=pathlib.Path, default=None)
    ap.add_argument("--binary-suffix", type=_suffix, action="append", default=[], metavar=".EXT",
                    help="one more suffix to skip unread as binary (repeatable; text formats refused)")
    ap.add_argument("--selftest", action="store_true", help="prove the gate fires and stays quiet")
    args = ap.parse_args(argv)
    env_root = os.environ.get("CI_KIT_ROOT")  # same order as the kit's other gates
    root = args.root or (pathlib.Path(env_root) if env_root else None)
    flags = ["--selftest"] if args.selftest else []
    return _gate(flags, root.resolve() if root else None, BINARY_SUFFIXES | frozenset(args.binary_suffix))


if __name__ == "__main__":
    sys.exit(main())
