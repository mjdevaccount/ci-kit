"""The kit's SUPERVISOR wiring: what a supervisor reads from its render besides pools and budgets.

A runner supervisor (a host-side script, one instance per block) needs more than sizes: where its
state, lock and metrics live, the GitHub account its runners register under, the container labels
it owns, how long a runner may stay busy or idle, the shared caches it mounts into runners and an
optional pull-through registry cache on the runner VM's daemon. A script that hard-codes them is
one domain's script; one that reads them from its render is the library's. They are DECLARED in the
kit, never the capacity file: a wiring change must not move `source_sha256` (kit.py).

    supervisor:                       # optional: a kit without it renders exactly what it did before
      owner: octo                     # the GitHub account (or org) the runners register under
      metric_prefix: ci_              # every published series starts with it
      conf_dir: /etc/ci               # host config: the API token, the App key
      run_dir: /run/ci                # volatile state; each instance gets <run_dir>/<instance>
      prom_dir: /var/lib/prometheus/node-exporter
      alloc_jq: /usr/local/lib/ci/alloc.jq   # where the allocator is INSTALLED on the host
      labels: {repo: ci.repo, keep: ci.keep, canary: ci.canary}
      work_root: /srv/ci              # each runner's work folder, at the same path in VM and container
      runner_env: {GIT_HTTP_LOW_SPEED_LIMIT: "1000"}  # set in every runner, in this order
      caches:                         # bounded shared directories, mounted at the same path
        pip: {dir: /srv/ci-cache/pip, max_bytes: 1073741824, env: PIP_CACHE_DIR, set: {}}
      registry_cache:                 # optional: a pull-through cache on the runner VM's daemon
        image: ghcr.io/distribution/distribution:3.1.2
        name: ci-registry
        addr: 127.0.0.1:5000
        debug_addr: 127.0.0.1:5001
        dir: /srv/ci-cache/registry
        upstream: https://registry-1.docker.io
        ttl: 336h
        max_bytes: 10737418240
        log_environment: ci
    blocks:
      runners:
        ...
        supervisor:                   # this instance's own wiring (needs the top-level section)
          state_dir: /etc/ci          # persistent per-instance state (pinned image ID, counters)
          dockerfile: /usr/local/lib/ci/runner.Dockerfile
          image: ci-runner-shared:local
          prom_file: ci_runner.prom   # under prom_dir
          busy_limit_s: 46800         # a BUSY runner older than this is reaped
          idle_limit_s: 21600         # an IDLE one
          stray_limit_s: 10800        # a container without the repo label (0 = none may run)
          network: ci-secure          # optional: the docker network its runners join
          caches: [pip]               # optional: which of the kit's caches it mounts, in order
          registry_cache: true        # optional: it runs the kit's registry cache

The render carries the result under `supervisor`, every path resolved, plus `foreign_vmids`: the VM
ids of every OTHER block in the capacity file. An instance that finds its own runner VM among them
is driving another tier's VM and must refuse. schema.py already refuses two blocks on one guest at
check time; this carries the same rule, from the same file, to run time.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ci_kit.runners._checks import _nonneg_int, _pos, _unknown

SUPERVISOR_KEYS = {"owner", "metric_prefix", "conf_dir", "run_dir", "prom_dir", "alloc_jq", "labels",
                   "work_root", "runner_env", "caches", "registry_cache"}
_REQUIRED = SUPERVISOR_KEYS - {"runner_env", "caches", "registry_cache"}
BLOCK_SUPERVISOR_KEYS = {"state_dir", "dockerfile", "image", "prom_file", "busy_limit_s", "idle_limit_s",
                         "stray_limit_s", "network", "caches", "registry_cache"}
_BLOCK_REQUIRED = BLOCK_SUPERVISOR_KEYS - {"network", "caches", "registry_cache"}
LABEL_KEYS = ("repo", "keep", "canary")
CACHE_KEYS = {"dir", "max_bytes", "env", "set"}
REGISTRY_KEYS = ("image", "name", "addr", "debug_addr", "dir", "upstream", "ttl", "max_bytes", "log_environment")

_PATH = re.compile(r"^/(?:[A-Za-z0-9._-]+/?)*$")
_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
_PREFIX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*_$")
_DOCKER_LABEL = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ENV = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_PROM = re.compile(r"^[A-Za-z0-9_.-]+\.prom$")
_ADDR = re.compile(r"^[A-Za-z0-9.-]+:[0-9]{1,5}$")
_TTL = re.compile(r"^[0-9]+[smh]$")
_IMAGE = re.compile(r"^\S+$")


@dataclass(frozen=True)
class Cache:
    """A bounded shared directory: mounted at `dir` in every runner that names it, `env` set to that
    path and each `set` pair after it; wiped past `max_bytes` while no runner is busy."""

    name: str
    dir: str
    max_bytes: int
    env: str
    set: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RegistryCache:
    """A pull-through registry cache on the runner VM's daemon (its `registry-mirrors` points here)."""

    image: str
    name: str
    addr: str
    debug_addr: str
    dir: str
    upstream: str
    ttl: str
    max_bytes: int
    log_environment: str


@dataclass(frozen=True)
class Supervisor:
    """The kit's top-level `supervisor` section: wiring every instance shares."""

    owner: str
    metric_prefix: str
    conf_dir: str
    run_dir: str
    prom_dir: str
    alloc_jq: str
    labels: Mapping[str, str]
    work_root: str
    runner_env: Mapping[str, str] = field(default_factory=dict)
    caches: tuple[Cache, ...] = ()
    registry_cache: RegistryCache | None = None


@dataclass(frozen=True)
class BlockSupervisor:
    """One block's `supervisor`: the wiring its instance does not share with the others."""

    state_dir: str
    dockerfile: str
    image: str
    prom_file: str
    busy_limit_s: int
    idle_limit_s: int
    stray_limit_s: int
    network: str | None = None
    caches: tuple[str, ...] = ()
    registry_cache: bool = False


def _ok(x: Any, rx: re.Pattern) -> bool:
    return isinstance(x, str) and bool(rx.match(x)) and "/../" not in x + "/"


def _env_problems(where: str, env: Any) -> list[str]:
    if not isinstance(env, Mapping):
        return [f"{where} must be a mapping"]
    return [f"{where}.{k} must be a {_ENV.pattern} name with a string value"
            for k, v in env.items() if not (_ok(k, _ENV) and isinstance(v, str))]


def supervisor_problems(s: Supervisor) -> list[str]:
    """Every way the top-level section is malformed."""
    checks = [("owner", s.owner, _OWNER, f"must match {_OWNER.pattern}"),
              ("metric_prefix", s.metric_prefix, _PREFIX, f"must match {_PREFIX.pattern}")]
    checks += [(k, getattr(s, k), _PATH, "must be an absolute path")
               for k in ("conf_dir", "run_dir", "prom_dir", "alloc_jq", "work_root")]
    p = [f"supervisor.{k} {why}" for k, v, rx, why in checks if not _ok(v, rx)]
    if not isinstance(s.labels, Mapping) or set(s.labels) != set(LABEL_KEYS):
        p.append(f"supervisor.labels must name exactly {', '.join(LABEL_KEYS)}")
    else:
        p += [f"supervisor.labels.{k} must be a docker label key" for k in LABEL_KEYS
              if not _ok(s.labels[k], _DOCKER_LABEL)]
        if len(set(s.labels.values())) != len(LABEL_KEYS):
            p.append("supervisor.labels: repo, keep and canary need three different labels")
    p += _env_problems("supervisor.runner_env", s.runner_env)
    for c in s.caches:
        w = f"supervisor.caches.{c.name}"
        p += [f"{w}{k} {why}" for k, ok, why in (
            ("", _ok(c.name, _NAME), f"needs a name matching {_NAME.pattern}"),
            (".dir", _ok(c.dir, _PATH), "must be an absolute path"),
            (".max_bytes", _pos(c.max_bytes), "must be a positive integer"),
            (".env", _ok(c.env, _ENV), f"must match {_ENV.pattern}")) if not ok]
        p += _env_problems(f"{w}.set", c.set)
    r = s.registry_cache
    dirs = [c.dir for c in s.caches] + ([r.dir] if r is not None else [])
    if len(set(dirs)) != len(dirs) or s.work_root in dirs:
        p.append("supervisor: every cache needs its own dir, apart from work_root")
    if r is not None:
        w = "supervisor.registry_cache"
        p += [f"{w}.{k} {why}" for k, ok, why in (
            ("addr", _ok(r.addr, _ADDR), "must be host:port"),
            ("debug_addr", _ok(r.debug_addr, _ADDR), "must be host:port"),
            ("image", _ok(r.image, _IMAGE), "must be an image reference"),
            ("name", _ok(r.name, _NAME), f"must match {_NAME.pattern}"),
            ("dir", _ok(r.dir, _PATH), "must be an absolute path"),
            ("upstream", isinstance(r.upstream, str) and r.upstream.startswith("https://"), "must be an https:// URL"),
            ("ttl", _ok(r.ttl, _TTL), "must be a duration like 336h"),
            ("max_bytes", _pos(r.max_bytes), "must be a positive integer"),
            ("log_environment", _ok(r.log_environment, _NAME), f"must match {_NAME.pattern}")) if not ok]
        if r.addr == r.debug_addr:
            p.append(f"{w}: addr and debug_addr must differ")
    return p


def block_problems(key: str, b: BlockSupervisor, s: Supervisor | None) -> list[str]:
    """Every way one block's section is malformed, or names what the top-level one lacks."""
    w = f"blocks.{key}.supervisor"
    if s is None:
        return [f"{w} needs the kit's top-level supervisor section"]
    p = [f"{w}.{k} {why}" for k, ok, why in (
        ("state_dir", _ok(b.state_dir, _PATH), "must be an absolute path"),
        ("dockerfile", _ok(b.dockerfile, _PATH), "must be an absolute path"),
        ("image", _ok(b.image, _IMAGE), "must be an image reference"),
        ("prom_file", _ok(b.prom_file, _PROM), f"must match {_PROM.pattern}"),
        ("busy_limit_s", _nonneg_int(b.busy_limit_s), "must be a non-negative integer"),
        ("idle_limit_s", _nonneg_int(b.idle_limit_s), "must be a non-negative integer"),
        ("stray_limit_s", _nonneg_int(b.stray_limit_s), "must be a non-negative integer"),
        ("network", b.network is None or _ok(b.network, _NAME), f"must match {_NAME.pattern}"),
        ("registry_cache", isinstance(b.registry_cache, bool), "must be true or false")) if not ok]
    if not isinstance(b.caches, tuple) or not all(isinstance(c, str) for c in b.caches):
        return p + [f"{w}.caches must be a list of cache names"]
    known = {c.name for c in s.caches}
    p += [f"{w}.caches names {c!r}, which supervisor.caches does not declare" for c in b.caches if c not in known]
    if len(set(b.caches)) != len(b.caches):
        p.append(f"{w}.caches names a cache twice")
    if b.registry_cache is True and s.registry_cache is None:
        p.append(f"{w}.registry_cache needs supervisor.registry_cache")
    return p


def supervisor_from_dict(d: Any) -> tuple[Supervisor | None, list[str]]:
    """The top-level section from its parsed form: (Supervisor, []) or (None, problems)."""
    p = _unknown("supervisor", d, SUPERVISOR_KEYS)
    if p:
        return None, p
    p = [f"supervisor: missing {k!r}" for k in sorted(_REQUIRED - set(d))]
    caches_in = d.get("caches") or {}
    if not isinstance(caches_in, dict):
        p.append("supervisor.caches: must be a mapping")
        caches_in = {}
    for name, c in caches_in.items():
        p += _unknown(f"supervisor.caches.{name}", c, CACHE_KEYS)
        if isinstance(c, dict):
            p += [f"supervisor.caches.{name}: missing {k!r}" for k in sorted(CACHE_KEYS - {"set"} - set(c))]
    reg = d.get("registry_cache")
    if reg is not None:
        p += _unknown("supervisor.registry_cache", reg, set(REGISTRY_KEYS))
        if isinstance(reg, dict):
            p += [f"supervisor.registry_cache: missing {k!r}" for k in REGISTRY_KEYS if k not in reg]
    if p:
        return None, p
    caches = tuple(Cache(name=str(n), dir=c["dir"], max_bytes=c["max_bytes"], env=c["env"],
                         set=dict(c.get("set") or {})) for n, c in caches_in.items())
    return Supervisor(**{k: d[k] for k in sorted(_REQUIRED)}, runner_env=dict(d.get("runner_env") or {}),
                      caches=caches, registry_cache=RegistryCache(**reg) if reg is not None else None), []


def block_supervisor_from_dict(key: str, d: Any) -> tuple[BlockSupervisor | None, list[str]]:
    """One block's section from its parsed form: (BlockSupervisor, []) or (None, problems)."""
    w = f"blocks.{key}.supervisor"
    p = _unknown(w, d, BLOCK_SUPERVISOR_KEYS)
    if p:
        return None, p
    p = [f"{w}: missing {k!r}" for k in sorted(_BLOCK_REQUIRED - set(d))]
    if "caches" in d and not isinstance(d["caches"], list):
        p.append(f"{w}.caches must be a list of cache names")
    if p:
        return None, p
    return BlockSupervisor(**{**d, "caches": tuple(d.get("caches") or ())}), []


def supervisor_doc(s: Supervisor, b: BlockSupervisor, instance: str, foreign_vmids: list[int]) -> dict:
    """What one instance reads under `supervisor` in its render: every path resolved. Mappings are
    rendered as [{name, value}] lists so their ORDER reaches a jq reader as declared."""
    by_name = {c.name: c for c in s.caches}

    def pairs(m: Mapping[str, str]) -> list[dict]:
        return [{"name": k, "value": v} for k, v in m.items()]

    return {
        "owner": s.owner,
        "metric_prefix": s.metric_prefix,
        "conf_dir": s.conf_dir,
        "state_dir": b.state_dir,
        "run_dir": f"{s.run_dir.rstrip('/')}/{instance}",
        "prom_file": f"{s.prom_dir.rstrip('/')}/{b.prom_file}",
        "alloc_jq": s.alloc_jq,
        "dockerfile": b.dockerfile,
        "image": b.image,
        "labels": {k: s.labels[k] for k in LABEL_KEYS},
        "work_root": s.work_root,
        "busy_limit_s": b.busy_limit_s,
        "idle_limit_s": b.idle_limit_s,
        "stray_limit_s": b.stray_limit_s,
        **({"network": b.network} if b.network is not None else {}),
        "runner_env": pairs(s.runner_env),
        "caches": [{"name": c, "dir": by_name[c].dir, "max_bytes": by_name[c].max_bytes, "env": by_name[c].env,
                    "set": pairs(by_name[c].set)} for c in b.caches],
        **({"registry_cache": {k: getattr(s.registry_cache, k) for k in REGISTRY_KEYS}}
           if b.registry_cache and s.registry_cache is not None else {}),
        "foreign_vmids": sorted(foreign_vmids),
    }
