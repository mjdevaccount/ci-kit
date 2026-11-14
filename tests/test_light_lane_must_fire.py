"""The five lint workflows take the caller's LIGHT lane before its CI_RUNNER (v1.18.0).

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
LINTS = ("doc-refs.yml", "dup-code.yml", "secret-argv.yml", "hidden-unicode.yml", "gitleaks.yml")
_LANES = ("inputs.runner", "vars.CI_RUNNER_LIGHT", "vars.CI_RUNNER", "'\"ubuntu-latest\"'")


def problems(text: str) -> list[str]:
    """Every runs-on: ONE fromJSON over (input) || LIGHT || CI_RUNNER || hosted, in that order."""
    out = []
    lines = re.findall(r"^\s*runs-on:\s*(.+)$", text, re.M)
    if not lines:
        return ["no runs-on"]
    for expr in lines:
        m = re.fullmatch(r"\$\{\{ fromJSON\((.+)\) \}\}", expr.strip())
        if m is None:
            out.append(f"not one fromJSON over the lanes: {expr}")
            continue
        got = [x.strip() for x in m.group(1).split("||")]
        want = [x for x in _LANES if x != "inputs.runner" or "inputs.runner" in got]
        if got != want:
            out.append(f"lanes {got} != {want}")
    return out


class TestLightLane(unittest.TestCase):
    def test_every_lint_takes_the_light_lane(self):
        for f in LINTS:
            with self.subTest(f):
                self.assertEqual(problems((_WF / f).read_text(encoding="utf-8")), [])

    def test_check_fires_on_each_mutation(self):
        text = (_WF / "doc-refs.yml").read_text(encoding="utf-8")
        mutations = {
            "light lane dropped": text.replace(" || vars.CI_RUNNER_LIGHT", ""),
            "order swapped": text.replace("vars.CI_RUNNER_LIGHT || vars.CI_RUNNER ||",
                                          "vars.CI_RUNNER || vars.CI_RUNNER_LIGHT ||"),
            "hosted fallback dropped": text.replace(""" || '"ubuntu-latest"'""", ""),
            "input demoted": text.replace("inputs.runner || vars.CI_RUNNER_LIGHT",
                                          "vars.CI_RUNNER_LIGHT || inputs.runner"),
        }
        for why, bad in mutations.items():
            with self.subTest(why):
                self.assertNotEqual(bad, text, f"mutation '{why}' did not apply")
                self.assertNotEqual(problems(bad), [], why)


if __name__ == "__main__":
    unittest.main()
