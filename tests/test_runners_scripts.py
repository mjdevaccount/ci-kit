"""The supervisor's packaged scripts (ci_kit/runners/scripts.py, ci_kit/runners/sh/): what ships, that
it names no consumer, its hash, and the kit's `scripts_dir` copy (--write / --check).

Both directions throughout: a clean copy checks clean and each kind of drift is a finding; the
purity pattern passes the package and catches every estate-shaped name it exists to keep out.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess

import pytest

from ci_kit.runners import KitError, kit_from_dict
from ci_kit.runners.cli import main
from ci_kit.runners.scripts import SH_DIR, script_findings, scripts, scripts_sha256, write_scripts

yaml = pytest.importorskip("yaml")

GOLDEN = pathlib.Path(__file__).parent / "fixtures" / "runners" / "golden"
PKG = pathlib.Path(__file__).resolve().parents[1] / "ci_kit" / "runners"
DOC = pathlib.Path(__file__).resolve().parents[1] / "docs" / "runners.md"

# ci-kit is PUBLIC and the scripts are copied into other trees: nothing in what ships may name the
# deployment it was lifted from -- a consumer's prefixes, paths, accounts, repositories or hosts
_ESTATE = (r"(?i)edge[-_]|/edge\b|mjdev|trade[-_](data|infra|ui|quant)|home[-_]host|elitemini",
           r"|(?<![a-z])home(?![a-z])")
ESTATE = re.compile("".join(_ESTATE))

# THE RUNNERS' OWN TESTS are public too, and they are what a reader copies a kit or a scenario from:
# every runners test module and fixture is held to the same pattern. Two kinds of token are exempt,
# each by exact spelling and in one file only:
#   - this module's pattern and its must-fire examples are estate-shaped BY DESIGN;
#   - the reusable workflows' shipped default pool labels. tests/_runners.py spells the runs-on
#     expression exactly as every workflow does, so it carries them; renaming them moves every
#     caller's jobs to other runners -- a release, not a test edit. The exemption holds only while a
#     workflow still ships the label (test_the_shipped_label_exemption_is_still_needed).
TESTS = pathlib.Path(__file__).resolve().parent
SHIPPED_LABELS = ("home-secure", "home-sealed")
# the reusable workflows' DEPRECATED App inputs, still read as fallbacks (docs/pr-ops/push-token.md):
# test_generic_names_must_fire pins that they still work, so it must spell them
SHIPPED_ALIASES = ("vars.EDGE_BOT_APP_ID", "secrets.EDGE_BOT_PRIVATE_KEY", "EDGE_BOT_APP_ID", "EDGE_BOT_PRIVATE_KEY")
# RUNNER-ADJACENT tests: no "runners" in the name, but they build pools, labels and runner switches
# a reader copies as readily as a kit, so the same scan reads them
ADJACENT = ("test_generic_names_must_fire.py", "test_pr_head_fail_fast.py")
CATCH = ['emit "edge_ci_github_auth 1"', "GH_AUTH_RUN=/run/edge-ci", 'OWNER="${EDGE_CI_OWNER:-x}"',
         "GH_AUTH_OWNER=mjdevaccount", "LIB=/usr/local/lib/edge", "# three trade-data pools", "see home-host docs",
         "INSTANCE=home", "--label home.repo=x"]
# keyed by path under tests/ (a same-named file elsewhere gets nothing), and each token removed only
# where it stands alone: a shipped label with a suffix glued on (-ops, .x) is still a hit
EXEMPT = {"_runners.py": SHIPPED_LABELS, "test_generic_names_must_fire.py": (*SHIPPED_ALIASES, *SHIPPED_LABELS),
          pathlib.Path(__file__).name: (*_ESTATE, *CATCH, *SHIPPED_LABELS, *SHIPPED_ALIASES)}


def _unexempt(rel: str, line: str) -> str:
    """`line` with every token exempt for the file at `rel` (posix, under tests/) removed, whole only."""
    for token in EXEMPT.get(rel, ()):
        line = re.sub(rf"(?<![\w.-]){re.escape(token)}(?![\w.-])", "", line)
    return line


def runner_tests(root: pathlib.Path = TESTS) -> list[pathlib.Path]:
    """Every runners test module and fixture file under `root` (*runners* by name, fixtures/runners/, and
    the ADJACENT modules at its top)."""
    return [p for p in sorted(root.rglob("*")) if p.is_file() and "__pycache__" not in p.parts
            and ("runners" in p.name or (root / "fixtures" / "runners") in p.parents
                 or (p.parent == root and p.name in ADJACENT))]


def estate_hits(root: pathlib.Path = TESTS) -> list[str]:
    """`path:line: text` for every estate-shaped name in the runner tests, the listed exemptions removed."""
    hits = []
    for p in runner_tests(root):
        rel = p.relative_to(root).as_posix()
        for i, line in enumerate(p.read_bytes().decode("utf-8").splitlines(), 1):
            if ESTATE.search(_unexempt(rel, line)):
                hits.append(f"{rel}:{i}: {line.strip()}")
    return hits


RUN = {"supervisor.sh"}  # run by a unit; every other script is SOURCED by it
SOURCED = re.compile(r'^\s*(?:if\s+)?\.\s+"\$\(dirname "\$\(readlink -f "\$\{BASH_SOURCE\[0\]\}"\)"\)/([^"]+)"', re.M)


def test_what_ships():
    assert sorted(scripts()) == ["github-auth.sh", "runs.sh", "supervisor.sh"]
    for name, body in scripts().items():
        assert b"\r" not in body, f"{name}: CRLF (the .gitattributes eol=lf rule is what keeps the hash stable)"
        assert body.startswith(b"#!/usr/bin/env bash\n" if name in RUN else b"# shellcheck shell=bash"), name


def sourced_beside(body: str) -> list[str]:
    """Every file a script sources from beside itself (`. "$(dirname "$(readlink -f ...)")/<name>"`)."""
    return SOURCED.findall(body)


def test_the_supervisor_sources_only_what_ships():
    """Everything supervisor.sh sources from its own directory is a packaged script: a name the package
    does not ship would be a supervisor that goes blind (exit 2) on the first tick after --write."""
    body = scripts()["supervisor.sh"].decode("utf-8")
    assert sorted(sourced_beside(body)) == ["github-auth.sh", "runs.sh"]
    assert set(sourced_beside(body)) <= set(scripts())
    # MUST-FIRE: the reader sees a renamed source, in both of the supervisor's spellings
    planted = body.replace('/runs.sh"', '/ci-runs.sh"').replace('/github-auth.sh"; then', '/auth.sh"; then')
    assert sorted(sourced_beside(planted)) == ["auth.sh", "ci-runs.sh"]


@pytest.mark.parametrize("name", sorted(scripts()))
def test_every_script_parses(name, tmp_path):
    bash = shutil.which("bash")  # resolved: a bare "bash" on Windows is System32's WSL launcher
    if bash is None:
        if os.environ.get("CI"):
            pytest.fail("bash must be on PATH: a supervisor cannot run without it")
        pytest.skip("bash not on PATH (workstation)")
    f = tmp_path / name
    f.write_bytes(scripts()[name])
    r = subprocess.run([bash, "-n", f.as_posix()], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_the_package_names_no_consumer():
    hits = [f"{p.relative_to(PKG.parent)}:{i}: {line.strip()}"
            for p in [*sorted(PKG.rglob("*")), DOC] if p.is_file() and p.suffix in {".sh", ".jq", ".py", ".md"}
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if ESTATE.search(line)]
    assert hits == []


def test_the_runner_tests_name_no_consumer():
    # the scope is real: the golden fixture, the kit tests and the selection constants are all in it
    names = {p.name for p in runner_tests()}
    assert {"_runners.py", "test_runners_must_fire.py", "test_review_gate_runners_must_fire.py", "kit.yml",
            "runners.yml", "ci-runners.json", *ADJACENT} <= names, sorted(names)
    assert estate_hits() == []


def test_the_shipped_label_exemption_is_still_needed():
    wf = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows"
    text = "".join(p.read_text(encoding="utf-8") for p in sorted(wf.glob("*.yml")))
    for label in SHIPPED_LABELS:
        assert f'"{label}"' in text, f"no workflow ships {label} any more: drop it from SHIPPED_LABELS"
    for alias in SHIPPED_ALIASES[:2]:
        assert "${{ " + alias + " }}" in text, f"no workflow reads {alias} any more: drop it from SHIPPED_ALIASES"


def test_an_adjacent_test_is_read_and_its_exemption_is_its_own(tmp_path):
    """MUST-FIRE: an estate name planted in an ADJACENT module is a hit; the deprecated App inputs are
    exempt in test_generic_names_must_fire.py only, and only standing alone."""
    alias = f"X = '{SHIPPED_ALIASES[2]}'\n"
    (tmp_path / ADJACENT[1]).write_text(CATCH[7] + "\n", encoding="utf-8")
    (tmp_path / ADJACENT[0]).write_text(alias + f"Y = '{SHIPPED_ALIASES[2]}_V2'\n", encoding="utf-8")
    (tmp_path / "deep").mkdir()
    (tmp_path / "deep" / ADJACENT[0]).write_text(alias, encoding="utf-8")
    (tmp_path / "test_other.py").write_text(alias, encoding="utf-8")
    assert {p.relative_to(tmp_path).as_posix() for p in runner_tests(tmp_path)} == set(ADJACENT)
    assert [h.split(": ", 1)[0] for h in estate_hits(tmp_path)] == [f"{ADJACENT[0]}:2", f"{ADJACENT[1]}:1"]


def test_the_runner_test_scan_catches_a_planted_name(tmp_path):
    """MUST-FIRE: an estate name in a runners fixture or test is a hit; a shipped label is exempt only in
    tests/_runners.py itself, and only standing alone; a test outside the runners' scope is not read."""
    # the planted text is built from this module's own exempt tokens, so it names nothing here
    label, name = f"X = '{SHIPPED_LABELS[0]}'\n", CATCH[7] + "\n"
    (tmp_path / "fixtures" / "runners").mkdir(parents=True)
    (tmp_path / "fixtures" / "runners" / "kit.yml").write_text(name, encoding="utf-8")
    (tmp_path / "_runners.py").write_text(label, encoding="utf-8")
    (tmp_path / "test_runners_x.py").write_text(label, encoding="utf-8")
    (tmp_path / "test_other.py").write_text(name, encoding="utf-8")
    assert {p.name for p in runner_tests(tmp_path)} == {"kit.yml", "_runners.py", "test_runners_x.py"}
    assert [h.split(":")[0] for h in estate_hits(tmp_path)] == ["fixtures/runners/kit.yml", "test_runners_x.py"]
    # review 889 F1: an exemption is the FILE's, not its basename's, and a token's, not a substring's
    deep = tmp_path / "fixtures" / "runners" / "deep"
    deep.mkdir()
    (deep / pathlib.Path(__file__).name).write_text(CATCH[3] + "\n", encoding="utf-8")
    (deep / "_runners.py").write_text(label, encoding="utf-8")
    (tmp_path / "_runners.py").write_text(f"X = '{SHIPPED_LABELS[0]}-ops'\nY = '{SHIPPED_LABELS[1]}.x'\n",
                                          encoding="utf-8")
    assert [h.split(": ", 1)[0] for h in estate_hits(tmp_path)] == [
        "_runners.py:1", "_runners.py:2", "fixtures/runners/deep/_runners.py:1",
        f"fixtures/runners/deep/{pathlib.Path(__file__).name}:1", "fixtures/runners/kit.yml:1", "test_runners_x.py:1"]


@pytest.mark.parametrize("line", CATCH)
def test_the_purity_pattern_catches_an_estate_name(line):
    assert ESTATE.search(line), line


@pytest.mark.parametrize("line", ["an edge case", "the trade-off of backfill", "knowledge", "homepage"])
def test_the_purity_pattern_passes_plain_english(line):
    assert not ESTATE.search(line), line


def test_scripts_sha256_is_what_coreutils_computes_on_the_copies(tmp_path):
    bash = shutil.which("bash")  # resolved: a bare "bash" on Windows is System32's WSL launcher
    if bash is None or shutil.which("sha256sum") is None:
        if os.environ.get("CI"):
            pytest.fail("bash/sha256sum not on PATH: the host-side recompute cannot be proven")
        pytest.skip("bash/sha256sum not on PATH (workstation)")
    write_scripts(tmp_path)
    names = " ".join(sorted(scripts()))
    out = subprocess.run([bash, "-c", f"cd \"$1\" && LC_ALL=C sha256sum -t -- {names} | sha256sum | cut -c1-12",
                          "_", tmp_path.as_posix()], capture_output=True, text=True, check=True).stdout.strip()
    assert out == scripts_sha256() and re.fullmatch(r"[0-9a-f]{12}", out)


def test_scripts_sha256_moves_with_any_byte_and_any_name():
    base = scripts()
    assert scripts_sha256(base) == scripts_sha256()
    first = sorted(base)[0]
    assert scripts_sha256({**base, first: base[first] + b"\n"}) != scripts_sha256(base)
    assert scripts_sha256({("x" + k): v for k, v in base.items()}) != scripts_sha256(base)


def test_script_findings_both_directions(tmp_path):
    d = tmp_path / "sh"
    assert {f.split(" ")[0] for f in script_findings(d, "sh", "W")} == {f"sh/{n}" for n in scripts()}
    write_scripts(d)
    assert script_findings(d, "sh", "W") == []
    (d / "runs.sh").write_bytes((SH_DIR / "runs.sh").read_bytes().replace(b"RUN_PAGES=3", b"RUN_PAGES=1"))
    (d / "old.sh").write_text("# a script the package no longer ships\n", encoding="utf-8")
    got = script_findings(d, "sh", "W")
    assert got == ["sh/runs.sh is not ci-kit's runs.sh: run W",
                   "sh/old.sh is not a ci-kit script (scripts_dir is the package's alone): delete it"]


def _root(tmp_path) -> pathlib.Path:
    root = tmp_path / "repo"
    shutil.copytree(GOLDEN, root)
    k = root / "kit.yml"
    d = yaml.safe_load(k.read_text(encoding="utf-8"))
    d["scripts_dir"] = "generated/sh"
    d["renders"] = {"impl": {"output": "monitoring/impl.yml", "template": 'want: "${scripts_sha256}"\n'}}
    k.write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf-8")
    return root


def _cli(root, mode) -> int:
    return main([mode, "--config", str(root / "runners.yml"), "--kit", str(root / "kit.yml"), "--root", str(root)])


def test_write_then_check_then_drift(tmp_path, capsys):
    root = _root(tmp_path)
    assert _cli(root, "--check") == 1
    assert "generated/sh/runs.sh is not ci-kit's runs.sh" in capsys.readouterr().err
    assert _cli(root, "--write") == 0
    assert f"scripts_sha256 {scripts_sha256()}" in capsys.readouterr().out
    assert {p.name for p in (root / "generated" / "sh").iterdir()} == set(scripts())
    assert (root / "monitoring" / "impl.yml").read_text(encoding="utf-8") == f'want: "{scripts_sha256()}"\n'
    assert _cli(root, "--check") == 0
    (root / "generated" / "sh" / "github-auth.sh").write_text("# hand edit\n", encoding="utf-8")
    assert _cli(root, "--check") == 1
    assert "github-auth.sh is not ci-kit's github-auth.sh" in capsys.readouterr().err


def test_a_kit_without_scripts_dir_writes_and_checks_no_scripts(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(GOLDEN, root)
    assert _cli(root, "--write") == 0 and _cli(root, "--check") == 0
    assert not any(p.suffix == ".sh" for p in root.rglob("*"))


@pytest.mark.parametrize(("edit", "why"), [
    ({"scripts_dir": ""}, "scripts_dir must be a non-empty string"),
    ({"scripts_dir": "gen", "out_dir": "gen"}, "scripts_dir must be its own directory"),
    ({"scripts_dir": "gen/sh", "alloc_jq": "gen/sh/alloc.jq"}, "alloc_jq must not be inside scripts_dir"),
])
def test_a_malformed_scripts_dir_is_refused(edit, why):
    d = yaml.safe_load((GOLDEN / "kit.yml").read_text(encoding="utf-8"))
    with pytest.raises(KitError, match=why):
        kit_from_dict({**d, **edit})
    kit_from_dict({**d, "scripts_dir": "gen/sh", "alloc_jq": "gen/alloc.jq"})  # the other direction


def _impl_snippet() -> str:
    """supervisor.sh's IMPL_SHA lines, as they ship."""
    body = scripts()["supervisor.sh"].decode("utf-8")
    m = re.search(r'^IMPL_SHA="\$\(.*?\n\[\[ "\$\{IMPL_SHA\}" =~ .*?$', body, re.M | re.S)
    assert m, "the IMPL_SHA block moved -- re-aim this test"
    return m.group(0)


def test_the_supervisor_publishes_what_scripts_sha256_computes(tmp_path):
    """The impl_info the supervisor emits is the kit's scripts_sha256 of the directory it runs from:
    the same value both ways, and a changed byte (or a stray file) in that directory moves it."""
    bash = shutil.which("bash")
    if bash is None:
        if os.environ.get("CI"):
            pytest.fail("bash must be on PATH: a supervisor cannot run without it")
        pytest.skip("bash not on PATH (workstation)")
    write_scripts(tmp_path)
    # the snippet hashes the directory the script runs from: aim it at the copies (the probe lives elsewhere)
    here = '"$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"'
    snippet = _impl_snippet()
    assert snippet.count(here) == 1, "the IMPL_SHA block no longer hashes its own directory"
    run = snippet.replace(here, f'"{tmp_path.as_posix()}"') + '\necho "$IMPL_SHA"\n'
    probe_dir = tmp_path.parent / (tmp_path.name + "-probe")
    probe_dir.mkdir()
    probe = probe_dir / "probe.sh"
    probe.write_text(run, encoding="utf-8", newline="\n")

    def impl() -> str:
        return subprocess.run([bash, probe.as_posix()], capture_output=True, text=True, check=True).stdout.strip()

    assert impl() == scripts_sha256()
    (tmp_path / "runs.sh").write_bytes(scripts()["runs.sh"] + b"#\n")
    assert impl() not in (scripts_sha256(), "unknown")
    (tmp_path / "runs.sh").write_bytes(scripts()["runs.sh"])
    (tmp_path / "zz.sh").write_text("#\n", encoding="utf-8")
    assert impl() != scripts_sha256()
