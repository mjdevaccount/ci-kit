"""The capacity file's key sets, name patterns and value predicates, shared by every check."""

from __future__ import annotations

import re

REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
VARIABLE = re.compile(r"^[A-Z][A-Z0-9_]*$")
LABEL = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# A guest's EXPECTED power state. `stopped` = kept on purpose but powered off: still rendered (the
# provisioner reads its size) and still counted in every budget (a revival must fit) unless
# `budgeted: false`. `on_demand` = stopped between jobs, started only by its supervisor.
GUEST_STATES = ("running", "stopped", "on_demand")
# `shares: <reserve>` (only with state on_demand): the guest runs INSIDE a host reserve instead of
# beside it, so its RAM is not summed twice; the reserve's other owner must yield to it (a kit
# share duty). `budgeted: false` (only with state stopped): a retired guest whose cores and RAM
# another guest took -- rendered, but left out of the host sums, so a revival must shrink something.

KEYS = {
    "host": {"name", "threads", "ram_mb", "cpu_overcommit", "reserves_mb"},
    "guest": {"vmid", "cores", "ram_mb", "planned", "state", "budgeted", "shares"},
    # backfill: false turns the allocator's drain backfill off (default on)
    # redecide: false stops the supervisor re-running the allocator every refill pass (default on)
    # mem_book_ratio / live_floor_mb: admission books peaks against (ram - reserve) x ratio, capped
    # each tick by the VM's live MemAvailable less live_floor_mb
    "block": {"vm", "max_total", "cpu_overcommit", "memory_overcommit", "vm_reserve_mb", "defaults",
              "pools", "backfill", "redecide", "mem_book_ratio", "live_floor_mb"},
    # no peak_mb here: a defaulted peak is a declared number, not a measured one
    "defaults": {"cpus", "memory_mb", "pids", "labels"},
    # repo: the GitHub repo this pool serves when the pool's KEY is not that repo (a class pool)
    # variable: the repo variable this pool's jobs read in runs-on -- the kill switch an alert names
    # young_mb: what a runner younger than the supervisor's young window is charged against the live
    #   cap while MemAvailable cannot see its growth yet (default peak_mb; <= peak_mb)
    # starve_s: this pool's own STARVE_S -- how long its oldest job waits, held without a
    #   reservation, before the allocator drains for it (default 300)
    "pool": {"enabled", "min_idle", "max_runners", "cpus", "memory_mb", "pids", "labels", "peak_mb", "repo",
             "variable", "young_mb", "starve_s"},
}


def _nonneg_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and x >= 0


def _pos(x, integer=True) -> bool:
    if isinstance(x, bool):
        return False
    return (isinstance(x, int) if integer else isinstance(x, (int, float))) and x > 0


def _unknown(where: str, d, allowed: set[str]) -> list[str]:
    if not isinstance(d, dict):
        return [f"{where or 'top level'}: must be a mapping"]
    return [f"{where or 'top level'}: unknown key {k!r}" for k in sorted(set(d) - allowed)]
