"""alloc.jq (what a runner VM runs) and ci_kit.runners.alloc.allocate (its Python twin) give the
SAME verdict on every input: hand-built shapes for each branch of the rule, plus a seeded random
matrix, all through ONE jq run over a stream of inputs.

A missing jq FAILS in CI (`$CI` set) and skips only on a workstation without it: a skipped
parity test reads exactly like a passing one.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess

import pytest

from ci_kit.runners.alloc import JQ_PATH, AllocError, allocate

KEYS = ("repo", "min", "max", "running", "busy", "idle", "queued", "oldest", "since_start")


def _jq() -> str:
    jq = shutil.which("jq")
    if jq is None:
        if os.environ.get("CI"):
            pytest.fail("jq not on PATH: the parity test cannot run (the supervisor needs it too)")
        pytest.skip("jq not on PATH (workstation)")
    return jq


def _jq_many(docs: list[dict]) -> list[dict]:
    out = subprocess.run([_jq(), "-c", "-f", str(JQ_PATH)], input="\n".join(json.dumps(d) for d in docs),
                         capture_output=True, text=True, check=True).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _doc(total, *pools, mem=10**9, peaks=None, **extra):
    return {"max_total": total, "mem_budget_mb": mem,
            "peaks": peaks if peaks is not None else {p[0]: 1 for p in pools},
            "pools": [dict(zip(KEYS, p, strict=False)) for p in pools], **extra}


HAND = {
    "quiet at floor": _doc(7, ("a", 2, 5, 2, 0, 2, 0, 0), ("b", 2, 5, 2, 0, 2, 0, 0)),
    "burst round-robin": _doc(5, ("a", 0, 5, 0, 0, 0, 4, 30), ("b", 0, 5, 0, 0, 0, 4, 10)),
    "reclaim idle surplus": _doc(3, ("a", 0, 3, 3, 0, 3, 0, 0), ("b", 0, 3, 0, 0, 0, 2, 50)),
    "memory holds the big pool": _doc(6, ("big", 0, 2, 0, 0, 0, 1, 100), ("small", 0, 4, 0, 0, 0, 3, 50),
                                      mem=4000, peaks={"big": 5000, "small": 1000}),
    "starved pool drains": _doc(4, ("big", 0, 2, 0, 0, 0, 1, 400), ("small", 0, 4, 3, 3, 0, 2, 50),
                                mem=6000, peaks={"big": 4000, "small": 1000}),
    "starved, backfill off": _doc(4, ("big", 0, 2, 0, 0, 0, 1, 400), ("small", 0, 4, 3, 3, 0, 2, 50),
                                  mem=6000, peaks={"big": 4000, "small": 1000}, backfill=False),
    "per-pool starve_s 0 reserves at once": _doc(4, ("big", 0, 2, 0, 0, 0, 1, 1), ("small", 0, 4, 3, 3, 0, 2, 50),
                                                 mem=6000, peaks={"big": 4000, "small": 1000}, starve_s={"big": 0}),
    "carried head picks first": _doc(4, ("big", 0, 2, 0, 0, 0, 1, 10), ("small", 0, 4, 0, 0, 0, 3, 500),
                                     mem=4000, peaks={"big": 4000, "small": 1000}, prev_head="big"),
    "blind carried head keeps its room": _doc(4, ("big", 0, 2, 0, 0, 0, 0, 0), ("small", 0, 4, 2, 2, 0, 3, 500),
                                              mem=5000, peaks={"big": 4000, "small": 1000}, prev_head="big",
                                              extra_blind="big"),
    "aged pool ranks first": _doc(2, ("a", 0, 2, 0, 0, 0, 1, 2000, 1500), ("b", 0, 2, 0, 0, 0, 1, 2500, 100),
                                  mem=1000, peaks={"a": 1000, "b": 1000}),
    "live cap binds": _doc(5, ("a", 0, 5, 1, 1, 0, 4, 30), mem=8000, peaks={"a": 1000}, mem_base_mb=4000,
                           live_free_mb=1500),
    "live read failed: base binds": _doc(5, ("a", 0, 5, 1, 1, 0, 4, 30), mem=8000, peaks={"a": 1000}, mem_base_mb=2500),
    "unknown repo charged the largest peak": _doc(4, ("gone", 0, 0, 1, 1, 0, 0, 0), ("a", 0, 4, 0, 0, 0, 3, 9),
                                                 mem=3000, peaks={"a": 500, "b": 2000}),
    "floor short of memory": _doc(4, ("a", 3, 4, 0, 0, 0, 0, 0), mem=2500, peaks={"a": 1000}),
    "no pools": _doc(3, mem=100, peaks={}),
}


def _materialise(d: dict) -> dict:
    d = json.loads(json.dumps(d))
    blind = d.pop("extra_blind", None)
    for p in d["pools"]:
        if p["repo"] == blind:
            p["blind"] = True
    return d


def _random_docs(n: int, seed: int = 20261127) -> list[dict]:
    rng = random.Random(seed)
    docs = []
    for _ in range(n):
        names = rng.sample(["a", "b", "c", "d", "e", "f"], rng.randint(1, 5))
        pools, peaks = [], {}
        for r in names:
            lo = rng.choice([0, 0, 0, 1, 2])
            busy = rng.randint(0, 3)
            idle = rng.randint(0, 2)
            p = {"repo": r, "min": lo, "max": rng.randint(max(lo, 1), 6), "running": busy + idle + rng.randint(0, 1),
                 "busy": busy, "idle": idle, "queued": rng.choice([0, 0, 1, 2, 5, 9]),
                 "oldest": rng.choice([0, 30, 299, 300, 900, 1500, 4000])}
            if rng.random() < 0.4:
                p["since_start"] = rng.choice([0, 600, 1200, 5000])
            if rng.random() < 0.05:
                p["blind"] = True
            pools.append(p)
            peaks[r] = rng.choice([256, 512, 1024, 2048, 3500, 6000])
        budget = rng.choice([2000, 4096, 8000, 12000, 20000])
        doc = {"max_total": rng.randint(1, 10), "mem_budget_mb": budget, "peaks": peaks, "pools": pools}
        if rng.random() < 0.5:
            doc["prev_head"] = rng.choice(names + ["zz"])
        if rng.random() < 0.3:
            doc["mem_base_mb"] = budget // 2
            if rng.random() < 0.7:
                doc["live_free_mb"] = rng.choice([-500, 0, 700, 3000, budget])
        if rng.random() < 0.2:
            doc["backfill"] = False
        if rng.random() < 0.3:
            doc["starve_s"] = {rng.choice(names): rng.choice([0, 60, 900])}
        docs.append(doc)
    return docs


def test_hand_shapes_agree():
    docs = [_materialise(d) for d in HAND.values()]
    for name, want, doc in zip(HAND, _jq_many(docs), docs, strict=True):
        assert allocate(doc) == want, name


def test_random_matrix_agrees():
    docs = _random_docs(3000)
    got = _jq_many(docs)
    assert len(got) == len(docs)
    drained = sum(1 for g in got if g["drain"])
    assert drained > 50, "the matrix must exercise the drain, or the parity proves little"
    for i, (doc, want) in enumerate(zip(docs, got, strict=True)):
        assert allocate(doc) == want, (i, doc)


@pytest.mark.parametrize("doc", [
    {"max_total": 2, "peaks": {}, "pools": []},
    {"max_total": 2, "mem_budget_mb": 100, "pools": []},
    {"max_total": 2, "mem_budget_mb": 100, "peaks": {"a": 0},
     "pools": [{"repo": "a", "min": 0, "max": 1, "running": 0, "busy": 0, "idle": 0, "queued": 1, "oldest": 0}]},
])
def test_both_refuse_the_same_inputs(doc):
    r = subprocess.run([_jq(), "-c", "-f", str(JQ_PATH)], input=json.dumps(doc), capture_output=True, text=True)
    assert r.returncode != 0
    with pytest.raises(AllocError):
        allocate(doc)
