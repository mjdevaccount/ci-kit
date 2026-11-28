"""The supervisor's SCRIPTS: bash and jq that ship as ci-kit package data (`ci_kit/runners/sh/`).

A runner host runs bash, jq and curl, not ci-kit: the scripts are COPIED into the consumer's tree
by `ci-runners --write` (the kit's `scripts_dir`) and installed from there. `--check` holds every
copy byte-equal to the package's and refuses a file in `scripts_dir` the package does not ship (a
script renamed or dropped upstream would otherwise stay installed, stale). `scripts_dir` is the
package's alone.

`scripts_sha256` names the set: sha256 over the `sha256sum` manifest of every script, sorted by
name, first 12 hex characters (the width of `source_sha256`). A host can recompute it from the
installed copies with nothing but coreutils:

    cd <dir> && LC_ALL=C sha256sum -t -- <every name, sorted> | sha256sum | cut -c1-12

so a supervisor can PUBLISH the implementation it runs and an alert can hold it to the one the kit
pins (a `renders` template may name `${scripts_sha256}`).
"""

from __future__ import annotations

import hashlib
import pathlib

SH_DIR = pathlib.Path(__file__).with_name("sh")


def scripts() -> dict[str, bytes]:
    """name -> bytes of every packaged script, sorted by name (dotfiles and caches excluded)."""
    return {p.name: p.read_bytes() for p in sorted(SH_DIR.iterdir(), key=lambda p: p.name)
            if p.is_file() and not p.name.startswith(".")}


def scripts_sha256(files: dict[str, bytes] | None = None) -> str:
    """The set's hash (module docstring): what `sha256sum ... | sha256sum | cut -c1-12` prints."""
    files = scripts() if files is None else files
    manifest = "".join(f"{hashlib.sha256(files[n]).hexdigest()}  {n}\n" for n in sorted(files))
    return hashlib.sha256(manifest.encode("utf-8")).hexdigest()[:12]


def write_scripts(dest: pathlib.Path) -> list[pathlib.Path]:
    """Copy every script into `dest` (created), LF bytes as shipped; returns the paths written."""
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for name, body in scripts().items():
        (dest / name).write_bytes(body)
        out.append(dest / name)
    return out


def script_findings(dest: pathlib.Path, label: str, write_command: str) -> list[str]:
    """A missing or drifted copy, or a file the package does not ship, under `dest` (`label` names it)."""
    want = scripts()
    findings = [f"{label}/{name} is not ci-kit's {name}: run {write_command}" for name, body in want.items()
                if not (dest / name).is_file() or (dest / name).read_bytes() != body]
    if dest.is_dir():
        findings += [f"{label}/{p.name} is not a ci-kit script (scripts_dir is the package's alone): delete it"
                     for p in sorted(dest.iterdir(), key=lambda p: p.name) if p.name not in want]
    return findings
