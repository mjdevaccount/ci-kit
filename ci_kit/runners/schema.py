"""The capacity file's schema and every budget it declares, as findings (empty = valid).

The file sizes a Proxmox-style host: its threads and RAM, the guests on it, and one or more runner
blocks, each a set of per-repo pools inside ONE runner VM. `validate` refuses, among others: an
unknown key; guest cores over host threads x cpu_overcommit; guest RAM plus host reserves over host
RAM; max_total x the largest runner over the runner VM's cores and RAM (x their overcommit); and
THE REAL BUDGET at each pool's MEASURED peak_mb against the VM's RAM less vm_reserve_mb -- the
floors must fit at their peaks, and each declared ceiling must fit alone. The allocator admits by
the same numbers at runtime (alloc.jq): a start needs a free slot AND its pool's peak free.
"""

from __future__ import annotations

from ci_kit.runners._checks import GUEST_STATES, KEYS, _pos, _unknown
from ci_kit.runners.budget import pools
from ci_kit.runners.kit import Kit
from ci_kit.runners.pools import block_budgets, pool_findings


def top_keys(kit: Kit) -> set[str]:
    return {"host", "guests", *kit.keys}


def validate(cfg: dict, kit: Kit) -> list[str]:
    """Every way the capacity file can be wrong under `kit`, as findings (empty = valid)."""
    f = _unknown("", cfg, top_keys(kit))
    required = {"host", "guests"} | {b.key for b in kit.blocks if b.required}
    if f or any(k not in cfg for k in required):
        return f + [f"missing top-level key {k!r}" for k in sorted(required - set(cfg or {}))]
    if not isinstance(cfg, dict):
        return f
    if not any(b in cfg for b in kit.keys):
        return [f"no runner block: the file declares none of {', '.join(kit.keys)}, so nothing is checked"]
    f = _guests(cfg, kit)
    if f:
        return f
    for b in kit.keys:
        if b in cfg:
            f += _validate_block(cfg, kit, b)
    present = [b for b in kit.keys if b in cfg]
    if len(present) > 1 and not f:
        if len({cfg[b]["vm"] for b in present}) != len(present):
            f.append("each runner block needs its own guest: the isolation IS the VM")
    for b in kit.blocks:
        if b.one_job_per_boot and b.key in cfg and not f:
            r = cfg[b.key]
            if r["max_total"] != 1:
                f.append(f"{b.key}.max_total must be 1: one job per boot of one VM")
            if cfg["guests"][r["vm"]].get("state") != "on_demand":
                f.append(f"{b.key}.vm must be state on_demand: only its supervisor starts it")
    if not f:
        for rule in kit.rules:
            f += rule(cfg, kit)
    return f


def _guests(cfg: dict, kit: Kit) -> list[str]:
    """The host and its guests: shapes first, then the host budgets the file is FOR."""
    host, guests = cfg["host"], cfg["guests"]
    f = _unknown("host", host, KEYS["host"])
    if not isinstance(guests, dict) or not guests:
        return f + ["guests: at least one guest is required"]
    for b in kit.keys:
        if b in cfg:
            f += _unknown(b, cfg[b], KEYS["block"])
    if f:
        return f
    for k in ("threads", "ram_mb"):
        if not _pos(host.get(k)):
            f.append(f"host.{k} must be a positive integer")
    for k in ("cpu_overcommit",):
        if not _pos(host.get(k), integer=False):
            f.append(f"host.{k} must be a positive number")
    reserves = host.get("reserves_mb") or {}
    if not isinstance(reserves, dict) or not all(_pos(v) for v in reserves.values()):
        f.append("host.reserves_mb must map names to positive integers")
        reserves = {}
    vmids = []
    for name, g in guests.items():
        f += _unknown(f"guests.{name}", g, KEYS["guest"])
        if not isinstance(g, dict):
            continue
        f += _guest(name, g, reserves)
        vmids.append(g.get("vmid"))
    shared = [g["shares"] for g in guests.values() if isinstance(g, dict) and "shares" in g]
    if len(set(shared)) != len(shared):
        f.append("guests: a reserve can be shared by one guest only")
    if len(set(vmids)) != len(vmids):
        f.append("guests: vmids must be unique")
    if f:
        return f
    # each finding names both sides so the fix is obvious
    budgeted = [g for g in guests.values() if g.get("budgeted", True)]
    cores = sum(g["cores"] for g in budgeted)
    cap = host["threads"] * host["cpu_overcommit"]
    if cores > cap:
        f.append(f"guests want {cores} cores; host allows {cap:g} (threads x cpu_overcommit)")
    # a guest that shares a reserve runs INSIDE it: the reserve is already in the sum
    ram = sum(g["ram_mb"] for g in budgeted if "shares" not in g) + sum(reserves.values())
    if ram > host["ram_mb"]:
        f.append(f"guests + reserves want {ram} MB; host has {host['ram_mb']} MB")
    return f


def _guest(name: str, g: dict, reserves: dict) -> list[str]:
    f = []
    for k in ("vmid", "cores", "ram_mb"):
        if not _pos(g.get(k)):
            f.append(f"guests.{name}.{k} must be a positive integer")
    if "planned" in g and not isinstance(g["planned"], bool):
        f.append(f"guests.{name}.planned must be true or false")
    if "state" in g and g["state"] not in GUEST_STATES:
        f.append(f"guests.{name}.state must be one of {', '.join(GUEST_STATES)}")
    if "budgeted" in g:
        if not isinstance(g["budgeted"], bool):
            f.append(f"guests.{name}.budgeted must be true or false")
        elif g["budgeted"] is False and g.get("state") != "stopped":
            f.append(f"guests.{name}: budgeted false needs state stopped (a running guest uses its share)")
    if "shares" in g:
        if g["shares"] not in reserves:
            f.append(f"guests.{name}.shares must name a host.reserves_mb entry")
        elif g.get("state") != "on_demand":
            f.append(f"guests.{name}: shares needs state on_demand (an always-on guest cannot yield)")
        elif _pos(g.get("ram_mb")) and g["ram_mb"] > reserves[g["shares"]]:
            f.append(f"guests.{name}.ram_mb {g['ram_mb']} exceeds reserve {g['shares']} "
                     f"({reserves[g['shares']]} MB)")
    return f


def _validate_block(cfg: dict, kit: Kit, block: str) -> list[str]:
    """One runner block against its VM: shape, pools, labels, then budgets."""
    f: list[str] = []
    guests, r = cfg["guests"], cfg[block]
    for k in ("cpu_overcommit", "memory_overcommit"):
        if not _pos(r.get(k), integer=False):
            f.append(f"{block}.{k} must be a positive number")
    for k in ("max_total", "vm_reserve_mb"):
        if not _pos(r.get(k)):
            f.append(f"{block}.{k} must be a positive integer")
    for k in ("backfill", "redecide"):
        if k in r and not isinstance(r[k], bool):
            f.append(f"{block}.{k} must be true or false")
    ratio = r.get("mem_book_ratio", 1)
    if not _pos(ratio, integer=False) or not 1 <= ratio <= 2:
        f.append(f"{block}.mem_book_ratio must be a number in [1, 2]")
    if "live_floor_mb" in r and not _pos(r["live_floor_mb"]):
        f.append(f"{block}.live_floor_mb must be a positive integer")
    # booking past the RAM is only safe while the live cap can say no
    if _pos(ratio, integer=False) and ratio > 1 and "live_floor_mb" not in r:
        f.append(f"{block}.mem_book_ratio > 1 needs live_floor_mb (the live MemAvailable cap)")
    vm = guests.get(r.get("vm"))
    if not isinstance(vm, dict):
        f.append(f"{block}.vm {r.get('vm')!r} is not a guest")
    elif vm.get("planned"):
        f.append(f"{block}.vm {r.get('vm')!r} is only planned")
    f += _unknown(f"{block}.defaults", r.get("defaults"), KEYS["defaults"])
    ps = r.get("pools")
    if not isinstance(ps, dict) or not ps:
        f.append(f"{block}.pools: at least one pool is required")
    if f:
        return f
    f = pool_findings(cfg, kit, block)
    if f:
        return f
    return block_budgets(cfg, kit, block, pools(cfg, kit, block))
