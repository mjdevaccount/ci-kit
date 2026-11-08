"""The four lint workflows take the caller's LIGHT lane before its CI_RUNNER (v1.18.0).

A self-hosted pool admits a runner at its pool's measured peak memory; a doc lint on trade-data's
CI_RUNNER pool was booked at 2560 MB and held the room a unit job needed (home-host, 2026-11-08:
~75% of queued jobs were these lints). Each lint workflow must read vars.CI_RUNNER_LIGHT first,
then CI_RUNNER, then ubuntu-latest -- and an explicit `runner` input still wins. Must-fire: the
check is run against a mutated copy that drops the light lane and must fail.
"""

from __future__ import annotations

import pathlib
import re
import unittest

_WF = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows"
LINTS = ("doc-refs.yml", "dup-code.yml", "secret-argv.yml", "gitleaks.yml")
_LIGHT = "vars.CI_RUNNER_LIGHT && fromJSON(vars.CI_RUNNER_LIGHT)"
_HOME = "vars.CI_RUNNER && fromJSON(vars.CI_RUNNER)"


def problems(text: str) -> list[str]:
    """Every runs-on in a lint workflow: input (if any) > LIGHT > CI_RUNNER > ubuntu-latest."""
    out = []
    lines = re.findall(r"^\s*runs-on:\s*(.+)$", text, re.M)
    if not lines:
        return ["no runs-on"]
    for expr in lines:
        if _LIGHT not in expr:
            out.append(f"no light lane: {expr}")
            continue
        if _HOME not in expr or "'ubuntu-latest'" not in expr:
            out.append(f"light lane without the CI_RUNNER/hosted fallback: {expr}")
            continue
        if expr.index(_LIGHT) > expr.index(_HOME):
            out.append(f"CI_RUNNER read before the light lane: {expr}")
        if "inputs.runner" in expr and expr.index("inputs.runner") > expr.index(_LIGHT):
            out.append(f"the runner input no longer wins: {expr}")
    return out


class TestLightLane(unittest.TestCase):
    def test_every_lint_takes_the_light_lane(self):
        for f in LINTS:
            with self.subTest(f):
                self.assertEqual(problems((_WF / f).read_text(encoding="utf-8")), [])

    def test_check_fires_on_each_mutation(self):
        text = (_WF / "doc-refs.yml").read_text(encoding="utf-8")
        mutations = {
            "light lane dropped": text.replace(_LIGHT + " || ", ""),
            "order swapped": text.replace(_LIGHT, "__L__").replace(_HOME, _LIGHT).replace("__L__", _HOME),
            "hosted fallback dropped": text.replace(" || 'ubuntu-latest'", ""),
        }
        for why, bad in mutations.items():
            with self.subTest(why):
                self.assertNotEqual(bad, text, f"mutation '{why}' did not apply")
                self.assertNotEqual(problems(bad), [], why)


if __name__ == "__main__":
    unittest.main()
