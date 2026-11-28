"""The budget math a render carries and admission spends: each pool's resolved shape, its derived
ceiling, and the runner VM's memory budget.

`repo` is the POOL id (runner names, container labels, metrics, the allocator); `gh_repo` is the
GitHub repository its runners register in and its demand is read from. A pool keyed by its repo
needs no `repo:`; a second pool for one repo, by job class (a heavy test job beside light lint
jobs), names the repo it serves and its own label and variable.
"""

from __future__ import annotations

from ci_kit.runners.kit import Kit


def pools(cfg: dict, kit: Kit, block: str) -> list[dict]:
    """Each ENABLED pool with defaults filled in, sorted by pool key (render order is stable)."""
    r = cfg[block]
    out = []
    for repo in sorted(r["pools"]):
        p = {**r["defaults"], **r["pools"][repo]}
        if p.get("enabled"):
            out.append({"repo": repo, "gh_repo": p.get("repo", repo),
                        "variable": p.get("variable", kit.block(block).variable), "min": p["min_idle"],
                        "max": p["max_runners"] if "max_runners" in p else derived_ceiling(cfg, block, p),
                        "cpus": p["cpus"],
                        "memory_mb": p["memory_mb"], "pids": p["pids"], "labels": list(p["labels"]),
                        # published beside the measured working set, so a job that beat the
                        # declared peak can be named from outside
                        "peak_mb": p["peak_mb"],
                        # the live cap's charge for a runner too young to show its memory
                        "young_mb": p.get("young_mb", p["peak_mb"]),
                        # absent = the allocator's default STARVE_S (rendered only when declared)
                        **({"starve_s": p["starve_s"]} if "starve_s" in p else {})})
    return out


def derived_ceiling(cfg: dict, block: str, p: dict) -> int:
    """A pool with no max_runners may grow as far as ADMISSION lets it: max_total slots, and as many
    runners at its own peak as the VM's booked budget holds. With memory-weighted admission live, a
    hand ceiling below that is the only thing that can hold a pool back while the VM has room, so
    declare max_runners only for a reason admission cannot see (a measured OOM shape, a
    one-at-a-time workflow) and say why beside it."""
    r = cfg[block]
    # the BOOKED budget (x mem_book_ratio): the live cap, not this ceiling, holds the real RAM
    budget = vm_budget(cfg, block)["mem_budget_mb"]
    return max(1, min(r["max_total"], budget // p["peak_mb"]))


def vm_budget(cfg: dict, block: str) -> dict:
    """What the allocator admits against: the runner VM's RAM less its reserve, times
    mem_book_ratio. With a ratio or a live floor, the unscaled base and the floor are rendered too:
    the allocator falls back to the base when its live MemAvailable read fails (never an
    overcommit admitted blind)."""
    r = cfg[block]
    base = cfg["guests"][r["vm"]]["ram_mb"] - r["vm_reserve_mb"]
    ratio = r.get("mem_book_ratio", 1)
    out = {"mem_budget_mb": int(base * ratio)}
    if ratio != 1 or "live_floor_mb" in r:
        out["mem_base_mb"] = base
    if "live_floor_mb" in r:
        out["live_floor_mb"] = r["live_floor_mb"]
    return out
