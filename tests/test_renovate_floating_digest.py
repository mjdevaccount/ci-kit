"""cgr.dev/chainguard/minio:latest digest updates are releasable, in their own PR.

Renovate reads release timestamps from Docker Hub only and ages a digest update against the
matched version, so an unversioned `latest` on cgr.dev is held "indefinitely" under the default
minimumReleaseAgeBehaviour (docs.renovatebot.com/key-concepts/minimum-release-age/). A consumer's
two langfuse-minio pins sat behind for 25 days with no PR (its dependency dashboard). The preset
replaces the age gate for that one package: no age wait, its own weekend-window PR, and (since
2026-11-28) a merge on green that the consumer's held deploy recreates under a drain.

packageRules apply in order and a later match wins, so this resolves the WHOLE list for the
update rather than reading one rule: a rule added below it that re-arms automerge or the age
wait for docker digests turns this red.
"""
from __future__ import annotations

import fnmatch
import functools
import pathlib
import re

import json5

PRESET = pathlib.Path(__file__).resolve().parent.parent / "renovate" / "default.json5"
MATCHERS = {"matchManagers", "matchDatasources", "matchPackageNames", "matchUpdateTypes",
            "matchCurrentValue", "matchNewValue", "matchSourceUrls", "matchRepositories",
            "matchDepTypes", "matchFileNames"}
# Renovate CONCATENATES these down the rule list (`mergeable: true`, lib/config/options/index.ts)
MERGEABLE = {"prBodyNotes", "addLabels"}
# value matchers -> the dep field they read; a dep WITHOUT the field fails the rule, as in Renovate
# (lib/util/package-rules/*.ts: `if (!currentValue) return false`)
VALUE_FIELDS = {"matchCurrentValue": "currentValue", "matchNewValue": "newValue",
                "matchRepositories": "repository", "matchSourceUrls": "sourceUrl",
                "matchDepTypes": "depType", "matchFileNames": "packageFile"}


def _one(p: str, value: str) -> bool:
    """Renovate getRegexOrGlobPredicate: `/re/` or `/re/i`, else a glob; a leading `!` negates."""
    neg = p.startswith("!")
    body = p[1:] if neg else p
    if body.startswith("/") and body.rstrip("i").endswith("/") and len(body.rstrip("i")) > 1:
        flags = re.I if body.endswith("/i") else 0
        return bool(re.search(body[1:body.rstrip("i").rfind("/")], value, flags)) != neg
    return fnmatch.fnmatchcase(value, body.replace("**", "*")) != neg


def _name_matches(patterns: list[str] | str, name: str) -> bool:
    """Renovate matchRegexOrGlobList: some positive matches (if any), and no negative does."""
    pats = [patterns] if isinstance(patterns, str) else patterns
    pos = [p for p in pats if not p.startswith("!")]
    if pos and not any(_one(p, name) for p in pos):
        return False
    return all(_one(p, name) for p in pats if p.startswith("!"))


@functools.lru_cache(maxsize=1)
def _preset() -> dict:
    return json5.loads(PRESET.read_text(encoding="utf-8"))


def _resolve(dep: dict) -> dict:
    preset = _preset()
    cfg = {k: preset[k] for k in ("minimumReleaseAge", "schedule") if k in preset}
    for rule in preset["packageRules"]:
        if "matchManagers" in rule and dep["manager"] not in rule["matchManagers"]:
            continue
        if "matchDatasources" in rule and dep["datasource"] not in rule["matchDatasources"]:
            continue
        if "matchUpdateTypes" in rule and dep["updateType"] not in rule["matchUpdateTypes"]:
            continue
        if "matchPackageNames" in rule and not _name_matches(rule["matchPackageNames"], dep["packageName"]):
            continue
        if any(m in rule and not (dep.get(f) and _name_matches(rule[m], dep[f])) for m, f in VALUE_FIELDS.items()):
            continue
        for k, v in rule.items():
            if k in MATCHERS or k == "description":
                continue
            cfg[k] = [*cfg.get(k, []), *v] if k in MERGEABLE else v
    return cfg


MINIO = {"manager": "docker-compose", "datasource": "docker", "updateType": "digest",
         "packageName": "cgr.dev/chainguard/minio"}


def test_minio_digest_has_no_age_wait() -> None:
    assert _resolve(MINIO).get("minimumReleaseAge") is None


def test_minio_digest_merges_on_green_in_its_own_group() -> None:
    # Since 2026-11-28 the hold lives in the consumer's DEPLOY (a held deploy recreates minio under
    # a drain), so a green digest merges; its own group and window are what stay.
    cfg = _resolve(MINIO)
    assert cfg.get("automerge") is True
    assert "held-deploy" in cfg.get("labels", [])
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
