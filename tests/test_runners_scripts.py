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
ESTATE = re.compile(r"(?i)edge[-_]|/edge\b|mjdev|trade[-_](data|infra|ui|quant)|home[-_]host|elitemini"
                    r"|(?<![a-z])home(?![a-z])")


def test_what_ships():
    assert sorted(scripts()) == ["github-auth.sh", "runs.sh"]
    for name, body in scripts().items():
        assert b"\r" not in body, f"{name}: CRLF (the .gitattributes eol=lf rule is what keeps the hash stable)"
        assert body.startswith(b"# shellcheck shell=bash"), name


def test_the_package_names_no_consumer():
    hits = [f"{p.relative_to(PKG.parent)}:{i}: {line.strip()}"
            for p in [*sorted(PKG.rglob("*")), DOC] if p.is_file() and p.suffix in {".sh", ".jq", ".py", ".md"}
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if ESTATE.search(line)]
    assert hits == []


@pytest.mark.parametrize("line", [
    'emit "edge_ci_github_auth 1"', "GH_AUTH_RUN=/run/edge-ci", 'OWNER="${EDGE_CI_OWNER:-x}"',
    "GH_AUTH_OWNER=mjdevaccount", "LIB=/usr/local/lib/edge", "# three trade-data pools", "see home-host docs",
    'INSTANCE=home', "--label home.repo=x",
])
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
