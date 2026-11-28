"""The kit's `supervisor` wiring (ci_kit/runners/supervisor.py): rendered when declared, absent when
not, and every malformed shape refused.

A kit with no `supervisor` must render byte-identically to before (test_runners_must_fire's golden
covers that); a kit with it renders each wired block's resolved paths, limits, caches, registry
cache and `foreign_vmids`. Both directions of every rule run here: the good kit loads, each edit
that breaks one rule is a KitError naming it.
"""

from __future__ import annotations

import copy
import json
import pathlib

import pytest

from ci_kit.runners import KitError, kit_from_dict, render, validate

yaml = pytest.importorskip("yaml")

GOLDEN = pathlib.Path(__file__).parent / "fixtures" / "runners" / "golden"

SUP = {
    "owner": "octo",
    "metric_prefix": "ci_",
    "conf_dir": "/etc/ci",
    "run_dir": "/run/ci",
    "prom_dir": "/var/lib/node-exporter",
    "alloc_jq": "/usr/local/lib/ci/alloc.jq",
    "labels": {"repo": "ci.repo", "keep": "ci.keep", "canary": "ci.canary"},
    "work_root": "/srv/ci",
    "runner_env": {"B_FIRST": "1", "A_SECOND": "2"},
    "caches": {
        "pip": {"dir": "/srv/ci-cache/pip", "max_bytes": 1024, "env": "PIP_CACHE_DIR", "set": {"PIP_X": "y"}},
        "npm": {"dir": "/srv/ci-cache/npm", "max_bytes": 2048, "env": "NPM_CONFIG_CACHE"},
    },
    "registry_cache": {"image": "registry.example/cache:1@sha256:" + "0" * 64, "name": "ci-registry",
                       "addr": "127.0.0.1:5000", "debug_addr": "127.0.0.1:5001", "dir": "/srv/ci-cache/registry",
                       "upstream": "https://registry.example", "ttl": "336h", "max_bytes": 4096,
                       "log_environment": "ci"},
}
SOCKET = {"state_dir": "/etc/ci", "dockerfile": "/usr/local/lib/ci/runner.Dockerfile", "image": "ci-runner-a:local",
          "prom_file": "ci_runner.prom", "busy_limit_s": 46800, "idle_limit_s": 21600, "stray_limit_s": 10800,
          "caches": ["npm", "pip"], "registry_cache": True}
SECURE = {"state_dir": "/etc/ci/b", "dockerfile": "/usr/local/lib/ci/runner-b.Dockerfile", "image": "ci-runner-b:local",
          "prom_file": "ci_runner_b.prom", "busy_limit_s": 1800, "idle_limit_s": 21600, "stray_limit_s": 0,
          "network": "ci-b"}


def _kit_dict() -> dict:
    d = yaml.safe_load((GOLDEN / "kit.yml").read_text(encoding="utf-8"))
    d["supervisor"] = copy.deepcopy(SUP)
    d["blocks"]["runners"]["instance"] = "alpha"
    d["blocks"]["runners"]["supervisor"] = copy.deepcopy(SOCKET)
    d["blocks"]["secure_runners"]["supervisor"] = copy.deepcopy(SECURE)
    return d


def _cfg():
    text = (GOLDEN / "runners.yml").read_text(encoding="utf-8")
    return yaml.safe_load(text), text


def _doc(block: str, d: dict | None = None) -> dict:
    cfg, text = _cfg()
    kit = kit_from_dict(d or _kit_dict())
    assert validate(cfg, kit) == []
    return json.loads(render(cfg, text, block, kit))


def test_a_wired_block_renders_its_resolved_supervisor():
    cfg, _ = _cfg()
    s = _doc("runners")["supervisor"]
    assert s["run_dir"] == "/run/ci/alpha" and s["prom_file"] == "/var/lib/node-exporter/ci_runner.prom"
    assert s["labels"] == {"repo": "ci.repo", "keep": "ci.keep", "canary": "ci.canary"}
    assert (s["busy_limit_s"], s["idle_limit_s"], s["stray_limit_s"]) == (46800, 21600, 10800)
    # mappings keep their DECLARED order (a docker run's -e order is part of what a golden pins)
    assert s["runner_env"] == [{"name": "B_FIRST", "value": "1"}, {"name": "A_SECOND", "value": "2"}]
    # the block's cache order, not the kit's
    assert [c["name"] for c in s["caches"]] == ["npm", "pip"]
    assert s["caches"][1] == {"name": "pip", "dir": "/srv/ci-cache/pip", "max_bytes": 1024, "env": "PIP_CACHE_DIR",
                              "set": [{"name": "PIP_X", "value": "y"}]}
    assert s["registry_cache"]["addr"] == "127.0.0.1:5000" and "network" not in s
    others = sorted(cfg["guests"][cfg[k]["vm"]]["vmid"] for k in ("secure_runners", "sealed_runners"))
    assert s["foreign_vmids"] == others


def test_an_unwired_detail_is_absent_not_defaulted():
    s = _doc("secure_runners")["supervisor"]
    assert s["network"] == "ci-b" and s["caches"] == [] and "registry_cache" not in s
    # the sealed block declares no supervisor: its render carries none
    assert "supervisor" not in _doc("sealed_runners")


def test_the_section_adds_only_the_supervisor_key():
    cfg, text = _cfg()
    d = _kit_dict()
    d.pop("supervisor")
    for b in d["blocks"].values():
        b.pop("supervisor", None)
    plain = kit_from_dict(d)
    before = json.loads(render(cfg, text, "runners", plain))
    after = _doc("runners")
    assert list(after) == [*before, "supervisor"]
    assert {k: v for k, v in after.items() if k != "supervisor"} == before


def _edit(path: str, value=None, *, drop: bool = False):
    def f(d):
        *head, last = path.split(".")
        node = d
        for k in head:
            node = node[k]
        if drop:
            node.pop(last)
        else:
            node[last] = value
    return f


BAD = {
    "unknown top key": _edit("supervisor.nope", 1),
    "missing owner": _edit("supervisor.owner", drop=True),
    "owner shape": _edit("supervisor.owner", "a/b"),
    "prefix without _": _edit("supervisor.metric_prefix", "ci"),
    "relative conf_dir": _edit("supervisor.conf_dir", "etc/ci"),
    "dotdot run_dir": _edit("supervisor.run_dir", "/run/../etc"),
    "labels missing canary": _edit("supervisor.labels", {"repo": "a.b", "keep": "a.c"}),
    "labels collide": _edit("supervisor.labels", {"repo": "a.b", "keep": "a.b", "canary": "a.c"}),
    "label shape": _edit("supervisor.labels.repo", "A B"),
    "env name": _edit("supervisor.runner_env", {"lower": "1"}),
    "env value": _edit("supervisor.runner_env", {"X": 1}),
    "cache missing env": _edit("supervisor.caches.pip.env", drop=True),
    "cache unknown key": _edit("supervisor.caches.pip.size", 1),
    "cache max_bytes": _edit("supervisor.caches.pip.max_bytes", 0),
    "cache dir is work_root": _edit("supervisor.caches.pip.dir", "/srv/ci"),
    "two caches one dir": _edit("supervisor.caches.npm.dir", "/srv/ci-cache/pip"),
    "caches not a mapping": _edit("supervisor.caches", ["pip"]),
    "registry missing ttl": _edit("supervisor.registry_cache.ttl", drop=True),
    "registry addr": _edit("supervisor.registry_cache.addr", "localhost"),
    "registry same ports": _edit("supervisor.registry_cache.debug_addr", "127.0.0.1:5000"),
    "registry http upstream": _edit("supervisor.registry_cache.upstream", "http://registry.example"),
    "registry ttl": _edit("supervisor.registry_cache.ttl", "2 weeks"),
    "block unknown key": _edit("blocks.runners.supervisor.nope", 1),
    "block missing image": _edit("blocks.runners.supervisor.image", drop=True),
    "block relative state_dir": _edit("blocks.runners.supervisor.state_dir", "ci"),
    "block prom_file": _edit("blocks.runners.supervisor.prom_file", "ci_runner.txt"),
    "block negative limit": _edit("blocks.runners.supervisor.busy_limit_s", -1),
    "block bool limit": _edit("blocks.runners.supervisor.idle_limit_s", True),
    "block unknown cache": _edit("blocks.runners.supervisor.caches", ["nope"]),
    "block cache twice": _edit("blocks.runners.supervisor.caches", ["pip", "pip"]),
    "block caches not a list": _edit("blocks.runners.supervisor.caches", "pip"),
    "block network": _edit("blocks.secure_runners.supervisor.network", "a b"),
    "block registry not bool": _edit("blocks.runners.supervisor.registry_cache", "yes"),
    "two blocks one state_dir": _edit("blocks.secure_runners.supervisor.state_dir", "/etc/ci"),
    "two blocks one prom_file": _edit("blocks.secure_runners.supervisor.prom_file", "ci_runner.prom"),
    "two blocks one image": _edit("blocks.secure_runners.supervisor.image", "ci-runner-a:local"),
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_a_malformed_supervisor_section_is_refused(name):
    d = _kit_dict()
    BAD[name](d)
    with pytest.raises(KitError) as e:
        kit_from_dict(d)
    # exactly ONE problem: the edit broke its own rule and nothing else (the good kit is clean)
    assert "; " not in str(e.value), str(e.value)


def test_a_block_section_needs_the_top_level_one():
    d = _kit_dict()
    d.pop("supervisor")
    with pytest.raises(KitError, match="needs the kit's top-level supervisor section"):
        kit_from_dict(d)


def test_a_registry_cache_block_needs_the_kits_registry_cache():
    d = _kit_dict()
    d["supervisor"].pop("registry_cache")
    with pytest.raises(KitError, match="registry_cache needs supervisor.registry_cache"):
        kit_from_dict(d)


def test_the_good_kit_loads_and_each_refusal_names_its_field():
    # the other direction: the unedited kit is clean, so every refusal above is its edit's alone
    kit_from_dict(_kit_dict())
    d = _kit_dict()
    BAD["block unknown cache"](d)
    with pytest.raises(KitError, match=r"blocks\.runners\.supervisor\.caches names 'nope'"):
        kit_from_dict(d)
