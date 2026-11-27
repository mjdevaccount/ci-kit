"""A database-image MAJOR waits for Dependency Dashboard approval, by name, and never lands itself.

Measured 2026-11-26: a consumer's "update database images to v19" and edge-platform's postgres
18 -> 19 CI service container sat in "Pending Status Checks" for six days. Docker Hub's
`tag_last_pushed` is the release timestamp and docker-library re-pushes floating tags every few
days, so the 3-day age wait re-armed under each rebuild: a hold nothing named, in the dashboard
section that promises the wait will end. The preset now holds the major under "Pending
Approval" with the gate in the title and the PR body.

Resolved over the WHOLE packageRules list (later match wins, the floating-digest test's
resolver, which models the value and repository matchers and Renovate's mergeable arrays), for
every repository a rule scopes to, so a rule added below -- repo-scoped or not -- that re-arms
automerge or drops the approval turns this red. Both directions: the non-major database updates
must NOT pick up the approval hold, and one approval is one decision (per image, except the
two server images, which are one pg_upgrade).
"""
from __future__ import annotations

import json5
import pytest
from test_renovate_floating_digest import PRESET, _resolve

DB_IMAGES = ["timescale/timescaledb-ha", "postgres", "edoburu/pgbouncer",
             "timescale/pgai-vectorizer-worker", "woblerr/pgbackrest_exporter"]
SERVER = {"timescale/timescaledb-ha", "postgres"}
# docker-compose (langfuse-pg), dockerfile (backup sidecar FROM), github-actions (a CI service container)
MANAGERS = ["docker-compose", "dockerfile", "github-actions"]
_RULES = json5.loads(PRESET.read_text(encoding="utf-8"))["packageRules"]
# None = a consumer no repo-scoped rule names; then every repository any rule is scoped to
REPOS = [None, *sorted({r for rule in _RULES for r in rule.get("matchRepositories", [])})]
TS_NOW = "pg18.7-ts2.32.1-all"  # trade-infra docker-compose.yml, 2026-11-26


def _dep(name: str, manager: str, update_type: str, repo: str | None = None, **values: str) -> dict:
    d = {"manager": manager, "datasource": "docker", "updateType": update_type, "packageName": name, **values}
    return {**d, "repository": repo} if repo else d


@pytest.mark.parametrize("repo", REPOS)
@pytest.mark.parametrize("manager", MANAGERS)
@pytest.mark.parametrize("name", DB_IMAGES)
def test_db_major_waits_for_dashboard_approval(name: str, manager: str, repo: str | None) -> None:
    cfg = _resolve(_dep(name, manager, "major", repo))
    assert cfg.get("dependencyDashboardApproval") is True
    assert cfg.get("automerge") is False  # github-actions majors auto-merge by an earlier rule
    assert cfg.get("minimumReleaseAge") is None  # the wait that re-armed on every re-push


@pytest.mark.parametrize("manager", MANAGERS)
def test_db_major_names_its_gate(manager: str) -> None:
    cfg = _resolve(_dep("postgres", manager, "major"))
    assert "upgrade plan" in cfg["commitMessageSuffix"]  # the dashboard line shows the PR title
    assert "dba" in cfg["labels"]
    notes = " ".join(cfg["prBodyNotes"])  # both rules' notes: prBodyNotes is mergeable
    assert "never merged by Renovate" in notes
    assert "extension" in notes and "upgrade plan" in notes and "backup pg_dump client" in notes


def test_one_approval_is_one_decision() -> None:
    """The server images share ONE pg_upgrade branch; every other db image's major is its own."""
    for name in DB_IMAGES:
        cfg = _resolve(_dep(name, "docker-compose", "major"))
        if name in SERVER:
            assert cfg["groupName"] == "database server images"
            assert "upgrade plan" in cfg["commitMessageSuffix"]
        else:
            assert cfg["groupName"] is None, name  # not "database images": its own branch
            assert "pg_upgrade" not in " ".join(cfg["prBodyNotes"]), name
            assert "upgrade plan" not in cfg["commitMessageSuffix"], name


@pytest.mark.parametrize("repo", REPOS)
def test_timescaledb_extension_major_is_held(repo: str | None) -> None:
    """ts2 -> ts3 is a `patch` under the versioning regex, and an ALTER EXTENSION on the cluster."""
    cfg = _resolve(_dep("timescale/timescaledb-ha", "docker-compose", "patch", repo,
                        currentValue=TS_NOW, newValue="pg18.7-ts3.0.0-all"))
    assert cfg.get("dependencyDashboardApproval") is True
    assert cfg.get("automerge") is False
    assert cfg["groupName"] == "timescaledb extension major"
    assert "ALTER EXTENSION" in cfg["commitMessageSuffix"]


def test_timescaledb_extension_rule_names_the_series_we_run() -> None:
    """The rule can only name one ts major; it must be the one the estate is on."""
    rule = next(r for r in _RULES if r.get("groupName") == "timescaledb extension major")
    cur = rule["matchCurrentValue"]
    assert cur.startswith("/") and f"-ts{TS_NOW.split('-ts')[1].split('.')[0]}" in cur.replace("\\", "")


@pytest.mark.parametrize("new", ["pg18.8-ts2.32.1-all", "pg18.7-ts2.33.0-all"])
def test_timescaledb_minor_is_not_held(new: str) -> None:
    cfg = _resolve(_dep("timescale/timescaledb-ha", "docker-compose", "patch",
                        currentValue=TS_NOW, newValue=new))
    assert "dependencyDashboardApproval" not in cfg
    assert cfg["groupName"] == "database images"


@pytest.mark.parametrize("update_type", ["minor", "patch", "digest"])
def test_db_non_major_is_not_held_for_approval(update_type: str) -> None:
    cfg = _resolve(_dep("postgres", "docker-compose", update_type))
    assert "dependencyDashboardApproval" not in cfg
    assert cfg["groupName"] == "database images"
    assert cfg["minimumReleaseAge"] == "3 days"


def test_other_image_majors_are_not_held_for_approval() -> None:
    cfg = _resolve(_dep("grafana/grafana", "docker-compose", "major"))
    assert "dependencyDashboardApproval" not in cfg


def test_no_later_rule_can_release_a_db_hold() -> None:
    """Belt to the resolver: no rule AFTER the hold, of any scope, re-arms automerge or drops the
    approval for a rule that could match a db image (named, or matching every package)."""
    first = next(i for i, r in enumerate(_RULES) if r.get("dependencyDashboardApproval") is True)
    for rule in _RULES[first:]:
        names = rule.get("matchPackageNames")
        reaches = names is None or any(n in names for n in DB_IMAGES)
        if reaches and (rule.get("automerge") is True or rule.get("dependencyDashboardApproval") is False):
            assert "matchUpdateTypes" in rule and "major" not in rule["matchUpdateTypes"], rule
