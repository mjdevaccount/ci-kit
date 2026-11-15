"""THE INVARIANT: ci-declared-literal never exempts a line its predecessor flagged.

The oracle below is a small, independent port of the predecessor's predicate (generic: the
pattern is a parameter): every line is judged against six comment openers, and PowerShell lines
first lose their `<# #>` block comments, where a quote opened on the same line protects `<#`. A
seeded fuzz over the characters that move either model asserts, for PowerShell and for each file
kind, {lines the tool flags} >= {lines the oracle flags}. Stricter is allowed; looser is a bug."""
from __future__ import annotations

import os
import pathlib
import random
import re
import subprocess

import pytest

from ci_kit import declared_literal as dl

VALUE = "192.0." + "2.7"   # RFC 5737
RX = re.escape(VALUE)
ORACLE_OPENERS = ("#", "//", "<!--", ";", "*", "--")
_OPEN_OR_QUOTE = re.compile(r"<#|['\"]")


def _oracle_ps_code(line: str, in_block: bool) -> tuple[str, bool]:
    """The code of one line outside `<# #>`. Inside a block the RAW line is searched for `#>`
    (so `<#>` closes a block, as the predecessor's `find` does); outside one, the next `<#` or
    quote is located by regex and a quote is skipped to its same-line partner."""
    kept, pos = [], 0
    while pos < len(line):
        if in_block:
            close = line.find("#>", pos)
            if close == -1:
                return "".join(kept), True
            pos, in_block = close + 2, False
            continue
        m = _OPEN_OR_QUOTE.search(line, pos)
        if m is None:
            kept.append(line[pos:])
            break
        kept.append(line[pos:m.start()])
        if m.group() == "<#":
            pos, in_block = m.end(), True
            continue
        end = line.find(m.group(), m.end())
        stop = len(line) if end == -1 else end + 1
        kept.append(line[m.start():stop])
        pos = stop
    return "".join(kept), in_block


def oracle(text: str, rel: str) -> set[int]:
    ps = rel.lower().endswith((".ps1", ".psm1", ".psd1"))
    flagged, in_block = set(), False
    for n, line in enumerate(text.splitlines(), 1):
        code = line
        if ps:
            code, in_block = _oracle_ps_code(line, in_block)
        if re.search(RX, code) and not code.lstrip().startswith(ORACLE_OPENERS):
            flagged.add(n)
    return flagged


NL = chr(10)
_PIECES = ["<#", "#>", "<#>", "<", ">", "#", "'", '"', "`", '@"', '"@', "@'", "'@", " ", "  ", chr(9), "x", "=", "//",
           "--", ";", "*", "<!--", "-->", "{", "}", "/", VALUE, VALUE]
KINDS = ["a.ps1", "a.psm1", "a.sh", "a.c", "a.css", "a.sql", "a.ini", "a.md", "a.json", "a.tf",
         "Dockerfile", "a.unknown", "a.vue", "a.yml"]


def _doc(rng: random.Random) -> str:
    lines = ["".join(rng.choice(_PIECES) for _ in range(rng.randint(0, 7))) for _ in range(rng.randint(1, 8))]
    return NL.join(lines) + NL


@pytest.fixture(scope="module")
def rules() -> list[dl.Rule]:
    return dl.compile_rules([dl.Rule(label="v", patterns=[RX], examples=[f"h={VALUE}"])])


@pytest.mark.parametrize("rel", KINDS)
def test_never_looser_than_the_predecessor(rel: str, rules: list[dl.Rule]) -> None:
    rng = random.Random(f"declared-literal:{rel}")
    for _ in range(4000):
        text = _doc(rng)
        new = {ln for ln, _label, _line in dl.findings_in(text, rel, rules)}
        missed = oracle(text, rel) - new
        assert not missed, (rel, sorted(missed), text)


@pytest.mark.parametrize("text", [
    f"ping gw#{VALUE}" + NL, f"Start-Process http://h/#/{VALUE}" + NL, f"echo `#{VALUE}" + NL,
    '$m = "first' + NL + f'second # {VALUE}"' + NL, f"$f = 1  <# t #> # c {VALUE}" + NL,
])
def test_the_reviewed_shapes_are_findings(text: str, rules: list[dl.Rule]) -> None:
    got = {ln for ln, *_ in dl.findings_in(text, "a.ps1", rules)}
    assert oracle(text, "a.ps1") <= got and got, text


def test_every_family_is_a_subset_of_the_predecessors_openers() -> None:
    for key, family in dl.COMMENT_FAMILIES.items():
        assert set(family) <= set(ORACLE_OPENERS), key
    assert set(dl.FALLBACK_COMMENTS) <= set(ORACLE_OPENERS)
    assert dl.OLD_OPENERS == ORACLE_OPENERS


def test_the_oracle_is_not_vacuous() -> None:
    """The oracle must itself exempt and flag, or the subset test proves nothing."""
    assert oracle(f"# {VALUE}" + NL + f"x={VALUE}" + NL, "a.sh") == {2}
    assert oracle("<#" + NL + VALUE + NL + f"#> {VALUE}" + NL, "a.ps1") == {3}
    assert oracle(f"'<#' {VALUE}" + NL, "a.ps1") == {1}
    assert oracle(f"<# <#> {VALUE}" + NL, "a.ps1") == {1}


# ---------------------------------------------------------------- the FILE level: which files are read
SYMLINKS = os.name != "nt"   # a Windows checkout without the privilege writes links as plain files
DECLARED = "decl.env"
NAMES = ["a.md", "b.MD", "c.Md", "d.txt", "e.ps1", "f.PS1", "g.sh", "h.conf", "i.MD.txt", "j"]


def oracle_tree(root: pathlib.Path, rels: list[str], declared: set[str], exempt: set[str]) -> set[tuple[str, int]]:
    """The predecessor's file walk: a declared path or an exempt suffix (as written) is skipped,
    everything else is read THROUGH links; what cannot be read is skipped."""
    hits = set()
    for rel in rels:
        path = root / rel
        if rel in declared or path.suffix in exempt:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hits |= {(rel, n) for n in oracle(text, rel)}
    return hits


def _tree(root: pathlib.Path, rng: random.Random) -> None:
    root.mkdir()
    (root / DECLARED).write_bytes(f"GW={VALUE}".encode() + NL.encode())
    (root / "sub").mkdir()
    for name in rng.sample(NAMES, rng.randint(2, len(NAMES))):
        (root / name).write_bytes(_doc(rng).encode())
    if SYMLINKS:
        regular = [p.name for p in root.iterdir() if p.is_file()]
        for i in range(rng.randint(0, 4)):
            target = rng.choice([*regular, "missing.txt", "sub"])
            os.symlink(target, root / rng.choice([f"l{i}.conf", f"l{i}.md", f"l{i}.MD", f"l{i}.ps1"]))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)


@pytest.mark.parametrize("exempt", [set(), {".md"}, {".MD"}, {".md", ".MD"}],
                         ids=lambda e: ",".join(sorted(e)) or "none")
def test_never_reads_fewer_files_than_the_predecessor(tmp_path: pathlib.Path, exempt: set[str]) -> None:
    rule = dl.compile_rules([dl.Rule(label="v", patterns=[RX], examples=[f"h={VALUE}"], declared=[DECLARED])])
    rng = random.Random(f"declared-literal-tree:{sorted(exempt)}")
    for n in range(40):
        root = tmp_path / f"t{n}"
        _tree(root, rng)
        rels = dl.tracked_all(root)
        seen = dl.scan(root, rule, frozenset(exempt), dl.COMMENT_FAMILIES)
        new = {(rel, ln) for rel, ln, _label, _line in seen.findings}
        missed = oracle_tree(root, rels, {DECLARED}, exempt) - new
        assert not missed, (sorted(exempt), sorted(missed), rels)
