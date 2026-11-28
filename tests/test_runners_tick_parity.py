"""ci_kit.runners.tick (the Python twins) and the PACKAGED supervisor's own functions give the SAME
answer on a matrix of inputs: each bash function is cut out of sh/supervisor.sh (or sourced from
sh/runs.sh) as it ships and run in real bash and jq under the supervisor's own `set -uo pipefail`,
hand-built cases for each branch plus a seeded random matrix where the input space is wide.

A missing bash or jq FAILS in CI (`$CI` set) and skips only on a workstation without them: a skipped
parity test reads exactly like a passing one.
"""

from __future__ import annotations

import json
import os
import random
import re
import shlex
import shutil
import subprocess
import tempfile

import pytest

from ci_kit.runners import tick
from ci_kit.runners.scripts import SH_DIR

SUP = (SH_DIR / "supervisor.sh").read_text(encoding="utf-8")
# jq.exe on a Windows workstation ends lines CRLF; a no-op on Linux. pipefail as the supervisor runs.
_PRE = "set -uo pipefail\njq() { command jq \"$@\" | tr -d '\\r'; }\n"
NOW = 1_800_000_000


def _bash() -> str:
    bash = shutil.which("bash")  # resolved: a bare "bash" on Windows is System32's WSL launcher
    if bash is None or shutil.which("jq") is None:
        if os.environ.get("CI"):
            pytest.fail("bash and jq must be on PATH: the parity test cannot run (the supervisor needs them too)")
        pytest.skip("bash/jq not on PATH (workstation)")
    return bash


def _fn(name: str) -> str:
    """The function `name` as sh/supervisor.sh ships it (a one-liner, or through its closing `}` line)."""
    lines = SUP.split("\n")
    start = [i for i, line in enumerate(lines) if line.startswith(f"{name}() {{")]
    assert len(start) == 1, f"{name}: defined {len(start)} times in supervisor.sh"
    if "; }" in lines[start[0]]:
        return lines[start[0]]
    end = lines.index("}", start[0])
    return "\n".join(lines[start[0]:end + 1])


def _block(pattern: str) -> str:
    """The top-level lines of supervisor.sh a regex names (an assignment the functions read)."""
    m = re.search(pattern, SUP, re.M | re.S)
    assert m is not None, pattern
    return m.group(0)


def _script(script: str, stdin: str = "") -> str:
    """Run `script` from a file (a matrix outgrows a command line), stdin = `stdin`."""
    fd, path = tempfile.mkstemp(suffix=".sh")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(script)
        return subprocess.run([_bash(), path], input=stdin, capture_output=True, text=True, check=True).stdout
    finally:
        os.unlink(path)


def _run(body: str, *fns: str, stdin: str = "") -> str:
    return _script(_PRE + "\n".join(_fn(f) for f in fns) + "\n" + body, stdin)


def _q(value) -> str:
    return shlex.quote(value if isinstance(value, str) else json.dumps(value))


def _assoc(name: str, values: dict) -> str:
    pairs = " ".join(f"[{shlex.quote(k)}]={shlex.quote(str(v))}" for k, v in values.items())
    return f"unset {name}; declare -A {name}=({pairs})\n"


YOUNG_VARS = _block(r"^YOUNG_S=\d+\nYOUNG_JSON=.*?\nYOUNG_MAX=[^\n]*")


# ---- carry_head --------------------------------------------------------------------------------

BLINDS = ("missing", True, False, None, 0, "")


def _carry_state(blind) -> list[dict]:
    a = {"repo": "a"} if blind == "missing" else {"repo": "a", "blind": blind}
    return [a, {"repo": "b", "blind": True}]


def test_carry_head():
    cases = [(p, h, b, n) for p in ("", "a", "b") for h in ("", "a", "b") for b in BLINDS for n in (0, 9, 10, 11)]
    body = "BLIND_CARRY_MAX=10\nE=$(mktemp)\n" + "".join(
        f'o=$(carry_head {_q(p)} {_q(h)} {_q(_carry_state(b))} {n} 2>"$E"); d=0; [ -s "$E" ] && d=1; echo "$o $d"\n'
        for p, h, b, n in cases)
    got = _run(body, "carry_head").splitlines()
    want = []
    for p, h, b, n in cases:
        head, nb, dropped = tick.carry_head(p, h, _carry_state(b), n)
        want.append(f"{head} {nb} {int(dropped)}")
    assert got == want
    assert "a 10 0" in want and "- 0 1" in want and "b 0 0" in want  # every branch is in the matrix


# ---- young_table / young_charge / young_of ------------------------------------------------------

RENDERS = [
    {"pools": [{"repo": "a", "peak_mb": 900, "young_mb": 400}, {"repo": "b", "peak_mb": 1200},
               {"repo": "c", "peak_mb": 700, "young_mb": None}, {"repo": "d", "peak_mb": 600, "young_mb": False},
               {"repo": "e", "peak_mb": 500, "young_mb": 0}, {"repo": "f", "peak_mb": 500, "young_mb": "350"},
               {"repo": "g", "peak_mb": 500, "young_mb": 12.5}, {"repo": "h", "peak_mb": 500, "young_mb": -3},
               {"repo": "i", "peak_mb": 500, "young_mb": True}]},
    {"pools": []},
    {"pools": [{"repo": "a", "peak_mb": 0}]},
]
REPOS = ("a", "b", "c", "d", "e", "f", "g", "h", "i", "zz")


def test_young_charge(tmp_path):
    for k, render in enumerate(RENDERS):
        cfg = tmp_path / f"r{k}.json"
        cfg.write_text(json.dumps(render), encoding="utf-8")
        body = f"CONFIG={_q(cfg.as_posix())}\n{YOUNG_VARS}\necho \"max $YOUNG_MAX\"\n" + "".join(
            f"young_charge {r}\n" for r in REPOS)
        got = _run(body, "isnum", "young_charge").splitlines()
        table, ymax = tick.young_table(render)
        assert got == [f"max {ymax}"] + [str(tick.young_charge(table, ymax, r)) for r in REPOS], render


def _young_listing(rng: random.Random) -> tuple[str, list[str]]:
    lines, waived = [], []
    for _ in range(rng.randint(0, 6)):
        repo = rng.choice(("a", "b", "f", "zz"))
        epoch = rng.choice((str(NOW - rng.randint(-50, 400)), str(NOW - 300), str(NOW - 299), "0" + str(NOW - 10),
                            "abc", "", "1" * 13, "1"))
        name = rng.choice((f"r-{repo}-{epoch}-x{rng.randint(0, 99)}", f"{epoch}-y", "plain"))
        ws = rng.choice(("", " 0", f" {rng.randint(1, 900) * 1048576}", " 5000000000", " 12x", " 1048575"))
        lines.append(f"{repo} {name}{ws}")
        if rng.random() < 0.3:
            waived.append(name)
    return "\n".join(lines), waived


def test_young_of(tmp_path):
    cfg = tmp_path / "r.json"
    cfg.write_text(json.dumps(RENDERS[0]), encoding="utf-8")
    table, ymax = tick.young_table(RENDERS[0])
    rng = random.Random(7)
    cases = [(f"a r-a-{NOW - 10}-x\nb r-b-{NOW - 10}-x {300 * 1048576}\nzz r-zz-{NOW - 300}-x", [f"r-b-{NOW - 10}-x"]),
             ("", [])] + [_young_listing(rng) for _ in range(80)]
    body = f"CONFIG={_q(cfg.as_posix())}\n{YOUNG_VARS}\ndate() {{ echo {NOW}; }}\n"
    for listing, waived in cases:
        body += _assoc("YWAIVE", {n: 1 for n in waived}) + f"young_of {_q(listing)}; young_of {_q(listing)} waived\n"
    got = _run(body, "isnum", "young_charge", "young_of").splitlines()
    want = []
    for listing, waived in cases:
        want += [str(tick.young_of(listing, table, ymax, NOW, waived)),
                 str(tick.young_of(listing, table, ymax, NOW, waived, waived=True))]
    assert got == want
    assert want[:2] == ["400", "900"]  # a young charged 400; b's 1200 less its 300 working set, waived


# ---- refill_more / since_start / redecide -------------------------------------------------------

POOLS = ("a", "b", "c")


def _counts(rng: random.Random) -> dict:
    return {
        "TARGET": {r: rng.randint(0, 3) for r in POOLS if rng.random() < 0.7},
        "QUEUED": {r: rng.randint(0, 5) for r in POOLS},
        "IDLEN": {r: rng.randint(0, 3) for r in POOLS},
        "STARTED": {r: rng.randint(0, 3) for r in POOLS},
        "failed": [r for r in POOLS if rng.random() < 0.2],
    }


def test_refill_more():
    rng = random.Random(11)
    cases = [_counts(rng) for _ in range(200)]
    body = f"REPOS={_q(' '.join(POOLS))}\n"
    for c in cases:
        body += "".join(_assoc(k, c[k]) for k in ("TARGET", "QUEUED", "IDLEN", "STARTED"))
        body += f"refill_failed={_q(' ' + ''.join(r + ' ' for r in c['failed']))}\nrefill_more && echo 1 || echo 0\n"
    got = _run(body, "refill_more").splitlines()
    want = [str(int(tick.refill_more(POOLS, c["TARGET"], c["failed"], c["QUEUED"], c["IDLEN"], c["STARTED"])))
            for c in cases]
    assert got == want
    assert "0" in want and "1" in want


def test_since_start(tmp_path):
    raws = [None, "", f"{NOW - 90}\n", f"{NOW + 90}", f"x{NOW - 5}y", f"{NOW - 7}" + "x" * 15 + "999", "junk", f"{NOW}"]
    body = f"START_DIR={_q(tmp_path.as_posix())}\nnow={NOW}\ndate() {{ echo {NOW}; }}\n"
    for k, raw in enumerate(raws):
        if raw is not None:
            (tmp_path / f"p{k}").write_text(raw, encoding="utf-8", newline="")
        body += f"since_start p{k}\n"
    got = _run(body, "mark_start", "since_start").splitlines()
    assert got == [str(tick.since_start(raw, NOW)) for raw in raws]
    assert (tmp_path / "p0").read_text(encoding="utf-8").strip() == str(NOW)  # no file: its clock starts now


def _state(rng: random.Random) -> list[dict]:
    return [{"repo": r, "min": 0, "max": 4, "running": rng.randint(0, 3), "busy": rng.randint(0, 3),
             "idle": rng.randint(0, 2), "queued": rng.randint(0, 6), "oldest": rng.randint(0, 900),
             "since_start": rng.randint(0, 900), **({"blind": True} if rng.random() < 0.2 else {})} for r in POOLS]


def test_redecide_state_and_room(tmp_path):
    """redecide's input to the allocator -- the overlaid state and the live room -- with decide()
    stubbed to capture both (and fail, which ends the pass before any start)."""
    cfg = tmp_path / "r.json"
    cfg.write_text(json.dumps({"pools": [{"repo": "a", "peak_mb": 900, "young_mb": 400}, {"repo": "b", "peak_mb": 1200},
                                         {"repo": "c", "peak_mb": 700}]}), encoding="utf-8")
    table, ymax = tick.young_table(json.loads(cfg.read_text(encoding="utf-8")))
    rng = random.Random(5)
    body = (f"CONFIG={_q(cfg.as_posix())}\n{YOUNG_VARS}\nREPOS={_q(' '.join(POOLS))}\nnow={NOW}\nok=1\n"
            "decide() { printf '%s\\n%s\\n' \"$1\" \"$3\" >> \"${CAP}\"; return 1; }\n"
            "guest() { echo \"${MEM}\"; }\n")
    want = []
    for k in range(60):
        c, el, state = _counts(rng), rng.randint(0, 120), _state(rng)
        jobwait = {r: rng.randint(0, 600) for r in POOLS}
        passn = {r: rng.randint(0, 2) for r in POOLS}
        starts = {r: rng.choice((None, str(NOW - rng.randint(0, 900)))) for r in POOLS}
        listing = "\n".join(f"{r} r-{r}-{NOW - rng.randint(0, 600)}-{i}" for r in POOLS for i in range(rng.randint(0, 2)))
        floor, mem = rng.choice(("", "512")), rng.choice((str(8 * 2**30), "", "x"))
        sd = tmp_path / f"s{k}"
        sd.mkdir()
        for r, raw in starts.items():
            if raw is not None:
                (sd / r).write_text(raw, encoding="utf-8")
        body += "".join(_assoc(n, c[n]) for n in ("TARGET", "QUEUED", "IDLEN", "STARTED"))
        body += _assoc("JOBWAIT", jobwait) + _assoc("PASSN", passn) + _assoc("YWAIVE", {})
        body += (f"REDECIDE=1; RDSKIP=0; refill_failed=' '; START_DIR={_q(sd.as_posix())}; LIVE_FLOOR_MB={_q(floor)}; "
                 f"MEM={_q(mem)}; state={_q(state)}; new_head=a; CAP={_q((tmp_path / f'c{k}').as_posix())}\n"
                 f"date() {{ echo {NOW + el}; }}\nredecide {_q(listing)} 2>/dev/null; echo \"skip ${{RDSKIP}}\"\n")
        want.append((k, c, el, state, jobwait, passn, starts, listing, floor, mem))
    got = _run(body, "isnum", "names_of", "mark_start", "since_start", "young_charge", "young_of", "redecide")
    def wanted(c: dict) -> bool:  # redecide's own gate ignores refill_failed: refill_more with none failed
        return tick.refill_more(POOLS, c["TARGET"], (), c["QUEUED"], c["IDLEN"], c["STARTED"])

    # a configured floor with an unreadable MemAvailable skips the pass (counted), once there is demand
    assert got.splitlines() == [f"skip {int(bool(w[8]) and w[9] in ('', 'x') and wanted(w[1]))}" for w in want]
    decided = 0
    for k, c, el, state, jobwait, passn, starts, listing, floor, mem in want:
        cap = tmp_path / f"c{k}"
        if not wanted(c) or (floor and mem in ("", "x")):
            assert not cap.exists(), k  # nothing left the tick saw queued, or a blind live read: no decision
            continue
        decided += 1
        rstate, lf = cap.read_text(encoding="utf-8").replace("\r", "").splitlines()
        overlays = {}
        for r in POOLS:
            if r in c["TARGET"]:
                live = sum(1 for line in listing.split("\n") if line.split(" ")[0] == r)
                overlays[r] = tick.redecide_entry(live + passn[r], c["QUEUED"][r], c["IDLEN"][r], c["STARTED"][r],
                                                  jobwait[r], el, tick.since_start(starts[r], NOW + el))
        assert json.loads(rstate) == tick.redecide_state(state, overlays), k
        if floor:
            young = tick.young_of(listing, table, ymax, NOW + el)
            charges = {r: tick.young_charge(table, ymax, r) for r in POOLS}
            assert lf == str(tick.redecide_live_free(int(mem), int(floor), young, passn, charges)), k
        else:
            assert lf == "null", k
    assert decided > 10


# ---- secure_violations ---------------------------------------------------------------------------

NET = "ci-sealed-net"
CLEAN = {
    "Name": "/runner-1", "AppArmorProfile": "docker-default", "Mounts": [],
    "Config": {"User": "1001:1001"},
    "NetworkSettings": {"Networks": {NET: {}}},
    "HostConfig": {"Binds": None, "Mounts": None, "VolumesFrom": None, "CapDrop": ["ALL"], "CapAdd": None,
                   "SecurityOpt": ["no-new-privileges"], "NetworkMode": NET, "Privileged": False, "GroupAdd": None,
                   "Devices": [], "PidMode": "", "IpcMode": "private", "UsernsMode": "", "UTSMode": "",
                   "CgroupnsMode": "private", "Sysctls": None, "Runtime": "runc", "Memory": 2**31,
                   "MemorySwap": 2**31, "PidsLimit": 512, "NanoCpus": 2 * 10**9},
}
HOST_EDITS = [
    {"Binds": ["/:/host"]}, {"Mounts": [{"Type": "bind"}]}, {"VolumesFrom": ["x"]}, {"CapDrop": None},
    {"CapDrop": ["NET_RAW"]}, {"CapDrop": "NOTALL"}, {"CapDrop": "NET_RAW"}, {"CapAdd": ["SYS_ADMIN"]},
    {"SecurityOpt": None}, {"SecurityOpt": ["no-new-privileges:true", "seccomp=unconfined"]},
    {"SecurityOpt": ["no-new-privileges=true"]}, {"SecurityOpt": ["label=disable"]}, {"NetworkMode": "host"},
    {"Privileged": True}, {"Privileged": 0}, {"Privileged": None}, {"GroupAdd": ["docker"]},
    {"Devices": [{"PathOnHost": "/dev/kvm"}]}, {"PidMode": "host"}, {"IpcMode": "host"}, {"IpcMode": "none"},
    {"IpcMode": ""}, {"IpcMode": None}, {"UsernsMode": "host"}, {"UTSMode": "host"}, {"CgroupnsMode": "host"},
    {"CgroupnsMode": None}, {"Sysctls": {"net.ipv4.ip_forward": "1"}}, {"Runtime": "runsc"}, {"Runtime": None},
    {"Memory": 0}, {"Memory": None}, {"MemorySwap": -1}, {"MemorySwap": None}, {"PidsLimit": None},
    {"PidsLimit": -1}, {"NanoCpus": 0}, {"Memory": "2g", "MemorySwap": "2g"}, {"Memory": True, "MemorySwap": 1},
    {"Memory": 1, "MemorySwap": 1.0}, {"Binds": False, "Sysctls": False, "PidMode": False},
]
TOP_EDITS = [
    {"Mounts": [{"Type": "volume"}]}, {"Mounts": None}, {"AppArmorProfile": "unconfined"}, {"AppArmorProfile": None},
    {"Config": {"User": "root"}}, {"Config": {}}, {"Config": None}, {"NetworkSettings": {"Networks": {NET: {}, "bridge": {}}}},
    {"NetworkSettings": {"Networks": {}}}, {"NetworkSettings": {"Networks": None}}, {"NetworkSettings": None},
    {"Name": "no-slash"}, {"Name": None}, {"HostConfig": None}, {"HostConfig": {}},
]


def _secure_cases() -> list[dict]:
    cases = [CLEAN]
    for edit in HOST_EDITS:
        cases.append({**CLEAN, "HostConfig": {**CLEAN["HostConfig"], **edit}})
    for edit in TOP_EDITS:
        cases.append({**CLEAN, **edit})
    rng = random.Random(3)
    for _ in range(60):
        h = dict(CLEAN["HostConfig"])
        for edit in rng.sample(HOST_EDITS, rng.randint(2, 5)):
            h.update(edit)
        c = {**CLEAN, "HostConfig": h, **rng.choice(TOP_EDITS)}
        cases.append(c)
    return cases


def test_secure_violations():
    cases = _secure_cases()
    got = _run(f"SECURE_NET={_q(NET)}\nsecure_violations", "secure_violations",
               stdin="\n".join(json.dumps(c) for c in cases) + "\n").splitlines()
    want = tick.secure_violations(cases, NET)
    assert got == want
    assert tick.secure_violations([CLEAN], NET) == []
    assert want[0] == "runner-1 bind" and "no-slash" not in " ".join(want)  # a clean edit prints nothing
    assert len(want) > len(HOST_EDITS)


# ---- registry_probe_lines ------------------------------------------------------------------------

_C = 'registry_proxy_{}_total{{type="{}"}} {}'
PROBES = [
    "", "200 401", "200 401\n", "200 200\n", "502 000\n", "200 403\n", "20 401\n", "200 401 \n", "200 401\r\n",
    "abc def\n" + _C.format("hits", "blob", 1) + "\n",
    "200 401\n" + "\n".join([_C.format("requests", "blob", 12), _C.format("hits", "manifest", 2.5),
                             _C.format("misses", "blob", 3.5), _C.format("pulled_bytes", "blob", "1e3"),
                             _C.format("pushed_bytes", "manifest", "7E+2"), _C.format("hits", "blob", "1.5e2"),
                             _C.format("hits", "layer", 1), _C.format("bogus", "blob", 1), _C.format("hits", "blob", "-1"),
                             _C.format("hits", "blob", "1e-3"), " " + _C.format("hits", "blob", 1), "junk", ""]) + "\n",
    "200 401\n" + _C.format("hits", "blob", 4) + "\n" + _C.format("hits", "blob", 5),
    "200 401\n" + _C.format("hits", "blob", "123456789012345678901") + "\n",
]


def test_registry_probe_lines(tmp_path):
    body = "MP=ci_\n"
    for k, text in enumerate(PROBES):
        f = tmp_path / f"p{k}"
        f.write_bytes(text.encode("utf-8"))
        body += f"registry_probe_lines < {_q(f.as_posix())}; echo '--'\n"
    got = _run(body, "registry_probe_lines").split("--\n")[:-1]
    assert got == ["".join(line + "\n" for line in tick.registry_probe_lines(t, "ci_")) for t in PROBES]
    assert tick.registry_probe_lines(PROBES[1], "ci_") == []  # an unterminated line 1 is not read


# ---- stray_age (STRAY_JQ) ------------------------------------------------------------------------

LABEL = "ci.repo"


def _c(cid: str, started: str, labels) -> dict:
    return {"Id": cid, "State": {"StartedAt": started}, "Config": {"Labels": labels}}


STRAYS = [
    [],
    [_c("reg", "2026-01-01T00:00:00Z", None)],
    [_c("r1", "2026-11-28T10:00:00.123456789Z", {LABEL: "o/r"}), _c("s1", "2026-11-28T09:00:00.5Z", {}),
     _c("s2", "2026-11-28T08:00:00Z", {LABEL: ""}), _c("s3", "2026-11-28T09:30:00Z", {LABEL: None}),
     _c("s4", "2026-11-28T09:45:00Z", {LABEL: False}), _c("reg", "2020-01-01T00:00:00Z", None)],
    [_c("s5", "2026-11-28T11:00:00Z", None), {"Id": "s6", "State": {"StartedAt": "2026-11-28T10:59:00Z"}}],
    [_c("f1", "2099-01-01T00:00:00Z", {})],
]


def test_stray_age():
    now = 1_795_860_000  # 2026-11-28T10:00:00Z
    sjq = _block(r"^STRAY_JQ='.*?'$")
    body = f"L_REPO={_q(LABEL)}\n{sjq}\n" + "".join(
        f'jq -r --arg x reg --argjson now {now} "$STRAY_JQ" <<<{_q(s)}\n' for s in STRAYS)
    assert _run(body).splitlines() == [str(tick.stray_age(s, "reg", now, LABEL)) for s in STRAYS]
    assert tick.stray_age(STRAYS[2], "reg", now, LABEL) == 7200  # s2's two hours; the cache and r1 do not count


# ---- job_demand (runs.sh) ------------------------------------------------------------------------

JOBS = {"jobs": [
    {"status": "queued", "labels": ["self-hosted", "pool-a"], "created_at": "2026-11-28T09:00:00Z"},
    {"status": "queued", "labels": ["self-hosted", "pool-a", "big"], "created_at": "2026-11-28T08:00:00Z"},
    {"status": "in_progress", "labels": ["self-hosted", "pool-a"], "created_at": "2026-11-28T07:00:00Z"},
    {"status": "queued", "labels": ["ubuntu-latest"], "created_at": "2026-11-28T06:00:00Z"},
    {"status": "queued", "labels": ["pool-a"], "created_at": "2026-11-28T09:30:00Z"},
    {"status": "queued", "labels": [], "created_at": "2026-11-28T05:00:00Z"},
]}
DEMANDS = [(["self-hosted", "pool-a"], "pool-a"), (["self-hosted", "pool-a", "big"], "pool-a"),
           (["self-hosted", "pool-a", "big"], "big"), (["self-hosted"], "pool-a"), ([], "pool-a"),
           (["ubuntu-latest"], "ubuntu-latest"), (["self-hosted", "pool-a"], "self-hosted")]


def test_job_demand():
    now = 1_795_860_000
    script = _PRE + f". {_q((SH_DIR / 'runs.sh').as_posix())}\n" + "".join(
        f"job_demand {_q(JOBS)} {_q(pl)} {_q(need)} {now}\n" for pl, need in DEMANDS) + f"job_demand {_q({'jobs': []})} '[]' x {now}\n"
    got = _script(script).splitlines()
    want = [" ".join(map(str, tick.job_demand(JOBS, pl, need, now))) for pl, need in DEMANDS]
    assert got == want + [" ".join(map(str, tick.job_demand({"jobs": []}, [], "x", now)))]
    assert want[0] == "2 3600" and want[1] == "3 7200"
