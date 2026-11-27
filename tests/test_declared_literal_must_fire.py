"""ci-declared-literal must fire: a literal outside its declared file exits 1 naming file:line, the
declared file / a comment line / an exempt suffix exit 0, and every way of not looking -- or of a
policy that cannot fire -- exits 2. Fixtures use RFC 5737 documentation ranges only, on throwaway
REAL git repos run the way a caller runs the tool."""
from __future__ import annotations

import codecs
import os
from pathlib import Path

import pytest
from conftest import NL, git, run_tool, write

from ci_kit import declared_literal as dl

NET = "192.0." + "2."          # TEST-NET-1
NET2 = "198.51." + "100."      # TEST-NET-2
RX = r"\b192\.0\.2\.\d{1,3}\b"
EX = f"GW={NET}1"
RULE = ["--pattern", RX, "--example", EX, "--declared", ".env.example"]


def _repo(new_repo, files: dict[str, str | bytes], name: str = "r") -> Path:
    r = new_repo(name)
    for rel, body in files.items():
        if isinstance(body, bytes):
            (r / rel).parent.mkdir(parents=True, exist_ok=True)
            (r / rel).write_bytes(body)
        else:
            write(r / rel, body)
    git(r, "add", "-A")
    git(r, "commit", "-qm", "c")
    return r


def _run(r: Path, *args: str):
    return run_tool("declared_literal", r, *args)


def _hits(stdout: str, label: str = "literal") -> set[str]:
    return {ln.split(": [", 1)[0].strip() for ln in stdout.splitlines() if f": [{label}]" in ln}


def test_literal_outside_declared_exits_1_naming_file_line(new_repo) -> None:
    r = _repo(new_repo, {"compose.yml": "x: 1" + NL + f"targets: ['{NET}10:9100']" + NL})
    res = _run(r, *RULE)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "compose.yml:2: [literal]" in res.stdout
    assert "1 tracked file(s) read" in res.stdout
    assert "1 rule(s) loaded: literal" in res.stdout


def test_declared_comment_and_clean_exit_0(new_repo) -> None:
    r = _repo(new_repo, {".env.example": f"GW={NET}1" + NL,
                         "a.sh": f"# history: {NET}1 retired" + NL + "GW=$GW" + NL,
                         "b.js": f"// was {NET}5" + NL, "c.sql": f"-- was {NET}5" + NL,
                         "d.ini": f"; was {NET}5" + NL, "e.html": f"<!-- was {NET}5 -->" + NL,
                         "f.c": f" * was {NET}5" + NL})
    res = _run(r, *RULE)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "6 tracked file(s) read" in res.stdout and "1 declared-only" in res.stdout
    assert "1/1 declared file(s) present" in res.stdout


def test_declared_is_per_rule_and_exact_path(new_repo) -> None:
    """A nested file with the declared NAME is not the declared file."""
    r = _repo(new_repo, {".env.example": f"GW={NET}1" + NL, "sub/.env.example": f"GW={NET}1" + NL})
    res = _run(r, *RULE)
    assert res.returncode == 1 and "sub/.env.example:1" in res.stdout, res.stdout


def test_md_exempt_only_when_asked(new_repo) -> None:
    r = _repo(new_repo, {"README.md": f"the site is http://{NET}5" + NL, "a.sh": "x=1" + NL})
    assert _run(r, *RULE).returncode == 1
    res = _run(r, *RULE, "--exempt-suffix", ".md")
    assert res.returncode == 0, res.stdout
    assert "1 tracked file(s) read (1 exempt by suffix" in res.stdout


def test_powershell_comments_stripped(new_repo) -> None:
    r = _repo(new_repo, {
        "d.ps1": "<#" + NL + f"  held {NET}5" + NL + "#>" + NL + f"<# one {NET}6 #>" + NL
                 + "param()" + NL + f"$gw = '{NET}1'" + NL,
        "g.ps1": f"$a = '{NET}1'  <# t #>" + NL + "<# o" + NL + f"#> $b = '{NET}1'" + NL
                 + 'Write-Host "use <# to open"' + NL + f"$c = '{NET}1'" + NL,
        "e.ps1": "<#" + NL + f"$gw = '{NET}1'" + NL,
        "f.sh": "N=1 <#" + NL + f"GW='{NET}1'" + NL,
        "h.ps1": "# see <# in docs" + NL + f"$d = '{NET}1'" + NL + '$s = @"' + NL + "use <# here" + NL
                 + '"@' + NL + f"$e = '{NET}1'" + NL + f"$f = 1  # was {NET}2" + NL,
        "q.ps1": 'Write-Host "a `" <# b"' + NL + f"$g = '{NET}1'" + NL,
    })
    res = _run(r, *RULE)
    assert res.returncode == 1
    assert _hits(res.stdout) == {"d.ps1:6", "g.ps1:1", "g.ps1:3", "g.ps1:5", "f.sh:2",
                                 "h.ps1:2", "h.ps1:6", "q.ps1:2"}, res.stdout
    assert "6 tracked file(s) read" in res.stdout   # the unclosed e.ps1 was still READ


def test_utf16_powershell_is_decoded_by_bom(new_repo) -> None:
    body = f"<# held {NET}5 #>{NL}$gw = '{NET}1'{NL}"
    r = _repo(new_repo, {"u.ps1": codecs.BOM_UTF16_LE + body.encode("utf-16-le")})
    res = _run(r, *RULE)
    assert res.returncode == 1 and _hits(res.stdout) == {"u.ps1:2"}, res.stdout


@pytest.mark.parametrize("rel", ["u.ps1", ".env", "Caddyfile", "Dockerfile", "x.tmpl", "cjk.sh"])
@pytest.mark.parametrize("enc", ["utf-16-le", "utf-16-be"])
def test_utf16_without_a_bom_is_read_whatever_the_name(new_repo, rel: str, enc: str) -> None:
    body = "# 设置网关" + NL + f"GW={NET}1" + NL
    r = _repo(new_repo, {rel: body.encode(enc), "a.sh": "x=1" + NL})
    res = _run(r, *RULE)
    assert res.returncode == 1 and _hits(res.stdout) == {f"{rel}:2"}, res.stdout + res.stderr


def test_a_nul_separator_in_utf8_text_is_read(new_repo) -> None:
    r = _repo(new_repo, {"k.ts": "const key = `${a}" + chr(0) + "${b}`" + NL + f"const h = '{NET}1'" + NL})
    res = _run(r, *RULE)
    assert res.returncode == 1 and _hits(res.stdout) == {"k.ts:2"}, res.stdout + res.stderr


def test_a_binary_is_still_read(new_repo) -> None:
    r = _repo(new_repo, {"logo.png": b"\x89PNG\x00\xff" + f"{NET}1".encode() + b"\x00"})
    res = _run(r, *RULE)
    assert res.returncode == 1 and "logo.png:1" in res.stdout, res.stdout + res.stderr


@pytest.mark.parametrize("body", [f"# note\rGW={NET}1\r" + NL, f"# note\fGW={NET}1" + NL,
                                  f"# note\u2028GW={NET}1" + NL, f"# note\x85GW={NET}1" + NL])
def test_a_comment_opener_shelters_nothing_after_a_non_lf_break(new_repo, body: str) -> None:
    r = _repo(new_repo, {"a.sh": "x=1" + NL + body})
    res = _run(r, *RULE)
    assert res.returncode == 1 and _hits(res.stdout) == {"a.sh:2"}, res.stdout


def test_js_comment_then_line_separator(new_repo) -> None:
    r = _repo(new_repo, {"k.js": f"// note\u2028const h = '{NET}1'" + NL})
    assert _hits(_run(r, *RULE).stdout) == {"k.js:1"}


@pytest.mark.parametrize(("rel", "body"), [
    ("run.sh", "docker run \\" + NL + f"  --add-host=gw:{NET}1 \\" + NL + "  img" + NL),
    ("jobs.cron", f"*/5 * * * * curl http://{NET}1/ping" + NL),
    ("crontab", f"*/5 * * * * curl http://{NET}1/ping" + NL),
    ("host.c", f'#define HOST "{NET}1"' + NL),
    ("site.css", f"#hub {{ background: url(http://{NET}1/x.png) }}" + NL),
])
def test_an_opener_is_a_comment_only_where_the_file_kind_says(new_repo, rel: str, body: str) -> None:
    r = _repo(new_repo, {rel: body})
    res = _run(r, *RULE)
    assert res.returncode == 1, (rel, res.stdout)


def test_comment_prefix_flag_adds_an_opener(new_repo) -> None:
    r = _repo(new_repo, {"t.j2": "{# was " + NET + "1 #}" + NL})
    assert _run(r, *RULE).returncode == 1
    assert _run(r, *RULE, "--comment-prefix", ".j2={#").returncode == 0
    assert _run(r, *RULE, "--comment-prefix", "nonsense").returncode == 2


@pytest.mark.parametrize("args", [
    [], ["--pattern", ""], ["--rule", "x"], ["--pattern", "(unclosed", "--example", "x"],
    ["--pattern", "a*", "--example", "a"],
    ["--rule", "x", "--pattern", "a", "--example", "a", "--rule", "x", "--pattern", "b", "--example", "b"],
    ["--pattern", RX],                                                  # no --example
    [f"--pattern={RX} ", f"--example={EX} "],                           # trailing space
    [f"--pattern= {RX}", f"--example={EX}"],                            # leading space
    ["--pattern", RX.replace("\\", "\\\\"), "--example", EX],           # double-escaped
    ["--pattern", r"a\bb", "--example", "ab"],                          # \b between word chars
    ["--pattern", RX, "--example", f"# {EX}"],                          # the example is a comment
    ["--pattern", RX, "--pattern", "nevermatches", "--example", EX],    # one pattern unproven
    ["--patt", RX, "--example", EX],                                    # no abbreviations
])
def test_bad_policy_exits_2_never_0(new_repo, args: list[str]) -> None:
    r = _repo(new_repo, {"a.sh": "GW=1" + NL})
    res = _run(r, *args)
    assert res.returncode == 2, (args, res.stdout + res.stderr)


def test_a_present_declared_file_holding_no_match_exits_2(new_repo) -> None:
    r = _repo(new_repo, {".env.example": "GW=changeme" + NL, "a.sh": "x=1" + NL})
    res = _run(r, *RULE)
    assert res.returncode == 2 and ".env.example is present but holds no match" in res.stderr, res.stderr


def test_an_absent_declared_file_is_fine(new_repo) -> None:
    r = _repo(new_repo, {"a.sh": "x=1" + NL})
    res = _run(r, *RULE)
    assert res.returncode == 0 and "0/1 declared file(s) present" in res.stdout, res.stdout


def test_only_exempt_and_declared_files_is_did_not_look(new_repo) -> None:
    files: dict[str, str | bytes] = {f"d{i}.md": "x" + NL for i in range(12)}
    files[".env.example"] = f"GW={NET}1" + NL
    res = _run(_repo(new_repo, files), *RULE, "--exempt-suffix", ".md")
    assert res.returncode == 2, res.stdout + res.stderr


def test_tracked_pycache_is_read(new_repo) -> None:
    r = _repo(new_repo, {"pkg/__pycache__/notes.txt": f"GW={NET}1" + NL})
    res = _run(r, *RULE)
    assert res.returncode == 1 and "pkg/__pycache__/notes.txt:1" in res.stdout, res.stdout + res.stderr


def test_zero_files_exits_2(new_repo) -> None:
    res = _run(new_repo(), *RULE)
    assert res.returncode == 2, res.stdout + res.stderr


def test_outside_any_repo_exits_2(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    write(plain / "a.sh", f"GW={NET}1" + NL)
    res = _run(plain, *RULE)
    assert res.returncode == 2, res.stdout + res.stderr


def test_tracked_but_missing_exits_2(new_repo) -> None:
    r = _repo(new_repo, {"a.sh": "x" + NL, "b.sh": "y" + NL})
    (r / "b.sh").unlink()
    res = _run(r, *RULE)
    assert res.returncode == 2 and "b.sh" in res.stderr, res.stdout + res.stderr


def test_several_rules_each_with_its_own_declared_set(new_repo) -> None:
    r = _repo(new_repo, {"nets.env": f"A={NET}1" + NL + f"B={NET2}1" + NL,
                         "hosts.env": f"B={NET2}1" + NL, "app.yml": f"a: {NET}1" + NL + f"b: {NET2}1" + NL})
    res = _run(r, "--rule", "one", "--pattern", RX, "--example", EX, "--declared", "nets.env",
               "--rule", "two", "--pattern", r"\b198\.51\.100\.\d+", "--example", f"h={NET2}1",
               "--declared", "hosts.env", "--message", "use the hosts file")
    assert res.returncode == 1
    lines = res.stdout
    assert "app.yml:1: [one]" in lines and "app.yml:2: [two]" in lines
    assert "nets.env:2: [two]" in lines          # rule two's declared set does not hold nets.env
    assert "hosts.env" not in lines.split("literal(s) outside")[0]
    assert "2 rule(s) loaded: one, two" in lines and "use the hosts file" in lines


def test_bracketed_path_keeps_its_rule_message(new_repo) -> None:
    """A route file named `[id].tsx` must not be mistaken for a rule label in the report."""
    r = _repo(new_repo, {"app/[id].tsx": f"const h = '{NET}9'" + NL})
    res = _run(r, "--rule", "net", "--pattern", RX, "--example", EX, "--message", "use the env var")
    assert res.returncode == 1 and "app/[id].tsx:1: [net]" in res.stdout
    assert "[net] declared in no file. use the env var" in res.stdout, res.stdout


def _policy(new_repo, body: bytes) -> Path:
    p = new_repo("policy") / "lit.args"
    p.write_bytes(body)
    return p


POLICY = ("# the policy, with notes" + NL + NL + "--rule" + NL + "spaced" + NL
          + r"--pattern=host = \b192\.0\.2\.\d+\b" + NL + f"--example=host = {NET}7" + NL
          + "--message" + NL + "a message with spaces" + NL)


def test_args_file_round_trips_a_regex_with_spaces_and_backslashes(new_repo) -> None:
    r = _repo(new_repo, {"a.txt": f"host = {NET}7" + NL, "b.txt": f"host={NET}7" + NL})
    res = _run(r, f"@{_policy(new_repo, POLICY.encode())}")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "a.txt:1: [spaced]" in res.stdout and "b.txt" not in res.stdout.split("literal(s)")[0]
    assert "a message with spaces" in res.stdout


@pytest.mark.parametrize("enc", ["crlf", "utf-8-bom"])
def test_args_file_crlf_and_bom_parse(new_repo, enc: str) -> None:
    r = _repo(new_repo, {"a.txt": f"host = {NET}7" + NL})
    body = POLICY.replace(NL, "\r\n").encode() if enc == "crlf" else codecs.BOM_UTF8 + POLICY.encode()
    res = _run(r, f"@{_policy(new_repo, body)}")
    assert res.returncode == 1 and "a.txt:1: [spaced]" in res.stdout, res.stdout + res.stderr


@pytest.mark.parametrize("body", [codecs.BOM_UTF16_LE + POLICY.encode("utf-16-le"),
                                  POLICY.encode("utf-16-le"), b"--pattern\n\xff\xfe\xfa\n"])
def test_args_file_not_utf8_exits_2_naming_it(new_repo, body: bytes) -> None:
    r = _repo(new_repo, {"a.txt": "x" + NL})
    res = _run(r, f"@{_policy(new_repo, body)}")
    assert res.returncode == 2, res.stdout + res.stderr
    assert "CANNOT CHECK" in res.stderr and ("UTF" in res.stderr or "NUL" in res.stderr), res.stderr


def test_args_file_utf16_names_the_encoding_and_its_cause(new_repo) -> None:
    r = _repo(new_repo, {"a.txt": "x" + NL})
    res = _run(r, f"@{_policy(new_repo, codecs.BOM_UTF16_LE + POLICY.encode('utf-16-le'))}")
    assert res.returncode == 2 and "is UTF-16-LE" in res.stderr and "Out-File" in res.stderr, res.stderr


def test_a_rule_without_an_example_says_so(new_repo) -> None:
    res = _run(_repo(new_repo, {"a.sh": "x=1" + NL}), "--pattern", RX)
    assert res.returncode == 2 and "has no --example" in res.stderr, res.stderr


def test_missing_args_file_exits_2(new_repo) -> None:
    res = _run(_repo(new_repo, {"a.txt": "x" + NL}), "@nope/missing.args")
    assert res.returncode == 2 and "CANNOT CHECK" in res.stderr


def test_selftest_mixed_with_a_policy_exits_2(new_repo) -> None:
    r = _repo(new_repo, {"a.txt": "x" + NL})
    assert _run(r, "--selftest", *RULE).returncode == 2
    assert _run(r, "--selftest", "--exempt-suffix", ".md").returncode == 2
    in_file = _policy(new_repo, ("--selftest" + NL).encode())
    res = _run(r, f"@{in_file}")
    assert res.returncode == 2 and "holds --selftest" in res.stderr, res.stderr


def test_ci_kit_root_env_is_honoured(new_repo, monkeypatch) -> None:
    r = _repo(new_repo, {"a.sh": f"GW={NET}1" + NL})
    monkeypatch.setenv("CI_KIT_ROOT", str(r))
    res = _run(r.parent, *RULE)   # run from OUTSIDE the checkout: only the env var can point at it
    assert res.returncode == 1 and "a.sh:1" in res.stdout, res.stdout + res.stderr


def test_predicate_units() -> None:
    rules = dl.compile_rules([dl.Rule(label="n", patterns=[RX], examples=[EX], declared=["d.env"])])
    assert dl.findings_in(f"x={NET}1\n", "a.yml", rules) == [(1, "n", f"x={NET}1")]
    assert dl.findings_in(f"x={NET}1\n", "d.env", rules) == []
    assert dl.findings_in(f"  # x={NET}1\n", "a.yml", rules) == []
    floor = dl._PsFloor()
    assert floor.live("a <# b #> c <# d") == "a  c " and floor.block
    strict = dl._PsStrict()
    assert strict.live("'<#' x") == "'<#' x" and not strict.block
    assert strict.live("x # <# y") == "x " and not strict.block
    assert strict.live("gw#y") == "gw#y"


def test_selftest_passes(tmp_path: Path) -> None:
    res = run_tool("declared_literal", tmp_path, "--selftest")
    assert res.returncode == 0, res.stdout + res.stderr


# ---------------------------------------------------------------- never looser, never a silent 0
def test_powershell_hash_shapes_that_are_code_stay_live(new_repo) -> None:
    """`#` mid-word, in a URL, backtick-escaped, or inside a string carried across lines is code."""
    r = _repo(new_repo, {"r.ps1": f"ping gw#{NET}1" + NL + f"Start-Process http://h/#/{NET}1" + NL
                         + f"echo `#{NET}1" + NL + '$m = "first' + NL + f'second # {NET}1"' + NL})
    res = _run(r, *RULE)
    assert _hits(res.stdout) == {"r.ps1:1", "r.ps1:2", "r.ps1:3", "r.ps1:5"}, res.stdout


def test_here_string_terminator_closes_only_at_column_0(new_repo) -> None:
    r = _repo(new_repo, {"s.ps1": '$s = @"' + NL + '  "@"' + NL + "<# still text" + NL
                         + f"$v = '{NET}1'" + NL + '"@' + NL})
    res = _run(r, *RULE)
    assert _hits(res.stdout) == {"s.ps1:4"}, res.stdout


@pytest.mark.parametrize("args", [["-h"], ["--help"], ["--version"], ["--help", *RULE], [*RULE, "-h"],
                                  ["--selftest", "--help"], ["--selftest", "-h"]])
def test_no_flag_short_circuits_to_0(new_repo, args: list[str]) -> None:
    r = _repo(new_repo, {"a.sh": "x=1" + NL})
    res = _run(r, *args)
    assert res.returncode == 2 and "CANNOT CHECK" in res.stderr, (args, res.stdout + res.stderr)


def test_help_inside_an_args_file_exits_2(new_repo) -> None:
    r = _repo(new_repo, {"a.sh": "x=1" + NL})
    res = _run(r, f"@{_policy(new_repo, ('--help' + NL).encode())}")
    assert res.returncode == 2, res.stdout + res.stderr


def test_receipt_written_only_by_a_scan(new_repo, monkeypatch) -> None:
    r = _repo(new_repo, {"a.sh": f"GW={NET}1" + NL, "b.sh": "x=1" + NL})
    receipt = r.parent / "receipt.txt"
    monkeypatch.setenv(dl.RECEIPT_ENV, str(receipt))
    assert _run(r, *RULE).returncode == 1
    assert receipt.read_text(encoding="utf-8") == "read=2 rules=1 findings=1" + NL
    receipt.unlink()
    for args in (["--selftest"], ["--help"], ["--pattern", "(bad", "--example", "x"]):
        _run(r, *args)
        assert not receipt.exists(), args


def test_overflowing_pattern_is_cannot_check(new_repo) -> None:
    res = _run(_repo(new_repo, {"a.sh": "x=1" + NL}), "--pattern=a{99999999999999999999}", "--example=a")
    assert res.returncode == 2 and "CANNOT CHECK" in res.stderr and "Traceback" not in res.stderr, res.stderr


def test_unexpected_error_is_cannot_check(new_repo, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("kaboom")
    root = _repo(new_repo, {"a.sh": "x=1" + NL})
    monkeypatch.setattr(dl, "scan", boom)
    assert dl.main([*RULE, "--root", str(root)]) == 2


def test_exempt_suffix_file_declared_by_one_rule_stays_exempt_for_the_others(new_repo) -> None:
    r = _repo(new_repo, {"values.md": f"A={NET}1" + NL + f"B={NET2}1" + NL, "a.sh": "x=1" + NL})
    res = _run(r, "--exempt-suffix", ".md", "--rule", "one", "--pattern", RX, "--example", EX,
               "--declared", "values.md", "--rule", "two", "--pattern", r"\b198\.51\.100\.\d+",
               "--example", f"h={NET2}1")
    assert res.returncode == 0, res.stdout + res.stderr
    res = _run(r, "--exempt-suffix", ".md", "--pattern", r"\b203\.0\.113\.\d+", "--example", "h=203.0.113.1",
               "--declared", "values.md")
    assert res.returncode == 2 and "values.md is present but holds no match" in res.stderr, res.stderr


def test_comment_prefix_cannot_touch_powershell(new_repo) -> None:
    r = _repo(new_repo, {"x.ps1": f"REM {NET}1" + NL, "x.bat": f"REM {NET}1" + NL})
    assert _run(r, *RULE, "--comment-prefix", ".ps1=REM").returncode == 2
    res = _run(r, *RULE, "--comment-prefix", "*=REM")
    assert res.returncode == 1 and _hits(res.stdout) == {"x.ps1:1"}, res.stdout


@pytest.mark.parametrize("gap", ["# a note", "", "   "])
def test_a_note_after_a_value_flag_exits_2(new_repo, gap: str) -> None:
    r = _repo(new_repo, {"a.sh": f"GW={NET}1" + NL})
    body = "--pattern" + NL + gap + NL + RX + NL + f"--example={EX}" + NL
    res = _run(r, f"@{_policy(new_repo, body.encode())}")
    assert res.returncode == 2 and "right after --pattern" in res.stderr, res.stderr


def test_a_hash_value_travels_in_the_equals_form(new_repo) -> None:
    r = _repo(new_repo, {"a.sh": f"GW={NET}1" + NL})
    body = f"--pattern={RX}" + NL + f"--example={EX}" + NL + "--message=#1 use the env file" + NL
    res = _run(r, f"@{_policy(new_repo, body.encode())}")
    assert res.returncode == 1 and "#1 use the env file" in res.stdout, res.stdout + res.stderr


# ---------------------------------------------------------------- the file level: never fewer files read
@pytest.mark.skipif(os.name == "nt", reason="a tracked symlink needs a POSIX checkout")
def test_a_symlink_is_read_through_under_its_own_path(new_repo) -> None:
    r = new_repo()
    write(r / ".env.example", f"GW={NET}1" + NL)
    write(r / "notes.md", f"GW={NET}1" + NL)
    os.symlink(".env.example", r / "link.conf")    # the declared file, seen through a link
    os.symlink("notes.md", r / "notes.txt")        # an exempt suffix, seen through a link
    os.symlink("missing.txt", r / "dangling.txt")
    os.symlink(".", r / "here")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "c")
    res = _run(r, *RULE, "--exempt-suffix", ".md")
    assert res.returncode == 1 and _hits(res.stdout) == {"link.conf:1", "notes.txt:1"}, res.stdout + res.stderr
    assert "2 directory/gitlink/dangling link skipped" in res.stdout, res.stdout


def test_exempt_suffix_matches_as_written(new_repo) -> None:
    r = _repo(new_repo, {"NOTES.MD": f"GW={NET}1" + NL, "a.md": f"GW={NET}1" + NL})
    res = _run(r, *RULE, "--exempt-suffix", ".md")
    assert res.returncode == 1 and _hits(res.stdout) == {"NOTES.MD:1"}, res.stdout
    res = _run(r, *RULE, "--exempt-suffix", ".MD")
    assert res.returncode == 1 and _hits(res.stdout) == {"a.md:1"}, res.stdout


@pytest.mark.parametrize("key", [".ps1", ".PSM1", "r2.ps1", "deploy.PS1", "x.psd1"])
def test_comment_prefix_refuses_any_powershell_key(new_repo, key: str) -> None:
    res = _run(_repo(new_repo, {"a.sh": "x=1" + NL}), *RULE, "--comment-prefix", f"{key}=ping")
    assert res.returncode == 2 and "PowerShell" in res.stderr, res.stderr


@pytest.mark.parametrize("code", [0, 1, 3, None, "text"])
def test_a_helpers_system_exit_is_2(new_repo, monkeypatch, code) -> None:
    def leave(*_a, **_k):
        raise SystemExit(code)
    root = _repo(new_repo, {"a.sh": "x=1" + NL})
    monkeypatch.setattr(dl, "scan", leave)
    assert dl.main([*RULE, "--root", str(root)]) == 2
