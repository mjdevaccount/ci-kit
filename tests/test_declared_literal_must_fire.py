"""ci-declared-literal must fire: a literal outside its declared file exits 1 naming file:line, the
declared file / a comment line / an exempt suffix exit 0, and every way of not looking exits 2.
Fixtures use RFC 5737 documentation ranges only, on throwaway REAL git repos run the way a caller
runs the tool."""
from __future__ import annotations

from pathlib import Path

import pytest
from conftest import NL, git, run_tool, write

from ci_kit import declared_literal as dl

NET = "192.0." + "2."          # TEST-NET-1
NET2 = "198.51." + "100."      # TEST-NET-2
RX = r"\b192\.0\.2\.\d{1,3}\b"
RULE = ["--pattern", RX, "--declared", ".env.example"]


def _repo(new_repo, files: dict[str, str]) -> Path:
    r = new_repo()
    for rel, body in files.items():
        write(r / rel, body)
    git(r, "add", "-A")
    git(r, "commit", "-qm", "c")
    return r


def _run(r: Path, *args: str):
    return run_tool("declared_literal", r, *args)


def test_literal_outside_declared_exits_1_naming_file_line(new_repo) -> None:
    r = _repo(new_repo, {"compose.yml": "x: 1" + NL + f"targets: ['{NET}61:9835']" + NL})
    res = _run(r, *RULE)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "compose.yml:2: [literal]" in res.stdout
    assert "1 tracked file(s) scanned" in res.stdout
    assert "1 rule(s) loaded: literal" in res.stdout


def test_declared_comment_and_clean_exit_0(new_repo) -> None:
    r = _repo(new_repo, {".env.example": f"GW={NET}1" + NL,
                         "a.sh": f"# history: {NET}1 retired" + NL + "GW=$GW" + NL,
                         "b.js": f"// was {NET}5" + NL, "c.sql": f"-- was {NET}5" + NL,
                         "d.ini": f"; was {NET}5" + NL, "e.html": f"<!-- was {NET}5 -->" + NL,
                         "f.c": f" * was {NET}5" + NL})
    res = _run(r, *RULE)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "7 tracked file(s) scanned" in res.stdout
    assert "1/1 declared file(s) present" in res.stdout


def test_declared_is_per_rule_and_exact_path(new_repo) -> None:
    """A nested file with the declared NAME is not the declared file."""
    r = _repo(new_repo, {".env.example": f"GW={NET}1" + NL, "sub/.env.example": f"GW={NET}1" + NL})
    res = _run(r, *RULE)
    assert res.returncode == 1 and "sub/.env.example:1" in res.stdout, res.stdout


def test_md_exempt_only_when_asked(new_repo) -> None:
    r = _repo(new_repo, {"README.md": f"the hub is http://{NET}5:8088" + NL})
    assert _run(r, *RULE).returncode == 1
    res = _run(r, *RULE, "--exempt-suffix", ".md")
    assert res.returncode == 0, res.stdout
    assert "1 by exempt suffix" in res.stdout


def test_powershell_block_comments_stripped(new_repo) -> None:
    r = _repo(new_repo, {
        "d.ps1": "<#" + NL + f"  held {NET}5" + NL + "#>" + NL + f"<# one {NET}6 #>" + NL
                 + "param()" + NL + f"$gw = '{NET}1'" + NL,
        "g.ps1": f"$a = '{NET}1'  <# t #>" + NL + "<# o" + NL + f"#> $b = '{NET}1'" + NL
                 + 'Write-Host "use <# to open"' + NL + f"$c = '{NET}1'" + NL,
        "e.ps1": "<#" + NL + f"$gw = '{NET}1'" + NL,
        "f.sh": "N=1 <#" + NL + f"GW='{NET}1'" + NL,
    })
    res = _run(r, *RULE)
    assert res.returncode == 1
    got = {ln.split(": [", 1)[0].strip() for ln in res.stdout.splitlines() if ": [literal]" in ln}
    assert got == {"d.ps1:6", "g.ps1:1", "g.ps1:3", "g.ps1:5", "f.sh:2"}, res.stdout
    assert "4 tracked file(s) scanned" in res.stdout   # the unclosed e.ps1 was still READ


@pytest.mark.parametrize("args", [[], ["--pattern", ""], ["--rule", "x"], ["--pattern", "(unclosed"],
                                  ["--pattern", "a*"], ["--rule", "x", "--pattern", "a", "--rule", "x",
                                                        "--pattern", "b"]])
def test_bad_policy_exits_2_never_0(new_repo, args: list[str]) -> None:
    r = _repo(new_repo, {"a.sh": "GW=1" + NL})
    res = _run(r, *args)
    assert res.returncode == 2, res.stdout + res.stderr


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
    res = _run(r, "--rule", "one", "--pattern", RX, "--declared", "nets.env",
               "--rule", "two", "--pattern", r"\b198\.51\.100\.\d+", "--declared", "hosts.env",
               "--message", "use the hosts file")
    assert res.returncode == 1
    lines = res.stdout
    assert "app.yml:1: [one]" in lines and "app.yml:2: [two]" in lines
    assert "nets.env:2: [two]" in lines          # rule two's declared set does not hold nets.env
    assert "hosts.env" not in lines.split("literal(s) outside")[0]
    assert "2 rule(s) loaded: one, two" in lines and "use the hosts file" in lines


def test_bracketed_path_keeps_its_rule_message(new_repo) -> None:
    """A route file named `[id].tsx` must not be mistaken for a rule label in the report."""
    r = _repo(new_repo, {"app/[id].tsx": f"const h = '{NET}9'" + NL})
    res = _run(r, "--rule", "net", "--pattern", RX, "--message", "use the env var")
    assert res.returncode == 1 and "app/[id].tsx:1: [net]" in res.stdout
    assert "[net] declared in no file. use the env var" in res.stdout, res.stdout


def test_args_file_round_trips_a_regex_with_spaces_and_backslashes(new_repo) -> None:
    r = _repo(new_repo, {"a.txt": f"host = {NET}7" + NL, "b.txt": f"host={NET}7" + NL})
    policy = new_repo("policy") / "lit.args"
    write(policy, "# the policy, with notes" + NL + NL + "--rule" + NL + "spaced" + NL
          + r"--pattern=host = \b192\.0\.2\.\d+\b" + NL + "--message" + NL + "a message with spaces" + NL)
    res = _run(r, f"@{policy}")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "a.txt:1: [spaced]" in res.stdout and "b.txt" not in res.stdout.split("literal(s)")[0]
    assert "a message with spaces" in res.stdout


def test_ci_kit_root_env_is_honoured(new_repo, monkeypatch) -> None:
    r = _repo(new_repo, {"a.sh": f"GW={NET}1" + NL})
    monkeypatch.setenv("CI_KIT_ROOT", str(r))
    res = _run(r.parent, *RULE)   # run from OUTSIDE the checkout: only the env var can point at it
    assert res.returncode == 1 and "a.sh:1" in res.stdout, res.stdout + res.stderr


def test_predicate_comment_and_powershell_units() -> None:
    rules = dl.compile_rules([dl.Rule(label="n", patterns=[RX], declared=["d.env"])])
    assert dl.findings_in(f"x={NET}1\n", "a.yml", rules) == [(1, "n", f"x={NET}1")]
    assert dl.findings_in(f"x={NET}1\n", "d.env", rules) == []
    assert dl.findings_in(f"  # x={NET}1\n", "a.yml", rules) == []
    assert dl.strip_ps_block_comments("a <# b #> c <# d", False) == ("a  c ", True)
    assert dl.strip_ps_block_comments("'<#' x", False) == ("'<#' x", False)


def test_selftest_passes(tmp_path: Path) -> None:
    res = run_tool("declared_literal", tmp_path, "--selftest")
    assert res.returncode == 0, res.stdout + res.stderr
