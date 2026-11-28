"""A green held-deploy bump MERGES; a held-deploy MAJOR and a database major do not -- must fire.

Operator ruling 2026-11-28: "isnt that the point of coded tests to check this shit? they are
green." The hold for a stateful compose image lives in the consumer's DEPLOY (edge-host
runbooks/lib-held-infra.sh: hold -> drain -> deploy -> verify -> release, unattended, one unit
per slot), not in the merge. So every `held-deploy` non-major resolves to automerge true and keeps
its (informational) `held-deploy` label -- the consumer's lane deploys from its own unit list,
edge-host infra_config.py HELD_UNITS, not from the label -- while three holds stay exactly as they were:

  - a windmill MAJOR (the consumer's lane refuses it too: a human upgrade),
  - every other held-deploy image's MAJOR (the default major rule),
  - a database-server major (Dependency Dashboard approval, never merged by Renovate).

Resolved over the WHOLE rule list (test_renovate_floating_digest._resolve), so a rule added
later that re-holds a green bump, or re-arms a major, turns this red.
"""
from __future__ import annotations

import pytest

from test_renovate_floating_digest import _resolve

# image -> the non-major update types it really gets (all compose)
HELD = {
    "langfuse/langfuse": ("patch", "minor", "digest"),
    "langfuse/langfuse-worker": ("patch", "minor", "digest"),
    "clickhouse/clickhouse-server": ("patch", "minor", "digest"),
    "redis": ("patch", "minor", "digest"),
    "ghcr.io/windmill-labs/windmill": ("patch", "minor", "digest"),
    "cgr.dev/chainguard/minio": ("digest",),
    "caddy": ("patch", "minor", "digest"),
}
CASES = [(img, ut) for img, uts in HELD.items() for ut in uts]


def _dep(name: str, manager: str, update: str) -> dict:
    return {"manager": manager, "datasource": "docker", "updateType": update,
            "packageName": name, "repository": "mjdevaccount/trade-infra"}


@pytest.mark.parametrize(("image", "update"), CASES)
def test_a_green_held_deploy_bump_merges_and_keeps_its_label_must_fire(image: str, update: str) -> None:
    cfg = _resolve(_dep(image, "docker-compose", update))
    assert cfg.get("automerge") is True, f"{image} {update}: held at merge again ({cfg.get('automerge')})"
    assert "held-deploy" in cfg.get("labels", []), f"{image} {update}: lost its held-deploy label"


def test_the_windmill_worker_dockerfile_moves_with_the_server() -> None:
    # One group spans both managers; the worker's FROM must merge exactly when the server does.
    for update in ("patch", "minor"):
        cfg = _resolve(_dep("ghcr.io/windmill-labs/windmill", "dockerfile", update))
        assert cfg.get("automerge") is True and cfg.get("groupName") == "windmill"
    assert _resolve(_dep("ghcr.io/windmill-labs/windmill", "dockerfile", "major")).get("automerge") is False


@pytest.mark.parametrize("image", [i for i in HELD if i != "cgr.dev/chainguard/minio"])
def test_a_held_deploy_major_never_merges_must_not_fire(image: str) -> None:
    cfg = _resolve(_dep(image, "docker-compose", "major"))
    assert cfg.get("automerge") is False, f"{image} major would merge unattended"


@pytest.mark.parametrize("image", ["postgres", "timescale/timescaledb-ha"])
def test_a_database_server_major_stays_on_dashboard_approval(image: str) -> None:
    cfg = _resolve(_dep(image, "docker-compose", "major"))
    assert cfg.get("automerge") is False
    assert cfg.get("dependencyDashboardApproval") is True
    assert cfg.get("groupName") == "database server images"
