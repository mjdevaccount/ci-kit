"""The `# renovate:` annotation manager in renovate/default.json5 reads the shape it exists for,
and only that shape.

Renovate runs weekly on a hosted bot; a regex that silently matches nothing is a pin nobody ever
bumps, and nothing red says so. This reads the pattern OUT OF the preset (no copy to drift), turns
RE2's `(?<name>` into Python's `(?P<name>` (the only syntax difference this pattern uses), and
drives it both ways.

Moved here from edge-ci with the preset (2026-11-03): the proof lives beside the file it proves.
"""
from __future__ import annotations

import json
import pathlib
import re

PRESET = pathlib.Path(__file__).resolve().parent.parent / "renovate" / "default.json5"


def _pattern() -> re.Pattern[str]:
    lines = [ln for ln in PRESET.read_text(encoding="utf-8").splitlines() if "renovate:" in ln and "datasource=(?<" in ln]
    assert len(lines) == 1, f"expected ONE annotation matchString in the preset, found {len(lines)}"
    literal = lines[0].strip().rstrip(",")
    return re.compile(json.loads(literal).replace("(?<", "(?P<"))


def _one(text: str) -> dict[str, str | None]:
    found = [m.groupdict() for m in _pattern().finditer(text)]
    assert len(found) == 1, found
    return found[0]


def test_a_python_constant_under_the_annotation_is_read() -> None:
    m = _one('GATE = "x"\n# renovate: datasource=github-releases depName=oasdiff/oasdiff\nOASDIFF_VERSION = "1.35.0"\n')
    assert (m["datasource"], m["depName"], m["currentValue"]) == ("github-releases", "oasdiff/oasdiff", "1.35.0")


def test_a_workflow_env_line_and_a_v_prefix_and_versioning_are_read() -> None:
    m = _one("    # renovate: datasource=github-tags depName=a/b versioning=loose\n    B_VERSION: v2.10.3\n")
    assert (m["currentValue"], m["versioning"]) == ("2.10.3", "loose")


def test_an_unannotated_version_is_not_read() -> None:
    assert not list(_pattern().finditer('# pinned by hand\nOASDIFF_VERSION = "1.35.0"\n'))


def test_a_prerelease_is_not_read_as_its_base_version() -> None:
    # `1.35.0-rc1` must not be bumped as if it were 1.35.0 -- the pattern ends at the version
    assert not list(_pattern().finditer('# renovate: datasource=github-releases depName=a/b\nV = "1.35.0-rc1"\n'))


def test_the_annotation_must_sit_on_the_line_directly_above() -> None:
    assert not list(_pattern().finditer('# renovate: datasource=github-releases depName=a/b\n\nV = "1.2.3"\n'))
