"""Python twins of the supervisor tick's PURE decisions (`sh/supervisor.sh`, `sh/runs.sh`).

A runner host runs the bash and jq. These exist so a domain can reason about a tick -- replay a
trace, simulate a scenario, test a policy -- without a host, and so each decision has a second,
independent statement that a parity test holds to the first: `tests/test_runners_tick_parity.py`
runs the PACKAGED functions in real bash and jq on a matrix of inputs and requires the same answer.
The bash stays the implementation; these twins are pinned to it, never the other way round.

Each twin takes what its bash reads (arguments, the arrays it consults, its stdin) as plain values
and returns what the bash prints, or the decision its exit code makes. The jq semantics that decide
edge cases are kept: truthiness (only null and false are false, so 0 and "" are true), `//` (the
right side for null or false), and the cross-type ordering a `>` uses on a field that may not be a
number. Inputs are the shapes the supervisor reads (a schema-checked render, docker's and GitHub's
JSON, decimal integers); a shape outside them is outside the contract, as it is for the bash.

  carry_head            the drain head the next run is handed, and the blind-carry count
  young_table           the young charge per pool and the fallback charge, from a render
  young_charge          one pool's charge for a runner MemAvailable cannot see yet
  young_of              the MB still to come from young runners in a listing (or the MB waived)
  since_start           a pool's age since its last start, from its start file's bytes
  refill_more           does the refill window stretch: a visible pool with queued jobs unstarted
  redecide_entry        one pool's state overlay in a refill-pass re-decide
  redecide_state        the tick's allocator state with those overlays applied
  redecide_live_free    the re-decide's live room, this pass's starts charged
  secure_violations     each container off the secure runner shape, and what is off
  registry_probe_lines  the registry cache probe's output as emit-ready samples
  stray_age             the oldest unlabelled container's age, the reaper's outside witness
  job_demand            a run's jobs that a pool's label set covers, and the oldest one's wait
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

BLIND_CARRY_MAX = 10  # sh/supervisor.sh BLIND_CARRY_MAX
YOUNG_S = 300  # sh/supervisor.sh YOUNG_S
NNP = ("no-new-privileges", "no-new-privileges:true", "no-new-privileges=true")
_ISNUM = re.compile(r"[0-9]+")
_EPOCH = re.compile(r"[1-9][0-9]{0,11}")
_CODES = re.compile(r"([0-9]{3}) ([0-9]{3})")
_COUNTER = re.compile(r'registry_proxy_(requests|hits|misses|pulled_bytes|pushed_bytes)_total'
                      r'\{type="(blob|manifest)"\} ([0-9]+(\.[0-9]+)?([eE][+]?[0-9]+)?)')
_FRACTION = re.compile(r"\.[0-9]+Z$")


def _alt(value, default):
    """jq's `value // default`: the default for null or false."""
    return default if value is None or value is False else value


def _truthy(value) -> bool:
    """jq's truthiness: only null and false are false."""
    return value is not None and value is not False


def _gt0(value) -> bool:
    """jq's `value > 0` across types: null < false < true < numbers < strings < arrays < objects."""
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value > 0
    return True


def _jq_eq(a, b) -> bool:
    """jq's `==`: a boolean equals only a boolean (Python's True == 1 does not hold in jq)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


def _isnum(text: str) -> bool:
    """The supervisor's isnum: a plain run of ASCII digits."""
    return _ISNUM.fullmatch(text) is not None


def carry_head(prev: str, head: str, state: Iterable[Mapping], nblind: int,
               max_carry: int = BLIND_CARRY_MAX) -> tuple[str, int, bool]:
    """-> (the head the next run drains for, "-" for none; the blind count; was a blind head dropped).

    A blind `prev` is carried only over an empty plan head or the same one, and only while it has
    been blind no more than `max_carry` runs in a row; any other run resets the count."""
    if prev and (not head or head == prev) and any(_truthy(_alt(p.get("blind"), False))
                                                    for p in state if p.get("repo") == prev):
        nblind += 1
        if nblind <= max_carry:
            return prev, nblind, False
        return "-", 0, True
    return (head or "-"), 0, False


def young_table(render: Mapping) -> tuple[dict, int]:
    """-> (pool -> young_mb, else peak_mb; the largest peak_mb, 0 for none): YOUNG_JSON / YOUNG_MAX."""
    pools = render.get("pools") or []
    table = {p["repo"]: _alt(p.get("young_mb"), p.get("peak_mb")) for p in pools}
    peaks = [p["peak_mb"] for p in pools if isinstance(p.get("peak_mb"), int) and not isinstance(p["peak_mb"], bool)]
    return table, max([v for v in peaks if v >= 0], default=0)


def young_charge(table: Mapping, young_max: int, repo: str) -> int:
    """The MiB a not-yet-visible runner of `repo` is charged: its table entry when that prints as a
    plain decimal, else the largest peak -- a pool missing from the render is never charged zero."""
    c = _alt(table.get(repo), None)
    if isinstance(c, int) and not isinstance(c, bool) and c >= 0:
        return c
    if isinstance(c, str) and _isnum(c):
        return int(c)
    return young_max


def young_of(listing: str, table: Mapping, young_max: int, now: int, waived_names: Iterable[str] = (),
             waived: bool = False, young_s: int = YOUNG_S) -> int:
    """`listing` = "<repo> <name> [<working-set bytes>]" lines. -> the MB still to come from runners
    younger than `young_s` (a name <...>-<epoch>-<suffix>, epoch a plain decimal with no leading 0),
    each charged its young charge less its working set, only when that leaves more than 0; with
    `waived`, the MB of the runners in `waived_names` (the YOUNG WAIVER) instead of the rest."""
    skip = set(waived_names)
    total = wtotal = 0
    for line in listing.split("\n"):
        fields = line.split(None, 2)
        if len(fields) < 2:
            continue
        repo, name = fields[0], fields[1]
        ws = fields[2].strip() if len(fields) > 2 else ""
        epoch = name.rsplit("-", 1)[0].rsplit("-", 1)[-1]
        if _EPOCH.fullmatch(epoch) is None or now - int(epoch) >= young_s:
            continue
        charge = young_charge(table, young_max, repo)
        if _isnum(ws):
            charge -= int(ws) // 1048576
        if charge <= 0:
            continue
        if name in skip:
            wtotal += charge
        else:
            total += charge
    return wtotal if waived else total


def since_start(raw: str | None, now: int) -> int:
    """Seconds since the pool's last start: `raw` = its start file's content (None = no file), read
    as the digits in its first 20 bytes. No digits = a clock that starts now (the bash writes the
    file), so 0; a start in the future is 0, never negative."""
    t = "".join(ch for ch in (raw or "")[:20] if "0" <= ch <= "9")
    return max(now - int(t), 0) if t else 0


def _left(repo: str, queued: Mapping[str, int], idle: Mapping[str, int], started: Mapping[str, int]) -> int:
    return queued.get(repo, 0) - idle.get(repo, 0) - started.get(repo, 0)


def refill_more(repos: Iterable[str], targets: Mapping[str, object], failed: Iterable[str],
                queued: Mapping[str, int], idle: Mapping[str, int], started: Mapping[str, int]) -> bool:
    """True while a pool the tick could see (a target set) whose start has not failed this run still
    has queued jobs that no idle runner it saw and no start since covers."""
    bad = set(failed)
    return any(_left(r, queued, idle, started) > 0 for r in repos
               if targets.get(r) not in (None, "") and r not in bad)


def redecide_entry(running: int, queued: int, idle: int, started: int, job_wait: int, elapsed: int,
                   since_start: int) -> dict[str, int]:
    """One pool's overlay in a re-decide, from what it can see without GitHub: running = busy = the
    live runners plus this pass's starts, idle 0, queued less its idle runners and every start since,
    and the tick's job wait aged by the time since the tick (over-ages, never under)."""
    left = max(queued - idle - started, 0)
    return {"running": running, "busy": running, "idle": 0, "queued": left,
            "oldest": job_wait + elapsed if left > 0 else 0, "since_start": since_start}


def redecide_state(state: Iterable[Mapping], overlays: Mapping[str, Mapping]) -> list[dict]:
    """The tick's allocator state with each pool's overlay merged in (a pool without one, as the tick saw it)."""
    return [{**p, **overlays.get(p.get("repo"), {})} for p in state]


def redecide_live_free(mem_bytes: int, floor_mb: int, young_mb: int, pass_starts: Mapping[str, int],
                       charges: Mapping[str, int]) -> int:
    """The re-decide's live room in MiB: MemAvailable less the floor and the young runners, less
    each start this pass made (in neither the listing nor MemAvailable yet) at its pool's young charge."""
    return mem_bytes // 1048576 - floor_mb - young_mb - sum(n * charges[r] for r, n in pass_starts.items() if n > 0)


def _violations(c: Mapping, net: str) -> list[str]:
    h = c.get("HostConfig") or {}
    so = _alt(h.get("SecurityOpt"), [])
    memory = h.get("Memory")
    checks = (
        ("bind", _alt(h.get("Binds"), []) != []),
        ("mount", _alt(h.get("Mounts"), []) != [] or _alt(c.get("Mounts"), []) != []),
        ("volumes-from", _alt(h.get("VolumesFrom"), []) != []),
        ("cap-drop", "ALL" not in _alt(h.get("CapDrop"), [])),
        ("cap-add", _alt(h.get("CapAdd"), []) != []),
        ("no-new-privileges", not any(o in NNP for o in so)),
        ("security-opt", any(o not in NNP for o in so)),
        ("apparmor", _alt(c.get("AppArmorProfile"), "") == "unconfined"),
        ("user", (c.get("Config") or {}).get("User") != "1001:1001"),
        ("network", h.get("NetworkMode") != net
         or sorted(_alt((c.get("NetworkSettings") or {}).get("Networks"), {})) != [net]),
        ("privileged", h.get("Privileged") is not False),
        ("group-add", _alt(h.get("GroupAdd"), []) != []),
        ("device", _alt(h.get("Devices"), []) != []),
        ("pid", _alt(h.get("PidMode"), "") != ""),
        ("ipc", _alt(h.get("IpcMode"), "") not in ("", "private", "none")),
        ("userns", _alt(h.get("UsernsMode"), "") != ""),
        ("uts", _alt(h.get("UTSMode"), "") != ""),
        ("cgroupns", _alt(h.get("CgroupnsMode"), "private") != "private"),
        ("sysctl", _alt(h.get("Sysctls"), {}) != {}),
        ("runtime", _alt(h.get("Runtime"), "runc") != "runc"),
        ("limits", not (_gt0(_alt(memory, 0)) and _jq_eq(h.get("MemorySwap"), memory)
                        and _gt0(_alt(h.get("PidsLimit"), 0)) and _gt0(_alt(h.get("NanoCpus"), 0)))),
    )
    return [what for what, off in checks if off]


def secure_violations(containers: Iterable[Mapping], net: str) -> list[str]:
    """`docker inspect` objects -> "<name> <what>,..." for each one off the secure runner's shape (an
    ALLOW-list where docker has one), in input order; a container on shape prints nothing."""
    out = []
    for c in containers:
        bad = _violations(c, net)
        if bad:
            out.append(f"{_alt(c.get('Name'), '?').removeprefix('/')} {','.join(bad)}")
    return out


def registry_probe_lines(text: str, prefix: str) -> list[str]:
    """The probe's output (line 1 "<cache code> <upstream code>", then registry_proxy_* counters) ->
    samples. Only newline-TERMINATED lines are read (bash `read`); a line 1 that is not two 3-digit
    codes publishes nothing; a counter line that is not exactly a known counter is dropped. Upstream
    reachable = 200 or 401 (anonymous /v2/ asks for a token)."""
    lines = text.split("\n")[:-1]
    m = _CODES.fullmatch(lines[0]) if lines else None
    if m is None:
        return []
    out = [f"{prefix}registry_cache_up {1 if m.group(1) == '200' else 0}",
           f"{prefix}registry_upstream_ok {1 if m.group(2) in ('200', '401') else 0}"]
    for line in lines[1:]:
        c = _COUNTER.fullmatch(line)
        if c is not None:
            out.append(f'{prefix}registry_proxy_{c.group(1)}_total{{type="{c.group(2)}"}} {float(c.group(3)):.0f}')
    return out


def _fromdate(iso: str) -> int:
    """jq's fromdateiso8601: "%Y-%m-%dT%H:%M:%SZ", UTC."""
    return int(datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp())


def stray_age(inspect: Iterable[Mapping], exclude_id: str, now: int, repo_label: str) -> int:
    """Seconds the oldest container WITHOUT the repo label (other than `exclude_id`, the registry
    cache) has run, a fractional StartedAt truncated; 0 for none. An unparseable StartedAt raises
    ValueError (the bash read fails: no sample)."""
    ages = [now - _fromdate(_FRACTION.sub("Z", c["State"]["StartedAt"])) for c in inspect
            if c.get("Id") != exclude_id and _alt(_alt((c.get("Config") or {}).get("Labels"), {}).get(repo_label), "") == ""]
    return math.floor(max(ages, default=0))


def job_demand(jobs: Mapping, pool_labels: list[str], need: str, now: int) -> tuple[int, int]:
    """-> (queued jobs that carry `need` and whose every label the pool's set covers, seconds the
    oldest of them has waited -- 0 for none). A job asking for a label no pool carries is a workflow
    bug, not a slow pool, so it is nobody's demand."""
    created = [_fromdate(j["created_at"]) for j in jobs["jobs"]
               if j.get("status") == "queued" and need in j["labels"] and all(x in pool_labels for x in j["labels"])]
    return len(created), (now - min(created) if created else 0)
