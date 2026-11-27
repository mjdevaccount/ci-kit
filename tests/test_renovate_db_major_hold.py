"""A database-image MAJOR waits for Dependency Dashboard approval, by name, and never lands itself.

Measured 2026-11-26: a consumer's "update database images to v19" and edge-platform's postgres
18 -> 19 CI service container sat in "Pending Status Checks" for six days. Docker Hub's
`tag_last_pushed` is the release timestamp and docker-library re-pushes floating tags every few
days, so the 3-day age wait re-armed under each rebuild: a hold nothing named, in the dashboard
section that promises the wait will end. The preset now holds the major under "Pending
Approval" with the gate in the title and the PR body.

Resolved over the WHOLE packageRules list (later match wins, the floating-digest test's
resolver), so a rule added below that re-arms automerge or drops the approval turns this red.
Both directions: the non-major database updates must NOT pick up the approval hold.
"""
from __future__ import annotations

import pytest
from test_renovate_floating_digest import _resolve

DB_IMAGES = ["timescale/timescaledb-ha", "postgres", "edoburu/pgbouncer",
             "timescale/pgai-vectorizer-worker", "woblerr/pgbackrest_exporter"]
# docker-compose (langfuse-pg), dockerfile (backup sidecar FROM), github-actions (a CI service container)
MANAGERS = ["docker-compose", "dockerfile", "github-actions"]


def _dep(name: str, manager: str, update_type: str) -> dict:
    return {"manager": manager, "datasource": "docker", "updateType": update_type, "packageName": name}


@pytest.mark.parametrize("manager", MANAGERS)
@pytest.mark.parametrize("name", DB_IMAGES)
def test_db_major_waits_for_dashboard_approval(name: str, manager: str) -> None:
    cfg = _resolve(_dep(name, manager, "major"))
    assert cfg.get("dependencyDashboardApproval") is True
    assert cfg.get("automerge") is False  # github-actions majors auto-merge by an earlier rule


@pytest.mark.parametrize("manager", MANAGERS)
def test_db_major_names_its_gate(manager: str) -> None:
    cfg = _resolve(_dep("postgres", manager, "major"))
    assert "upgrade plan" in cfg["commitMessageSuffix"]  # the dashboard line shows the PR title
    assert "dba" in cfg["labels"]
    notes = " ".join(cfg["prBodyNotes"])
    assert "extension" in notes and "upgrade plan" in notes and "infra/backup/Dockerfile" in notes


def test_db_major_has_no_age_wait_that_rearms_on_a_repush() -> None:
    assert _resolve(_dep("postgres", "docker-compose", "major")).get("minimumReleaseAge") is None


@pytest.mark.parametrize("update_type", ["minor", "patch", "digest"])
def test_db_non_major_is_not_held_for_approval(update_type: str) -> None:
    cfg = _resolve(_dep("postgres", "docker-compose", update_type))
    assert "dependencyDashboardApproval" not in cfg
    assert cfg["groupName"] == "database images"
    assert cfg["minimumReleaseAge"] == "3 days"


def test_other_image_majors_are_not_held_for_approval() -> None:
    cfg = _resolve(_dep("grafana/grafana", "docker-compose", "major"))
    assert "dependencyDashboardApproval" not in cfg
