"""A docker image on a registry with no release timestamps is releasable, and says so quietly.

Renovate's docker datasource reads release timestamps from Docker Hub ONLY
(docs/usage/key-concepts/minimum-release-age.md, "Which registries support release timestamps?").
Under the default minimumReleaseAgeBehaviour (timestamp-required) every other registry's update
is held forever -- and inside a group with a releasable member it is DROPPED from the branch
(lib/workers/repository/updates/generate.ts), so it is not even listed as pending. The preset
sets timestamp-optional for every non-Docker-Hub registry, and demotes the WARN Renovate logs
for that choice, which would otherwise sit in every such repo's Repository Problems forever.

Both directions: the registries that cannot be aged are released, Docker Hub (and every other
datasource) keeps the age gate, and the remap silences the one message and no other WARN.
"""
from __future__ import annotations

import fnmatch
import pathlib
import re

import json5

PRESET = pathlib.Path(__file__).resolve().parent.parent / "renovate" / "default.json5"

# Verbatim from Renovate's source (lib/workers/repository/process/lookup/filter-checks.ts and
# lib/workers/repository/update/branch/status-checks.ts, main 2026-11-26). THIS PIN IS A HAND
# RE-CHECK, not a live one: the test reads its own copy, so it stays green if upstream rewords a
# message. That fails SAFE -- the remap stops matching and the WARN reappears on the dashboard
# (visible, never hidden) -- and the fix is to re-read those two files and re-pin here.
WARN_LOOKUP = ("Some release(s) did not have a releaseTimestamp, but as we're running with "
               "minimumReleaseAgeBehaviour=timestamp-optional, proceeding. See debug logs for "
               "more information")
WARN_BRANCH = ("Some upgrade(s) did not have a releaseTimestamp, but as we're running with "
               "minimumReleaseAgeBehaviour=timestamp-optional, proceeding. See debug logs for "
               "more information")
# The timestamp-REQUIRED hold and the WARNs that mean something must NOT be remapped.
NOT_REMAPPED = [
    "Marking 1 release(s) as pending, as they do not have a releaseTimestamp and we're running "
    "with minimumReleaseAgeBehaviour=timestamp-required",
    "pip-compile error",
    "Hidden Unicode characters have been discovered in file(s) in your repository.",
]

# Images the estate actually pins (consumer dependency dashboards, 2026-11-26).
NO_TIMESTAMP = [
    "ghcr.io/windmill-labs/windmill",
    "ghcr.io/actions/actions-runner",
    "quay.io/prometheus/blackbox-exporter",
    "quay.io/prometheuscommunity/postgres-exporter",
    "lscr.io/linuxserver/speedtest-tracker",
    "registry.k8s.io/pause",
    "mcr.microsoft.com/dotnet/aspnet",
    "cgr.dev/chainguard/minio",
    "localhost:5000/team/app",
    "localhost/app",
]
DOCKER_HUB = [
    "redis",
    "prom/alertmanager",
    "clickhouse/clickhouse-server",
    "docker.io/library/redis",
    "index.docker.io/grafana/grafana",
    "registry-1.docker.io/library/postgres",
]


def _js_regex(p: str) -> tuple[str, int] | None:
    if p.startswith("/") and p.rstrip("i").endswith("/") and len(p.rstrip("i")) > 1:
        body = p[1:p.rstrip("i").rfind("/")]
        return body, re.I if p.endswith("/i") else 0
    return None


def _one(p: str, name: str) -> bool:
    rx = _js_regex(p)
    if rx:
        return re.search(rx[0], name, rx[1]) is not None
    return fnmatch.fnmatchcase(name, p.replace("**", "*"))


def _names_match(patterns: list[str], name: str) -> bool:
    """Renovate's matchRegexOrGlobList: any positive matches AND every `!` negation holds."""
    pos = [p for p in patterns if not p.startswith("!")]
    neg = [p[1:] for p in patterns if p.startswith("!")]
    if pos and not any(_one(p, name) for p in pos):
        return False
    return not any(_one(p, name) for p in neg)


def _behaviour(datasource: str, name: str) -> str:
    preset = json5.loads(PRESET.read_text(encoding="utf-8"))
    out = "timestamp-required"  # Renovate's default since 42
    for rule in preset["packageRules"]:
        if "minimumReleaseAgeBehaviour" not in rule:
            continue
        if "matchDatasources" in rule and datasource not in rule["matchDatasources"]:
            continue
        if "matchPackageNames" in rule and not _names_match(rule["matchPackageNames"], name):
            continue
        out = rule["minimumReleaseAgeBehaviour"]
    return out


def _remapped(msg: str) -> str | None:
    preset = json5.loads(PRESET.read_text(encoding="utf-8"))
    for remap in preset.get("logLevelRemap", []):
        rx = _js_regex(remap["matchMessage"])
        assert rx, f"matchMessage must be a /regex/: {remap['matchMessage']}"
        if re.search(rx[0], msg, rx[1]):
            return remap["newLogLevel"]
    return None


def test_every_non_docker_hub_registry_is_releasable() -> None:
    held = [n for n in NO_TIMESTAMP if _behaviour("docker", n) != "timestamp-optional"]
    assert not held, f"held forever (no registry timestamp, timestamp-required): {held}"


def test_docker_hub_keeps_the_age_gate() -> None:
    lifted = [n for n in DOCKER_HUB if _behaviour("docker", n) != "timestamp-required"]
    assert not lifted, f"Docker Hub publishes timestamps; the age gate must stay: {lifted}"
    preset = json5.loads(PRESET.read_text(encoding="utf-8"))
    assert preset["minimumReleaseAge"] == "3 days"


def test_other_datasources_keep_the_age_gate() -> None:
    # a dotted PyPI / npm name is not a registry host: the rule is docker-only
    for ds, name in [("pypi", "zope.interface"), ("npm", "socket.io"),
                     ("github-tags", "actions/checkout"), ("terraform-provider", "bpg/proxmox")]:
        assert _behaviour(ds, name) == "timestamp-required", (ds, name)


def test_the_timestamp_optional_warn_is_demoted() -> None:
    assert _remapped(WARN_LOOKUP) == "info"
    assert _remapped(WARN_BRANCH) == "info"


def test_no_other_message_is_remapped() -> None:
    hit = [m for m in NOT_REMAPPED if _remapped(m) is not None]
    assert not hit, f"remap reaches a message that must stay visible: {hit}"


def _versioning(datasource: str, name: str) -> str:
    preset = json5.loads(PRESET.read_text(encoding="utf-8"))
    out = "docker" if datasource == "docker" else "default"
    for rule in preset["packageRules"]:
        if "versioning" not in rule:
            continue
        if "matchDatasources" in rule and datasource not in rule["matchDatasources"]:
            continue
        if "matchPackageNames" in rule and not _names_match(rule["matchPackageNames"], name):
            continue
        out = rule["versioning"]
    return out


def _regex_key(versioning: str, tag: str) -> tuple[int, ...] | None:
    assert versioning.startswith("regex:"), versioning
    m = re.match(versioning[len("regex:"):].replace("(?<", "(?P<"), tag)
    if not m:
        return None
    g = m.groupdict()
    return tuple(int(g.get(k) or 0) for k in ("major", "minor", "patch", "build"))


def test_linuxserver_ls_suffix_is_a_build_number_not_a_compatibility() -> None:
    # docker versioning treats `-ls178` as a compatibility suffix, so `-ls179` is never proposed
    for name in ["lscr.io/linuxserver/speedtest-tracker", "ghcr.io/linuxserver/tautulli",
                 "linuxserver/plex"]:
        v = _versioning("docker", name)
        cur, new_build, new_patch = (_regex_key(v, t) for t in
                                     ("v1.15.1-ls178", "v1.15.1-ls179", "v1.15.2-ls180"))
        assert cur and new_build and new_patch, (name, v)
        assert cur < new_build < new_patch, (name, cur, new_build, new_patch)
    # and the rule reaches no other image
    assert _versioning("docker", "quay.io/prometheus/blackbox-exporter") == "docker"
