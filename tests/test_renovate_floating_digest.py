"""cgr.dev/chainguard/minio:latest digest updates are releasable, and never land unattended.

Renovate reads release timestamps from Docker Hub only and ages a digest update against the
matched version, so an unversioned `latest` on cgr.dev is held "indefinitely" under the default
minimumReleaseAgeBehaviour (docs.renovatebot.com/key-concepts/minimum-release-age/). trade-infra's
two langfuse-minio pins sat behind for 25 days with no PR (dashboard trade-infra#515). The preset
replaces the age gate for that one package with review: no age wait, no auto-merge.

packageRules apply in order and a later match wins, so this resolves the WHOLE list for the
update rather than reading one rule: a rule added below it that re-arms automerge or the age
wait for docker digests turns this red.
"""
from __future__ import annotations

import fnmatch
import pathlib
import re

import json5

PRESET = pathlib.Path(__file__).resolve().parent.parent / "renovate" / "default.json5"
MATCHERS = {"matchManagers", "matchDatasources", "matchPackageNames", "matchUpdateTypes",
            "matchCurrentValue", "matchSourceUrls", "matchRepositories"}


def _name_matches(patterns: list[str], name: str) -> bool:
    for p in patterns:
        if p.startswith("/") and p.rstrip("i").endswith("/"):
            flags = re.I if p.endswith("/i") else 0
            if re.search(p[1:p.rstrip("i").rfind("/")], name, flags):
                return True
        elif fnmatch.fnmatchcase(name, p.replace("**", "*")):
            return True
    return False


def _resolve(dep: dict) -> dict:
    preset = json5.loads(PRESET.read_text(encoding="utf-8"))
    cfg = {k: preset[k] for k in ("minimumReleaseAge", "schedule") if k in preset}
    for rule in preset["packageRules"]:
        if "matchSourceUrls" in rule or "matchRepositories" in rule or "matchCurrentValue" in rule:
            continue  # a docker image on cgr.dev has none of these here
        if "matchManagers" in rule and dep["manager"] not in rule["matchManagers"]:
            continue
        if "matchDatasources" in rule and dep["datasource"] not in rule["matchDatasources"]:
            continue
        if "matchUpdateTypes" in rule and dep["updateType"] not in rule["matchUpdateTypes"]:
            continue
        if "matchPackageNames" in rule and not _name_matches(rule["matchPackageNames"], dep["packageName"]):
            continue
        cfg.update({k: v for k, v in rule.items() if k not in MATCHERS and k != "description"})
    return cfg


MINIO = {"manager": "docker-compose", "datasource": "docker", "updateType": "digest",
         "packageName": "cgr.dev/chainguard/minio"}


def test_minio_digest_has_no_age_wait() -> None:
    assert _resolve(MINIO).get("minimumReleaseAge") is None


def test_minio_digest_never_auto_merges() -> None:
    cfg = _resolve(MINIO)
    assert cfg.get("automerge") is False
    assert cfg.get("groupName") == "chainguard minio"  # not "compose images": would un-automerge it
    assert cfg.get("updateNotScheduled") is False


def test_the_database_group_is_untouched() -> None:
    pg = dict(MINIO, packageName="postgres")
    cfg = _resolve(pg)
    assert cfg["groupName"] == "database images"
    assert cfg["automerge"] is False
    assert cfg["minimumReleaseAge"] == "3 days"


def test_other_compose_digests_keep_the_age_gate() -> None:
    cfg = _resolve(dict(MINIO, packageName="prom/alertmanager"))
    assert cfg["minimumReleaseAge"] == "3 days"
    assert cfg["automerge"] is True
