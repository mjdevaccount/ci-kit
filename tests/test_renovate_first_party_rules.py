"""A first-party package bump is never held by the weekly grouping, schedule or age wait.

ci-pin-drift goes red the day a kit releases. If Renovate holds that bump (3-day minimumReleaseAge,
weekend schedule, the weekly "python (non-major)" group), pin-drift stays red until a session
bumps by hand. 2026-11-04: finance-ops moved llm-ports to PyPI, its depName became `llm-ports`,
the name-keyed "first-party kits" rule stopped matching, and v1.1.0 sat pending while pin-drift
was red. Renovate applies packageRules in order and a later match wins, so the rule matched by
source URL must come AFTER every rule that groups, schedules or age-gates a registry manager.
"""
from __future__ import annotations

import pathlib

import json5

PRESET = pathlib.Path(__file__).resolve().parent.parent / "renovate" / "default.json5"
HOLDS = ("groupName", "schedule", "minimumReleaseAge")
REGISTRY_MANAGERS = {"pep621", "pip_requirements", "pip-compile", "npm", "nuget"}


def _rules() -> list[dict]:
    return json5.loads(PRESET.read_text(encoding="utf-8"))["packageRules"]


def _source_rule() -> tuple[int, dict]:
    found = [(i, r) for i, r in enumerate(_rules()) if "https://github.com/mjdevaccount/**" in r.get("matchSourceUrls", [])]
    assert len(found) == 1, f"expected ONE first-party matchSourceUrls rule, found {len(found)}"
    return found[0]


def test_the_source_url_rule_releases_every_hold() -> None:
    _, rule = _source_rule()
    assert rule["groupName"] is None
    assert rule["schedule"] == ["at any time"]
    assert rule["minimumReleaseAge"] is None
    # only the source URL: a name or manager filter would re-open the gap it closes
    assert set(rule) - {"description"} == {"matchSourceUrls", *HOLDS}


def test_no_later_rule_puts_a_hold_back_on_a_registry_manager() -> None:
    at, _ = _source_rule()
    # a later rule re-holds a kit when it sets a hold AND reaches registry packages: a registry
    # manager (or no manager filter) and no package list, or a package list naming a kit
    later = [r for r in _rules()[at + 1:]
             if any(k in r and r[k] is not None for k in HOLDS)
             and (not r.get("matchManagers") or set(r["matchManagers"]) & REGISTRY_MANAGERS)
             and (not r.get("matchPackageNames")
                  or any("mjdevaccount" in str(p).lower() or str(p) == "llm-ports" for p in r["matchPackageNames"]))]
    assert not later, f"a rule after the first-party source rule re-holds registry packages: {later}"


def test_the_source_rule_comes_after_the_python_group() -> None:
    at, _ = _source_rule()
    python_group = [i for i, r in enumerate(_rules()) if r.get("groupName") == "python (non-major)"]
    assert python_group and max(python_group) < at
