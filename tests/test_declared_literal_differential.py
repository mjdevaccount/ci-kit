"""THE INVARIANT: ci-declared-literal never exempts a line its predecessor flagged.

The oracle below is a small, independent port of the predecessor's predicate (generic: the
pattern is a parameter): every line is judged against six comment openers, and PowerShell lines
first lose their `<# #>` block comments, where a quote opened on the same line protects `<#`. A
seeded fuzz over the characters that move either model asserts, for PowerShell and for each file
kind, {lines the tool flags} >= {lines the oracle flags}. Stricter is allowed; looser is a bug."""
from __future__ import annotations

import random
import re

import pytest

from ci_kit import declared_literal as dl

VALUE = "192.0." + "2.7"   # RFC 5737
RX = re.escape(VALUE)
ORACLE_OPENERS = ("#", "//", "<!--", ";", "*", "--")
_TOKEN = re.compile(r"<#|#>|.", re.S)


def _oracle_ps_code(line: str, in_block: bool) -> tuple[str, bool]:
    """The code of one line outside `<# #>`, tokenised (so not the tool's scanner shape)."""
    kept: list[str] = []
    quote = None
    for tok in _TOKEN.findall(line):
        if in_block:
            in_block = tok != "#>"
        elif quote is not None:
            kept.append(tok)
            quote = None if tok == quote else quote
        elif tok in ("'", '"'):
            quote = tok
            kept.append(tok)
        elif tok == "<#":
            in_block = True
        else:
            kept.append(tok)
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
_PIECES = ["<#", "#>", "#", "'", '"', "`", '@"', '"@', "@'", "'@", " ", "  ", chr(9), "x", "=", "//",
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
