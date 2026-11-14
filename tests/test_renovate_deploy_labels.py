"""A compose image's PR label says what merging it DOES -- must fire.

trade-infra's compose is shipped by deploy-reconcile, but WHICH services a merge recreates is
edge-host scripts/infra_config.py's POLICY. The services it never recreates on its own (manual, or
absent = manual) were labelled `deploys-on-merge` (2026-11-14: langfuse stack, chainguard minio,
and through "compose images" caddy, crawl4ai, valkey) -- merged, they changed nothing on the box.
Their image names are pinned here from that POLICY (edge-host origin/main, 2026-11-14); a service
that moves between recreate and manual there changes this list.

The effective labels are computed the way Renovate does it: every matching rule in order, a later
`labels` replacing an earlier one.
"""
from __future__ import annotations

import pathlib

import json5
import pytest

PRESET = pathlib.Path(__file__).resolve().parent.parent / "renovate" / "default.json5"
REPO = "mjdevaccount/trade-infra"
# image -> the infra_config service(s) it runs: none of them may be recreated by a merge
HELD = {
    "langfuse/langfuse": "langfuse-web (unpolicied)",
    "langfuse/langfuse-worker": "langfuse-worker (unpolicied)",
    "clickhouse/clickhouse-server": "langfuse-clickhouse (manual, recreate_ok=False)",
    "redis": "langfuse-redis (unpolicied)",
    "cgr.dev/chainguard/minio": "langfuse-minio, langfuse-minio-ilm (unpolicied)",
    "caddy": "caddy (manual, recreate_ok=False)",
    "unclecode/crawl4ai": "crawl4ai (unpolicied)",
    "valkey/valkey": "valkey (unpolicied)",
}
# a merge DOES recreate these (infra_config POLICY recreate): the label must stay deploys-on-merge
RECREATED = ("grafana/loki", "prom/node-exporter", "searxng/searxng")


def _matches(rule: dict, dep: dict) -> bool:
    checks = {
        "matchRepositories": dep["repo"], "matchManagers": dep["manager"], "matchDatasources": "docker",
        "matchPackageNames": dep["name"], "matchUpdateTypes": dep["type"],
    }
    for key, value in checks.items():
        if key in rule and value not in rule[key]:
            return False
    unknown = [k for k in rule if k.startswith("match") and k not in checks]
    return not unknown  # a matcher this model does not read: treat the rule as not matching


def labels_for(name: str, update: str) -> list[str]:
    dep = {"repo": REPO, "manager": "docker-compose", "name": name, "type": update}
    out: list[str] = []
    for rule in json5.loads(PRESET.read_text(encoding="utf-8"))["packageRules"]:
        if _matches(rule, dep) and "labels" in rule:
            out = rule["labels"]
    return out


@pytest.mark.parametrize("update", ["patch", "minor", "digest"])
@pytest.mark.parametrize("image", sorted(HELD))
def test_a_service_no_merge_recreates_is_labelled_held_deploy_must_fire(image: str, update: str) -> None:
    if image == "cgr.dev/chainguard/minio" and update != "digest":
        pytest.skip("minio is pinned `latest@digest`: digest is its only update")
    got = labels_for(image, update)
    assert "held-deploy" in got and "deploys-on-merge" not in got, f"{image} -> {HELD[image]}: {got}"


@pytest.mark.parametrize("image", RECREATED)
def test_a_service_a_merge_recreates_keeps_deploys_on_merge_must_not_fire(image: str) -> None:
    assert labels_for(image, "minor") == ["deps", "deploys-on-merge"]
