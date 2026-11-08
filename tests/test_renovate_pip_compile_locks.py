"""A compiled, hash-locked requirements file is Renovate pip-compile's, never pip_requirements'.

trade-infra#823 (2026-11-08): pip_requirements edited a `uv pip compile --generate-hashes` output
line by line, kept the old hashes, and the image build failed `--require-hashes`. The preset routes
such locks by NAME (`requirements.lock`): pip-compile matches it, pip_requirements' default pattern
cannot. Both directions are pinned here, because the guard is the pair -- a pip-compile pattern that
also took hand-pinned requirements.txt files would WARN on every one of them, and a lock name that
pip_requirements' default also matched would bring the stale-hash edit back.
"""
from __future__ import annotations

import fnmatch
import pathlib
import re

import json5

PRESET = pathlib.Path(__file__).resolve().parent.parent / "renovate" / "default.json5"
# Renovate's default for pip_requirements (docs.renovatebot.com/modules/manager/pip_requirements/,
# read 2026-11-08 at 44.222.4). JS `\w` == Python `\w` for these ASCII names.
PIP_REQUIREMENTS_DEFAULT = r"(^|/)[\w-]*requirements([-._]\w+)?\.(txt|pip)$"

LOCKS = ["requirements.lock", "infra/research-mcp/requirements.lock"]
HAND_FILES = [
    "requirements.txt",
    "ops-api/requirements.txt",
    "rebuild/requirements.txt",
    "scripts/requirements.txt",
    "requirements-dev.txt",
    "requirements.in",
]


def _preset() -> dict:
    return json5.loads(PRESET.read_text(encoding="utf-8"))


def _pip_compile_patterns() -> list[re.Pattern[str]]:
    pats = _preset()["pip-compile"]["managerFilePatterns"]
    assert pats, "pip-compile has no managerFilePatterns: it matches nothing by default"
    out = []
    for p in pats:
        assert p.startswith("/") and p.endswith("/"), f"expected a /regex/ pattern, got {p!r}"
        out.append(re.compile(p[1:-1]))
    return out


def test_pip_compile_takes_every_lock() -> None:
    pats = _pip_compile_patterns()
    for f in LOCKS:
        assert any(p.search(f) for p in pats), f"pip-compile does not match {f}"


def test_pip_compile_leaves_hand_files_alone() -> None:
    pats = _pip_compile_patterns()
    for f in HAND_FILES:
        assert not any(p.search(f) for p in pats), f"pip-compile would parse hand file {f} (WARN per file)"


def test_pip_requirements_default_cannot_reach_a_lock() -> None:
    default = re.compile(PIP_REQUIREMENTS_DEFAULT)
    for f in LOCKS:
        assert not default.search(f), f"pip_requirements' default matches {f}: the stale-hash edit is back"
    assert default.search("ops-api/requirements.txt"), "pattern copy drifted: it no longer matches a hand file"


def _hits(pattern: str, path: str) -> bool:
    """A Renovate managerFilePatterns entry: `/regex/`, else a minimatch glob (fnmatch is close enough
    for the `**/name` shapes this preset uses; `*` crossing `/` only makes it stricter here)."""
    if pattern.startswith("/") and pattern.endswith("/") and len(pattern) > 1:
        return bool(re.search(pattern[1:-1], path))
    return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, pattern.removeprefix("**/"))


def test_no_rule_points_pip_requirements_at_a_lock() -> None:
    extra = _preset().get("pip_requirements", {}).get("managerFilePatterns", [])
    for p in extra:
        assert not any(_hits(p, f) for f in LOCKS), f"pip_requirements extended onto a lock: {p}"


def test_pattern_reader_sees_globs_and_regexes() -> None:
    assert _hits("**/requirements.lock", "infra/research-mcp/requirements.lock")
    assert _hits("requirements.lock", "requirements.lock")
    assert _hits("/(^|/)requirements\\.lock$/", "infra/x/requirements.lock")
    assert not _hits("**/requirements.lock", "infra/x/requirements.txt")
