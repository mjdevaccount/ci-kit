"""CI-only tooling lands on green with no age wait, majors included; nothing that ships rides along.

ci-kit#139 (the Renovate validator pin in ci.yml, 44 -> 45) sat behind `renovate/stability-days`,
went conflicted behind main, and was superseded by a hand PR (#144). A tool only CI runs is proven
by the PR that bumps it, so the preset lifts the 3-day wait and the weekend window for it and lets
its majors auto-merge.

Resolved over the WHOLE packageRules list (the floating-digest test's resolver: later match wins),
both directions: the CI-tooling shapes get the fast lane, and every shape outside it -- a docker
`services:` image in a workflow (a postgres major above all), a first-party kit, the review gate's
engine, a fails-green action major, an annotation pin outside CI files -- keeps the policy it had.
"""
from __future__ import annotations

import pytest
from test_renovate_floating_digest import _resolve
from test_renovate_first_party_rules import KIT_WINDOW

WF = ".github/workflows/ci.yml"


def _action(name: str, update_type: str, dep_type: str = "action") -> dict:
    return {"manager": "github-actions", "datasource": "github-tags", "depType": dep_type,
            "updateType": update_type, "packageName": name, "packageFile": WF}


def _pin(name: str, update_type: str, package_file: str = WF, datasource: str = "npm") -> dict:
    return {"manager": "custom.regex", "datasource": datasource, "updateType": update_type,
            "packageName": name, "packageFile": package_file}


def _fast(cfg: dict) -> None:
    assert cfg.get("minimumReleaseAge") is None
    assert cfg.get("automerge") is True
    assert cfg.get("schedule") == KIT_WINDOW


@pytest.mark.parametrize("update_type", ["major", "minor", "patch", "digest"])
def test_the_case_that_set_it_renovate_validator_pin(update_type: str) -> None:
    """#139: `# renovate: datasource=npm depName=renovate` over RENOVATE_VERSION in ci.yml."""
    _fast(_resolve(_pin("renovate", update_type)))


@pytest.mark.parametrize("update_type", ["major", "minor", "patch", "digest", "pinDigest"])
@pytest.mark.parametrize("dep_type", ["action", "workflow"])
def test_third_party_uses_pin_is_fast(update_type: str, dep_type: str) -> None:
    _fast(_resolve(_action("actions/checkout", update_type, dep_type)))


@pytest.mark.parametrize("name,file", [("oasdiff/oasdiff", "ci_kit/api_diff.py"),
                                       ("kucherenko/jscpd", "ci_kit/dup_code.py"),
                                       ("gitleaks/gitleaks", "ci_kit/gitleaks.py")])
def test_kit_gate_binary_pin_is_fast(name: str, file: str) -> None:
    _fast(_resolve(_pin(name, "major", file, "github-releases")))


def test_annotation_pin_outside_ci_files_keeps_the_default() -> None:
    """A `# renovate:` pin in a script the preset cannot call CI-only (any .py/.sh in a consumer)."""
    cfg = _resolve(_pin("some/tool", "major", "scripts/deploy.sh", "github-releases"))
    assert cfg["minimumReleaseAge"] == "3 days"
    assert cfg["automerge"] is False


@pytest.mark.parametrize("dep_type", ["service", "container", "docker"])
@pytest.mark.parametrize("name", ["postgres", "timescale/timescaledb-ha"])
def test_a_workflow_database_image_major_is_still_held(dep_type: str, name: str) -> None:
    """The same manager reads `services:` / `container:` images; a postgres major there is a
    production-tracking pg_upgrade decision, never CI tooling."""
    cfg = {**_action(name, "major", dep_type), "datasource": "docker"}
    cfg = _resolve(cfg)
    assert cfg.get("dependencyDashboardApproval") is True
    assert cfg.get("automerge") is False
    assert "schedule" not in cfg or cfg["schedule"] != KIT_WINDOW


@pytest.mark.parametrize("dep_type", ["service", "container"])
def test_a_workflow_service_image_keeps_the_age_wait(dep_type: str) -> None:
    cfg = _resolve({**_action("redis", "minor", dep_type), "datasource": "docker"})
    assert cfg["minimumReleaseAge"] == "3 days"


@pytest.mark.parametrize("name", ["dorny/paths-filter", "gitleaks/gitleaks-action"])
def test_fails_green_action_major_still_waits_for_a_human(name: str) -> None:
    cfg = _resolve(_action(name, "major"))
    assert cfg["automerge"] is False
    assert "fails-green" in cfg["labels"]


def test_review_gate_engine_is_not_fast() -> None:
    assert _resolve(_action("anthropics/claude-code-action", "minor"))["automerge"] is False
    cli = _resolve(_pin("@anthropic-ai/claude-code", "major", ".github/workflows/review-gate.yml"))
    assert cli["automerge"] is False
    assert cli["minimumReleaseAge"] == "3 days"


def test_first_party_kit_major_keeps_its_own_rules() -> None:
    """A kit's `uses:` line: the CI-tooling rule excludes it, so its schedule is the kit rule's."""
    cfg = _resolve(_action("mjdevaccount/ci-kit", "major"))
    assert cfg["minimumReleaseAge"] is None  # first-party rule, not this one
    assert cfg["groupName"] is None
