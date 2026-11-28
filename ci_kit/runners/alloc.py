"""The admission rule in Python: `allocate(doc)` returns exactly what `jq -c -f alloc.jq` prints.

The jq file is what runs on a runner VM (a supervisor there needs jq, not Python). This twin exists
so a domain can reason about, simulate and test admission in Python; tests pin the two verdicts
equal on a matrix of inputs, so neither can drift. Read alloc.jq for the WHY of every step -- this
module follows it line for line and keeps jq's semantics where Python's differ:

  * `a // b` is b when a is null OR false (never when a is 0): `_alt`.
  * truthiness is "not null and not false" (0 is true): `_truthy`.
  * `sort_by` is a stable sort on the key array; numbers before strings never mix here.
"""

from __future__ import annotations

import copy
import math
import pathlib
from typing import Any

JQ_PATH = pathlib.Path(__file__).with_name("alloc.jq")
STARVE_S = 300
AGE_S = 1200


class AllocError(ValueError):
    """The input is one jq refuses with error(...): the supervisor must not allocate on it."""


def _truthy(v: Any) -> bool:
    return v is not None and v is not False


def _alt(v: Any, d: Any) -> Any:
    return v if _truthy(v) else d


def _clamp0(x):
    return 0 if x < 0 else x


def _u(p: dict) -> Any:
    return min(_alt(p.get("since_start"), 0), p["oldest"])


def _aged(p: dict) -> Any:
    u = _u(p)
    return -u if u >= AGE_S and (p["running"] + p["start"]) == 0 else 0


def _could_fit(p: dict, floor_mb, floor_n, fit, total) -> bool:
    """Every other pool at its floor, this one at max(live, floor), plus the one it waits for."""
    return (floor_mb - p["min"] * p["peak"] + max(p["running"], p["min"]) * p["peak"] + p["peak"]) <= fit \
        and (floor_n - p["min"] + max(p["running"], p["min"]) + 1) <= total


def allocate(doc: dict) -> dict:
    """One allocator run: `{free, free_mb, drain, head, committed_mb, pools: [...]}`."""
    if not isinstance(doc, dict):
        raise AllocError(f"input must be an object, not {type(doc).__name__}")
    total = doc.get("max_total")
    budget = _alt(doc.get("mem_budget_mb"), None)
    if budget is None:
        raise AllocError("mem_budget_mb missing: count-only admission is refused")
    peaks = _alt(doc.get("peaks"), None)
    if peaks is None:
        raise AllocError("peaks missing")
    if not isinstance(peaks, dict) or not isinstance(doc.get("pools"), list):
        raise AllocError("peaks must be an object and pools a list")
    prev = _alt(doc.get("prev_head"), None)
    starve = _alt(doc.get("starve_s"), {})
    live = _alt(doc.get("live_free_mb"), None)
    base = _alt(doc.get("mem_base_mb"), None)
    fit = _alt(base, budget)
    bfon = doc.get("backfill") is not False
    maxpeak = _alt(max(peaks.values()) if peaks else None, 0)
    if len(doc["pools"]) > 0 and maxpeak <= 0:
        raise AllocError("no positive peak: admission would be count-only")

    pools = []
    for p in copy.deepcopy(doc["pools"]):
        p["desired"] = min(p["max"], max(p["min"], p["busy"] + p["queued"]))
        p["peak"] = _alt(peaks.get(p["repo"]), maxpeak)
        p.update(need=_clamp0(p["desired"] - p["running"]), floor_need=_clamp0(p["min"] - p["running"]),
                 start=0, held=False)
        pools.append(p)
    pools.sort(key=lambda p: (-p["oldest"], p["repo"]))
    booked = sum(p["running"] * p["peak"] for p in pools)
    if live is not None:
        second = live
    elif base is not None:
        second = base - booked
    else:
        second = budget - booked
    s = {"free": _clamp0(total - sum(p["running"] for p in pools)), "free_mb": min(budget - booked, second),
         "pools": pools}

    _floors(s)
    floor_mb = sum(p["min"] * p["peak"] for p in pools)
    floor_n = sum(p["min"] for p in pools)
    s.update(reserved=0, rslots=0, head=None)
    _blind_head(s, prev, floor_mb, floor_n, fit, total)
    _starved(s, prev, starve, floor_mb, floor_n, fit, total)
    s["drain"] = s["head"] is not None
    if s["drain"] and bfon:
        _backfill(s)
    elif not s["drain"]:
        _round_robin(s)
    head = s["head"]
    for p in pools:
        p["cap"] = p["min"] if head is not None and p["repo"] != head else p["desired"]
    _reclaim(s)
    return {"free": s["free"], "free_mb": s["free_mb"], "drain": s["drain"], "head": s["head"],
            "committed_mb": sum((p["running"] + p["start"]) * p["peak"] for p in s["pools"]),
            "pools": [{k: p[k] for k in ("repo", "desired", "start", "reclaim", "held", "cap")}
                      for p in sorted(s["pools"], key=lambda p: p["repo"])]}


def _grant(s: dict, p: dict, n=1) -> None:
    p["start"] += n
    p["need"] -= n
    s["free"] -= n
    s["free_mb"] -= n * p["peak"]


def _floors(s: dict) -> None:
    for p in s["pools"]:
        by_mem = _clamp0(math.floor(s["free_mb"] / p["peak"])) if p["peak"] > 0 else s["free"]
        _grant(s, p, min(p["floor_need"], s["free"], by_mem))


def _blind_head(s: dict, prev, floor_mb, floor_n, fit, total) -> None:
    bh = next((p for p in s["pools"] if p["repo"] == prev and _truthy(_alt(p.get("blind"), False))), None)
    if bh is None or bh["running"] != 0 or not _could_fit(bh, floor_mb, floor_n, fit, total):
        return
    if s["free"] > 0 and bh["peak"] <= s["free_mb"]:
        s["free"] -= 1
        s["free_mb"] -= bh["peak"]
    else:
        s["reserved"] += bh["peak"]
        s["rslots"] += 1
        s["head"] = prev


def _starved(s: dict, prev, starve, floor_mb, floor_n, fit, total) -> None:
    pools = s["pools"]
    carry = next((p["repo"] for p in pools if p["repo"] == prev and (p["running"] + p["start"]) == 0), None)
    order = [i for i, p in enumerate(pools)
             if p["need"] > 0 and (p["oldest"] >= _alt(starve.get(p["repo"]), STARVE_S) or p["repo"] == carry)]
    order.sort(key=lambda i: (0 if pools[i]["repo"] == carry else 1, _aged(pools[i]),
                              pools[i]["running"] + pools[i]["start"], -pools[i]["oldest"], pools[i]["repo"]))
    for i in order:
        p = pools[i]
        if (s["free"] - s["rslots"]) > 0 and p["peak"] <= (s["free_mb"] - s["reserved"]):
            _grant(s, p)
        elif (p["running"] + p["start"]) == 0 and _could_fit(p, floor_mb, floor_n, fit, total):
            p["held"] = True
            s["reserved"] += p["peak"]
            s["rslots"] += 1
            s["head"] = _alt(s["head"], p["repo"])
        else:
            p["held"] = True


def _backfill(s: dict) -> None:
    pools, h = s["pools"], s["head"]
    hp = next((p for p in pools if p["repo"] == h), None)
    if hp is None:
        return
    bigp = max([p["peak"] if p["repo"] != h and p["running"] > p["min"] else 0 for p in pools] or [0])
    fl_mb = sum(_clamp0(p["min"] - p["running"] - p["start"]) * p["peak"] for p in pools)
    fl_n = sum(_clamp0(p["min"] - p["running"] - p["start"]) for p in pools)
    bf = min(s["free_mb"], s["free_mb"] + bigp - hp["peak"]) - fl_mb
    order = [i for i, p in enumerate(pools) if p["repo"] != h and p["need"] > 0]
    order.sort(key=lambda i: (_aged(pools[i]), pools[i]["running"] + pools[i]["start"],
                              -pools[i]["oldest"], pools[i]["repo"]))
    stop: set[int] = set()
    while not (s["free"] <= fl_n or all(i in stop for i in order)):
        for i in order:
            p = pools[i]
            if i in stop:
                continue
            if s["free"] > fl_n and p["need"] > 0 and p["peak"] <= bf:
                _grant(s, p)
                p["held"] = False
                bf -= p["peak"]
            else:
                stop.add(i)
                if p["need"] > 0 and s["free"] > fl_n:
                    p["held"] = True


def _round_robin(s: dict) -> None:
    pools = s["pools"]
    while not (s["free"] <= 0 or not any(p["need"] > 0 and not p["held"] for p in pools)):
        for p in pools:
            if s["free"] > 0 and p["need"] > 0 and not p["held"]:
                if p["peak"] <= s["free_mb"]:
                    _grant(s, p)
                else:
                    p["held"] = True


def _reclaim(s: dict) -> None:
    unmet = sum(p["need"] for p in s["pools"])
    for p in s["pools"]:
        if p["cap"] >= p["desired"]:
            p["surplus"] = _clamp0(min(p["running"] - p["cap"], p["idle"]))
        elif p["start"] > 0:
            p["surplus"] = 0
        else:
            p["surplus"] = _clamp0(min(p["running"] - p["cap"], p["idle"] - p["queued"]))
    s["pools"] = sorted(s["pools"], key=lambda p: (-p["surplus"], p["repo"]))
    left = unmet
    for p in s["pools"]:
        r = min(p["surplus"], left)
        p["reclaim"] = r
        left -= r
