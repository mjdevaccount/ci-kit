"""One runner block's pools: their shapes, the trust-tier labels, class pools, and the VM budgets.

LABELS SEPARATE THE TRUST TIERS. A job reaches a pool by label only (a User account has no runner
groups), so each pool carries exactly ONE tier: its block's label, or that label plus `-<class>`
(`ci-heavy` is the `ci` tier). A pool that also carried another block's label would let a job land
on the wrong side of that block's isolation boundary.

TWO POOLS, ONE REPO. A runner takes any job whose labels are a subset of its own, and demand is
counted the same way, so nested label sets would let the big pool's runners take (and count) the
small pool's jobs: each class pool of one repo needs a label the other lacks, and its own
variable, or its kill switch stops the sibling too.
"""

from __future__ import annotations

from ci_kit.runners._checks import KEYS, LABEL, REPO, VARIABLE, _nonneg_int, _pos, _unknown
from ci_kit.runners.kit import Kit


def pool_findings(cfg: dict, kit: Kit, block: str) -> list[str]:
    r, meta = cfg[block], kit.block(block)
    defaults, ps = r["defaults"], r["pools"]
    f: list[str] = []
    for repo, p in ps.items():
        where = f"{block}.pools.{repo}"
        f += _unknown(where, p, KEYS["pool"])
        if not isinstance(p, dict):
            continue
        f += _one_pool(where, repo, {**defaults, **p}, p, r, block)
    if f:
        return f
    for repo in ps:
        labels = {**defaults, **ps[repo]}.get("labels") or []
        if {kit.tier(x) for x in labels} - {None} != {meta.label}:
            f.append(f"{block}.pools.{repo}.labels must carry the {meta.label!r} tier "
                     f"({meta.label!r} or {meta.label}-<class>) and no other")
    by_repo: dict[str, list[str]] = {}
    for repo, p in ps.items():
        if {**defaults, **p}.get("enabled") is True:
            by_repo.setdefault({**defaults, **p}.get("repo", repo), []).append(repo)
    for gh, keys in sorted(by_repo.items()):
        for i, x in enumerate(keys):
            for y in keys[i + 1:]:
                lx, ly = (set({**defaults, **ps[k]}["labels"]) for k in (x, y))
                vx, vy = ({**defaults, **ps[k]}.get("variable", meta.variable) for k in (x, y))
                if vx == vy:
                    f.append(f"{block}.pools.{x} and {y} both serve {gh} under variable {vx}: each "
                             f"class pool needs its own, or its kill switch also stops the sibling")
                if lx <= ly or ly <= lx:
                    f.append(f"{block}.pools.{x} and {y} both serve {gh} with nested labels "
                             f"{sorted(lx)} / {sorted(ly)}: each needs a label the other lacks")
    return f


def _one_pool(where: str, repo, p: dict, raw: dict, r: dict, block: str) -> list[str]:
    """One pool's own shape; `p` has the block defaults filled in, `raw` is as written."""
    f = []
    if not REPO.match(str(repo)):
        f.append(f"{where}: repo name must match {REPO.pattern}")
    if "repo" in raw and not (isinstance(raw["repo"], str) and REPO.match(raw["repo"])):
        f.append(f"{where}.repo must match {REPO.pattern}")
    if "variable" in raw and not (isinstance(raw["variable"], str) and VARIABLE.match(raw["variable"])):
        f.append(f"{where}.variable must match {VARIABLE.pattern}")
    if not isinstance(p.get("enabled"), bool):
        f.append(f"{where}.enabled must be true or false")
    # min_idle 0 = scale from zero: no runner until a job queues
    if not _nonneg_int(p.get("min_idle")):
        f.append(f"{where}.min_idle must be a non-negative integer")
    for k in ("max_runners", "cpus", "memory_mb", "pids"):
        if k == "max_runners" and k not in p:
            continue  # optional: absent = derived from admission (budget.derived_ceiling)
        if not _pos(p.get(k), integer=(k != "cpus")):
            f.append(f"{where}.{k} must be positive")
    if p.get("enabled") is True and not _pos(p.get("peak_mb")):
        f.append(f"{where}.peak_mb must be a positive integer: one busy job's MEASURED peak")
    if "young_mb" in p and not (_pos(p["young_mb"]) and (not _pos(p.get("peak_mb"))
                                                         or p["young_mb"] <= p["peak_mb"])):
        f.append(f"{where}.young_mb must be a positive integer <= peak_mb: the pool's MEASURED "
                 f"p95 working set (a charge over the peak only ever holds the queue)")
    if "starve_s" in p and not (_nonneg_int(p["starve_s"]) and p["starve_s"] <= 3600):
        f.append(f"{where}.starve_s must be an integer 0..3600 (s a held job waits before the drain)")
    if _nonneg_int(p.get("min_idle")) and _pos(p.get("max_runners")):
        if p["min_idle"] > p["max_runners"]:
            f.append(f"{where}: min_idle {p['min_idle']} > max_runners {p['max_runners']}")
        if p.get("enabled") is True and p["max_runners"] > r["max_total"]:
            f.append(f"{where}: max_runners {p['max_runners']} > {block}.max_total {r['max_total']}")
    labels = p.get("labels")
    if not isinstance(labels, list) or "self-hosted" not in labels \
            or not all(isinstance(x, str) and LABEL.match(x) for x in labels):
        f.append(f"{where}.labels must be a list of {LABEL.pattern} names including self-hosted")
    return f


def block_budgets(cfg: dict, kit: Kit, block: str, live: list[dict]) -> list[str]:
    """What runtime admission cannot make true, checked here against the runner VM.

    max_total is what the VM runs at once, so it -- not the sum of the pools' ceilings, which
    overlap by design -- must fit the VM at the largest per-runner size any pool declares. Then
    THE REAL BUDGET: declared memory_mb is unenforced, so it can pass while the VM runs out of
    memory; peak_mb is what a busy job was MEASURED to use. The floors must fit at their peaks
    (else a pool is short forever) and each declared ceiling must fit alone (else max_runners is a
    number it can never reach), both against the REAL RAM: the runtime booking may run past it
    (mem_book_ratio, capped live by MemAvailable), these may not."""
    f: list[str] = []
    if not live:
        return f
    r, vm, ps = cfg[block], cfg["guests"][cfg[block]["vm"]], cfg[block]["pools"]
    floors = sum(p["min"] for p in live)
    if floors > r["max_total"]:
        f.append(f"pools' min_idle sum to {floors}; {block}.max_total is {r['max_total']}")
    want_cpu = r["max_total"] * max(p["cpus"] for p in live)
    cap_cpu = vm["cores"] * r["cpu_overcommit"]
    if want_cpu > cap_cpu:
        f.append(f"max_total wants {want_cpu:g} cpus; {r['vm']} allows {cap_cpu:g} (cores x cpu_overcommit)")
    want_mem = r["max_total"] * max(p["memory_mb"] for p in live)
    cap_mem = vm["ram_mb"] * r["memory_overcommit"]
    if want_mem > cap_mem:
        f.append(f"max_total wants {want_mem} MB; {r['vm']} allows {cap_mem:g} MB (ram x memory_overcommit)")
    cap_real = vm["ram_mb"] - r["vm_reserve_mb"]
    floor_mb = sum(p["min"] * ps[p["repo"]]["peak_mb"] for p in live)
    if floor_mb > cap_real:
        shape = " + ".join(f"{p['min']} x {p['repo']} {ps[p['repo']]['peak_mb']}" for p in live if p["min"])
        f.append(f"floors at measured peaks want {floor_mb} MB ({shape}); {r['vm']} holds {cap_real} MB "
                 f"(ram_mb - vm_reserve_mb) -- lower a min_idle")
    for p in live:
        if "max_runners" not in ps[p["repo"]]:
            if p["min"] > p["max"]:
                f.append(f"{block}.pools.{p['repo']}: min_idle {p['min']} > its derived ceiling {p['max']} "
                         f"(max_total, or the budget at its peak_mb)")
            continue  # derived: its ceiling x peak fits the budget by construction
        want = p["max"] * ps[p["repo"]]["peak_mb"]
        if want > cap_real:
            f.append(f"{block}.pools.{p['repo']}: max_runners {p['max']} at measured peaks wants {want} MB; "
                     f"{r['vm']} holds {cap_real} MB (ram_mb - vm_reserve_mb) -- lower max_runners")
    # THE LIVE ROOM: under the live cap a start needs its pool's peak free in the VM's MemAvailable
    # less live_floor_mb, and an IDLE VM reads about ram_mb - vm_reserve_mb. A pool whose peak is
    # over that room can never start, every budget above passing.
    if "live_floor_mb" in r:
        cap_live = cap_real - r["live_floor_mb"]
        for p in live:
            if ps[p["repo"]]["peak_mb"] > cap_live:
                f.append(f"{block}.pools.{p['repo']}: peak_mb {ps[p['repo']]['peak_mb']} never fits the live room "
                         f"{cap_live} MB (ram_mb - vm_reserve_mb - live_floor_mb) -- it would queue forever")
    return f
