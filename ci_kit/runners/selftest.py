"""Both directions, on built inputs: every accepted shape renders as expected (ACCEPTS) and every
broken shape is refused (REFUSALS). `ci-runners --selftest` runs them all; the unit suite
parametrises over the same two tables, so each case is also its own test.

The inputs use DEMO_KIT, a three-block kit -- a shared docker-socket pool, a secure token-only
pool, and a sealed one-job-per-boot pool -- each on its own guest.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable

from ci_kit.runners.kit import Block, Kit
from ci_kit.runners.render import render
from ci_kit.runners.schema import validate

DEMO_KIT = Kit(blocks=(
    Block("runners", "shared", "ci-", "socket", "ci", "CI_RUNNER", "runners.json", required=True),
    Block("secure_runners", "secure", "tok-", "secure", "ci-secure", "CI_SECURE_RUNNER", "runners-secure.json"),
    Block("sealed_runners", "sealed", "boot-", "sealed", "ci-sealed", "CI_SEALED_RUNNER", "runners-sealed.json",
          one_job_per_boot=True),
))


def good() -> dict:
    return {
        "host": {"name": "h", "threads": 8, "ram_mb": 16384, "cpu_overcommit": 1.0,
                 "reserves_mb": {"hypervisor": 2048}},
        "guests": {"obs": {"vmid": 900, "cores": 2, "ram_mb": 4096},
                   "ci": {"vmid": 910, "cores": 4, "ram_mb": 8192},
                   "later": {"vmid": 920, "cores": 2, "ram_mb": 2048, "planned": True}},
        "runners": {"vm": "ci", "max_total": 4, "cpu_overcommit": 2.0, "memory_overcommit": 1.0,
                    "vm_reserve_mb": 1024,
                    "defaults": {"cpus": 2, "memory_mb": 2048, "pids": 1024, "labels": ["self-hosted", "ci"]},
                    # real budget 8192 - 1024 = 7168: a's ceiling 3 x 2048 = 6144 fits alone, the
                    # floors 1 x 2048 + 2 x 1024 = 4096 fit; the rest is runtime admission
                    "pools": {"a": {"enabled": True, "min_idle": 1, "max_runners": 3, "peak_mb": 2048},
                              "b": {"enabled": True, "min_idle": 2, "max_runners": 2, "peak_mb": 1024},
                              "off": {"enabled": False, "min_idle": 9, "max_runners": 9}}},
    }


def good_secure() -> dict:
    """good() plus a secure block on its own guest (the planned one, now built)."""
    cfg = good()
    cfg["guests"]["later"].pop("planned")
    cfg["secure_runners"] = {
        "vm": "later", "max_total": 2, "cpu_overcommit": 1.0, "memory_overcommit": 1.0,
        "vm_reserve_mb": 512,
        "defaults": {"cpus": 1, "memory_mb": 768, "pids": 512, "labels": ["self-hosted", "ci-secure"]},
        "pools": {"a": {"enabled": True, "min_idle": 0, "max_runners": 1, "peak_mb": 512}}}
    return cfg


def good_sealed() -> dict:
    """good_secure() plus a sealed block on a third, on_demand guest."""
    cfg = good_secure()
    cfg["host"].update(threads=10, ram_mb=20480)  # room for the third guest
    cfg["guests"]["sealed"] = {"vmid": 912, "cores": 1, "ram_mb": 1024, "state": "on_demand"}
    cfg["sealed_runners"] = {
        "vm": "sealed", "max_total": 1, "cpu_overcommit": 1.0, "memory_overcommit": 1.0,
        "vm_reserve_mb": 256,
        "defaults": {"cpus": 1, "memory_mb": 768, "pids": 1024, "labels": ["self-hosted", "ci-sealed"]},
        "pools": {"a": {"enabled": True, "min_idle": 0, "max_runners": 1, "peak_mb": 512}}}
    return cfg


def shared_reserve() -> dict:
    """good() plus an on_demand guest that runs INSIDE a host reserve (RAM counted once)."""
    cfg = good()
    # host RAM grows by the reserve ONLY: the guest fits only if it is not summed beside it
    cfg["host"].update(ram_mb=16384 + 1024, cpu_overcommit=1.125)
    cfg["host"]["reserves_mb"]["drill"] = 1024
    cfg["guests"]["seal"] = {"vmid": 912, "cores": 1, "ram_mb": 1024, "state": "on_demand", "shares": "drill"}
    return cfg


def _with(base: Callable[[], dict], edit: Callable[[dict], object]) -> dict:
    cfg = base()
    edit(cfg)
    return cfg


def _out(cfg: dict, block: str = "runners", text: str = "x") -> dict:
    return json.loads(render(cfg, text, block, DEMO_KIT))


def _pools(cfg: dict, key: str, block: str = "runners") -> dict:
    return {p["repo"]: p[key] for p in _out(cfg, block)["pools"]}


def _accepts(cfg: dict, expect: Callable[[], bool], what: str) -> list[str]:
    """[] when cfg is valid AND expect() holds on its render; else one failure naming `what`."""
    bad = validate(cfg, DEMO_KIT)
    if bad:
        return [f"{what}: rejected: {bad}"]
    return [] if expect() else [f"{what}: render wrong"]


def _a(cfg_fn, expect, what):
    def run() -> list[str]:
        cfg = cfg_fn()
        return _accepts(cfg, lambda: expect(cfg), what)
    return run


def _derived_peak() -> dict:
    c = _with(good, lambda c: c["runners"]["pools"]["b"].pop("max_runners"))
    c["runners"]["pools"]["b"]["peak_mb"] = 2400  # 7168 // 2400 = 2, floors 6848: valid
    return c


def _deterministic() -> list[str]:
    g = good()
    if render(g, "x", "runners", DEMO_KIT) != render(copy.deepcopy(g), "x", "runners", DEMO_KIT) \
            or render(g, "x", "runners", DEMO_KIT) == render(g, "y", "runners", DEMO_KIT):
        return ["render not deterministic, or the source sha does not follow the source"]
    return []


def _class_pool(c: dict) -> None:
    c["runners"]["pools"]["a-heavy"] = {"enabled": True, "min_idle": 0, "max_runners": 1, "peak_mb": 3072,
                                        "repo": "a", "labels": ["self-hosted", "ci-heavy"],
                                        "variable": "CI_RUNNER_HEAVY"}


ACCEPTS: dict[str, Callable[[], list[str]]] = {
    "good file: disabled pool skipped, order kept": _a(good, lambda c: [p["repo"] for p in _out(c)["pools"]] == ["a", "b"],
                                                       "disabled pool rendered or order lost"),
    "good file: planned guest not rendered, runner_vm": _a(
        good, lambda c: "later" not in _out(c)["guests"] and _out(c)["runner_vm"] == {"name": "ci", "vmid": 910},
        "planned guest rendered or runner_vm wrong"),
    "running guest renders no state": _a(good, lambda c: "state" not in _out(c)["guests"]["obs"],
                                         "a running guest rendered a state key"),
    "stopped guest renders its state": _a(lambda: _with(good, lambda c: c["guests"]["obs"].update(state="stopped")),
                                          lambda c: _out(c)["guests"]["obs"].get("state") == "stopped",
                                          "a stopped guest rendered without state"),
    # 8192 - 1024 - 5120 = 2048 = pool a's peak
    "peak exactly fits the live room": _a(lambda: _with(good, lambda c: c["runners"].update(live_floor_mb=5120)),
                                          lambda c: True, "a peak that fits the live room"),
    "on_demand guest inside its reserve": _a(shared_reserve, lambda c: _out(c)["guests"]["seal"].get("state") == "on_demand",
                                             "an on_demand guest inside its reserve lost its state"),
    "scale from zero (min_idle 0)": _a(lambda: _with(good, lambda c: c["runners"]["pools"]["a"].update(min_idle=0)),
                                       lambda c: True, "scale-from-zero pool"),
    # obs 2c + a retired 8c would break the core budget unless the retired one is out of the sum
    "unbudgeted stopped guest is out of the sums": _a(
        lambda: _with(good, lambda c: c["guests"].update(
            retired={"vmid": 930, "cores": 8, "ram_mb": 8192, "state": "stopped", "budgeted": False})),
        lambda c: _out(c)["guests"]["retired"].get("budgeted") is False, "an unbudgeted stopped guest"),
    "peak_mb rendered": _a(good, lambda c: _out(c)["pools"][0].get("peak_mb") == 2048, "peak_mb not rendered"),
    "young_mb defaults to peak_mb": _a(good, lambda c: _out(c)["pools"][0].get("young_mb") == 2048,
                                       "young_mb not rendered as peak_mb"),
    "declared young_mb rendered": _a(lambda: _with(good, lambda c: c["runners"]["pools"]["a"].update(young_mb=768)),
                                     lambda c: _out(c)["pools"][0].get("young_mb") == 768, "a declared young_mb"),
    "undeclared starve_s not rendered": _a(good, lambda c: "starve_s" not in _out(c)["pools"][0],
                                           "starve_s rendered for a pool that declares none"),
    "declared starve_s 0 rendered": _a(lambda: _with(good, lambda c: c["runners"]["pools"]["a"].update(starve_s=0)),
                                       lambda c: _out(c)["pools"][0].get("starve_s") == 0, "a declared starve_s 0"),
    "mem_budget_mb = vm ram less vm_reserve_mb": _a(good, lambda c: _out(c).get("mem_budget_mb") == 8192 - 1024,
                                                    "mem_budget_mb"),
    "render deterministic, sha follows the source": _deterministic,
    "class pool: gh_repo is the pool's repo": _a(lambda: _with(good, _class_pool),
                                                 lambda c: _pools(c, "gh_repo") == {"a": "a", "a-heavy": "a", "b": "b"},
                                                 "gh_repo not rendered as the pool's repo"),
    "class pool: its own variable": _a(lambda: _with(good, _class_pool),
                                       lambda c: _pools(c, "variable") == {"a": "CI_RUNNER", "a-heavy": "CI_RUNNER_HEAVY",
                                                                           "b": "CI_RUNNER"},
                                       "variable not rendered as the pool's kill switch"),
    # no max_runners = admission's limit, min(max_total 4, 7168 // 1024)
    "derived ceiling: min(max_total, budget // peak)": _a(
        lambda: _with(good, lambda c: c["runners"]["pools"]["b"].pop("max_runners")),
        lambda c: _pools(c, "max") == {"a": 3, "b": 4}, "a pool without max_runners not derived"),
    "derived ceiling follows the pool's peak": _a(_derived_peak, lambda c: _out(c)["pools"][1]["max"] == 2,
                                                  "derived ceiling ignores the budget at the pool's peak"),
    "secure block: instances and prefixes": _a(
        good_secure,
        lambda c: (_out(c)["instance"], _out(c)["prefix"], _out(c)["isolation"]) == ("shared", "ci-", "socket")
        and (_out(c, "secure_runners")["instance"], _out(c, "secure_runners")["prefix"],
             _out(c, "secure_runners")["isolation"]) == ("secure", "tok-", "secure")
        and _out(c, "secure_runners")["runner_vm"] == {"name": "later", "vmid": 920}
        and _out(c, "secure_runners")["max_total"] == 2, "secure render"),
    "sealed block: one job, its own vm and label": _a(
        good_sealed,
        lambda c: (_out(c, "sealed_runners")["instance"], _out(c, "sealed_runners")["prefix"],
                   _out(c, "sealed_runners")["isolation"], _out(c, "sealed_runners")["max_total"]) == ("sealed", "boot-", "sealed", 1)
        and _out(c, "sealed_runners")["runner_vm"] == {"name": "sealed", "vmid": 912}
        and _out(c, "sealed_runners")["pools"][0]["labels"] == ["self-hosted", "ci-sealed"], "sealed render"),
}

P = lambda c: c["runners"]["pools"]["a"]  # noqa: E731
S = lambda c: c["secure_runners"]  # noqa: E731
L = lambda c: c["sealed_runners"]  # noqa: E731
_NEW_CLASS = {"enabled": True, "min_idle": 0, "max_runners": 1, "peak_mb": 1024, "repo": "a"}

# name -> (base, edit): base() edited must be REFUSED by validate
REFUSALS: dict[str, tuple[Callable[[], dict], Callable[[dict], object]]] = {
    "unknown top key": (good, lambda c: c.update(extra=1)),
    "unknown pool key": (good, lambda c: P(c).update(max=3)),
    "typo'd guest key": (good, lambda c: c["guests"]["ci"].update(core=4)),
    "max_total cpu over vm": (good, lambda c: c["runners"].update(max_total=5)),
    "max_runners over max_total": (good, lambda c: P(c).update(max_runners=5)),
    "floors over max_total": (good, lambda c: P(c).update(min_idle=3)),
    "no max_total": (good, lambda c: c["runners"].pop("max_total")),
    "pool memory over vm": (good, lambda c: P(c).update(memory_mb=8192)),
    # declared sizes pass, measured peaks do not fit (a 3 x 2400 > 7168)
    "measured peaks over vm": (good, lambda c: P(c).update(peak_mb=2400)),
    # floors at peaks: a 1 x 2048 + b 2 x 3100 = 8248 > 7168, though each ceiling fits alone
    "floors over memory": (good, lambda c: c["runners"]["pools"]["b"].update(peak_mb=3100)),
    "reserve eats the room": (good, lambda c: c["runners"].update(vm_reserve_mb=2100)),
    "enabled pool without peak_mb": (good, lambda c: P(c).pop("peak_mb")),
    "young_mb over peak_mb": (good, lambda c: P(c).update(young_mb=2049)),
    "young_mb zero": (good, lambda c: P(c).update(young_mb=0)),
    "young_mb not an int": (good, lambda c: P(c).update(young_mb="768")),
    "young_mb defaulted": (good, lambda c: c["runners"]["defaults"].update(young_mb=512)),
    "starve_s negative": (good, lambda c: P(c).update(starve_s=-1)),
    "starve_s not an int": (good, lambda c: P(c).update(starve_s="60")),
    "starve_s over an hour": (good, lambda c: P(c).update(starve_s=3601)),
    "starve_s defaulted": (good, lambda c: c["runners"]["defaults"].update(starve_s=0)),
    "peak_mb defaulted": (good, lambda c: (P(c).pop("peak_mb"), c["runners"]["defaults"].update(peak_mb=1))),
    "no vm_reserve_mb": (good, lambda c: c["runners"].pop("vm_reserve_mb")),
    "guest cores over host": (good, lambda c: c["guests"]["obs"].update(cores=8)),
    "guest ram over host": (good, lambda c: c["guests"]["obs"].update(ram_mb=12000)),
    "planned guest counts": (good, lambda c: c["guests"]["later"].update(ram_mb=6000)),
    "min_idle > max_runners": (good, lambda c: P(c).update(min_idle=3, max_runners=2)),
    # derived ceiling 7168 // 4000 = 1 < min_idle 2 (floors would also fail; this names it)
    "min_idle > derived ceiling": (good, lambda c: (c["runners"]["pools"]["b"].pop("max_runners"),
                                                    c["runners"]["pools"]["b"].update(peak_mb=4000))),
    "zero runners": (good, lambda c: P(c).update(min_idle=0, max_runners=0)),
    "negative floor": (good, lambda c: P(c).update(min_idle=-1)),
    "unbudgeted but running": (good, lambda c: c["guests"]["obs"].update(budgeted=False)),
    "budgeted not bool": (good, lambda c: c["guests"]["obs"].update(state="stopped", budgeted="no")),
    # budgeted defaults to true: a stopped guest still counts unless it says otherwise
    "stopped guest still counts": (good, lambda c: c["guests"].update(
        retired={"vmid": 930, "cores": 8, "ram_mb": 8192, "state": "stopped"})),
    "vm is not a guest": (good, lambda c: c["runners"].update(vm="nope")),
    "vm is only planned": (good, lambda c: c["runners"].update(vm="later")),
    "duplicate vmid": (good, lambda c: c["guests"]["obs"].update(vmid=910)),
    "unknown guest state": (good, lambda c: c["guests"]["obs"].update(state="paused")),
    "labels without self-hosted": (good, lambda c: P(c).update(labels=["ci"])),
    "enabled not bool": (good, lambda c: P(c).update(enabled="yes")),
    "bool as count": (good, lambda c: P(c).update(min_idle=True, max_runners=True)),
    "no pools": (good, lambda c: c["runners"].update(pools={})),
    "missing required block": (good, lambda c: c.pop("runners")),
    "pool with no tier label": (good, lambda c: P(c).update(labels=["self-hosted", "big"])),
    "bad pool repo": (good, lambda c: P(c).update(repo="no/slash")),
    "mem_book_ratio over 2": (good, lambda c: c["runners"].update(mem_book_ratio=2.5, live_floor_mb=512)),
    "mem_book_ratio > 1 without live_floor_mb": (good, lambda c: c["runners"].update(mem_book_ratio=1.5)),
    "peak over the live room": (good, lambda c: c["runners"].update(live_floor_mb=5121)),
    # the class pool's labels are a superset of a's: its runners would take a's light jobs
    "class pool nests its sibling": (good, lambda c: c["runners"]["pools"].update(
        {"a-heavy": {**_NEW_CLASS, "labels": ["self-hosted", "ci", "heavy"], "variable": "CI_RUNNER_HEAVY"}})),
    # the class pool shares a's switch: deleting it to stop one pool would stop both
    "class pool shares its sibling's variable": (good, lambda c: c["runners"]["pools"].update(
        {"a-heavy": {**_NEW_CLASS, "labels": ["self-hosted", "ci-heavy"]}})),
    "bad pool variable": (good, lambda c: P(c).update(variable="ci-runner")),
    # a reserve shared: the guest must be on_demand, inside a real reserve, alone in it
    "shares an unknown reserve": (shared_reserve, lambda c: c["guests"]["seal"].update(shares="nope")),
    "shares while always on": (shared_reserve, lambda c: c["guests"]["seal"].update(state="running")),
    "bigger than its reserve": (shared_reserve, lambda c: c["guests"]["seal"].update(ram_mb=2048)),
    "two guests share one reserve": (shared_reserve, lambda c: c["guests"].update(
        seal2={"vmid": 913, "cores": 1, "ram_mb": 512, "state": "on_demand", "shares": "drill"})),
    # the trust tiers: a label that crosses an isolation boundary, or two blocks on one VM
    "secure pool carries ci": (good_secure, lambda c: S(c)["pools"]["a"].update(labels=["self-hosted", "ci-secure", "ci"])),
    "secure pool without ci-secure": (good_secure, lambda c: S(c)["defaults"].update(labels=["self-hosted"])),
    "shared pool carries ci-secure": (good_secure, lambda c: P(c).update(labels=["self-hosted", "ci", "ci-secure"])),
    "shared pool carries a secure class": (good_secure, lambda c: P(c).update(labels=["self-hosted", "ci-secure-x"])),
    "secure pool carries a shared class": (good_secure, lambda c: S(c)["pools"]["a"].update(
        labels=["self-hosted", "ci-secure", "ci-heavy"])),
    "secure block on the shared vm": (good_secure, lambda c: S(c).update(vm="ci")),
    "secure vm only planned": (good_secure, lambda c: c["guests"]["later"].update(planned=True)),
    "unknown secure key": (good_secure, lambda c: S(c).update(isolation="none")),
    "secure peaks over its vm": (good_secure, lambda c: S(c)["pools"]["a"].update(peak_mb=1600)),
    # the sealed tier: its own label family, its own on_demand VM, one job at a time
    "sealed pool carries ci": (good_sealed, lambda c: L(c)["pools"]["a"].update(labels=["self-hosted", "ci-sealed", "ci"])),
    "sealed pool carries ci-secure": (good_sealed, lambda c: L(c)["pools"]["a"].update(
        labels=["self-hosted", "ci-sealed", "ci-secure"])),
    "sealed pool without ci-sealed": (good_sealed, lambda c: L(c)["defaults"].update(labels=["self-hosted"])),
    "shared pool carries ci-sealed": (good_sealed, lambda c: P(c).update(labels=["self-hosted", "ci", "ci-sealed"])),
    "secure pool carries ci-sealed": (good_sealed, lambda c: S(c)["pools"]["a"].update(
        labels=["self-hosted", "ci-secure", "ci-sealed"])),
    "sealed block on the secure vm": (good_sealed, lambda c: L(c).update(vm="later")),
    "sealed block on the shared vm": (good_sealed, lambda c: L(c).update(vm="ci")),
    "sealed max_total 2": (good_sealed, lambda c: (L(c).update(max_total=2), c["guests"]["sealed"].update(cores=2, ram_mb=2048))),
    "sealed vm always on": (good_sealed, lambda c: c["guests"]["sealed"].pop("state")),
}


def refused(name: str) -> list[str]:
    base, edit = REFUSALS[name]
    return validate(_with(base, edit), DEMO_KIT)


def selftest() -> list[str]:
    bad: list[str] = []
    for check in ACCEPTS.values():
        bad += check()
    for name in REFUSALS:
        if not refused(name):
            bad.append(f"{name}: accepted, should be rejected")
    return bad
