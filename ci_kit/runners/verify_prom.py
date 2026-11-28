"""`ci-runners --verify-prom`: did a supervisor tick publish every series its alerts read?

An install that moves the supervisor must prove the NEW script looked, not that a file exists: a
rule over a series nobody publishes is a guard that cannot fire, and a supervisor that crashes after
it publishes leaves a fresh, complete-looking file every tick. This reads one `.prom` file
(`sh/supervisor.sh`'s textfile output) against the RENDER that supervisor was installed with -- the
file it reads, so every requirement is derived from what was shipped, never from a list of pools
kept here:

  * only samples labelled ``supervisor="<render instance>"`` count (another instance's file, or an
    older script that did not label, never satisfies one);
  * ``runner_collect_ok 1``; ``runner_config_info{source_sha256="<render's>"} 1`` (the LOADED
    config, not the copy); ``runner_max_total <render's max_total>`` (the allocator ran);
  * every per-tick sweep gauge and every gauge an alert reads (``REQUIRED``), as a non-negative
    integer;
  * per pool of the render: its target, its memory hold and its declared peak; the working-set
    maximum only for a pool the render keeps a floor in (``min >= 1``: only a LIVE runner is
    sampled -- no floor pool at all is said, not silently passed);
  * a ``secure`` render: the isolation conditions, exactly (``SECURE``), and at least one container
    inspected (zero violations over zero containers is a pass on nothing);
  * a ``socket`` render with a ``registry_cache``: the cache's gauges and the daemon's mirror on it
    (the protection is the mirror). The cache down, or switched off on purpose, is a NOTE.

Exit 0 every requirement met, 1 one is not (each printed), 2 could not check (an unreadable file,
a render with no supervisor section, an isolation this script does not run).

prometheus_client's text parser was checked: not adopted, because it would be this extra's first
dependency beyond YAML, to read a format this package's own script writes in one fixed shape
(``name{k="v",...} value``); the checks are line-exact, the shape an installer's grep had.
"""

from __future__ import annotations

import json
import pathlib
import re

SAMPLE = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{(.*)\})?[ \t]+(\S+)$")
LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')

# every series an alert or a per-tick sweep reads, without the metric prefix
REQUIRED = (
    "runner_vm_oom_kills_total", "runner_vm_boot_time_seconds", "runner_vm_stray_oldest_seconds",
    "runner_vm_volumes_bytes", "runner_vm_volumes", "runner_vm_volumes_pruned_total",
    "runner_vm_overlay2_bytes", "runner_vm_overlay2_measure_ok", "runner_vm_image_prune_ok",
    "runner_vm_image_prune_reported_bytes_total", "github_ratelimit_remaining",
    "runner_mem_budget_bytes", "runner_mem_committed_bytes",
)
PER_POOL = ("runner_target", "runner_mem_held", "runner_peak_declared_bytes")
SECURE = (("runner_secure_icc_disabled", 1), ("runner_secure_vm_hardened", 1), ("runner_vm_userns_max", 0),
          ("runner_vm_foreign_containers", 0), ("runner_isolation_violations", 0),
          ("runner_isolation_start_violations", 0))
REGISTRY = ("registry_disabled", "registry_cache_up", "registry_upstream_ok", "registry_mirror_configured",
            "registry_cache_bytes", "registry_cache_max_bytes")


class VerifyError(ValueError):
    """The inputs cannot be checked (exit 2), as distinct from a requirement not met (exit 1)."""


def samples(text: str, instance: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], str]:
    """(name, sorted other labels) -> value, for every sample labelled supervisor=<instance>."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        m = SAMPLE.match(line)
        if not m or line.startswith("#"):
            continue
        labels = dict(LABEL.findall(m.group(2) or ""))
        if labels.pop("supervisor", None) != instance:
            continue
        out[(m.group(1), tuple(sorted(labels.items())))] = m.group(3)
    return out


def _int(v: str | None) -> int | None:
    return int(v) if v is not None and re.fullmatch(r"[0-9]+", v) else None


def _shown(name: str, labels: dict[str, str]) -> str:
    return name + ("{" + ",".join(f'{k}="{x}"' for k, x in sorted(labels.items())) + "}" if labels else "")


def verify(text: str, render: dict) -> tuple[list[str], list[str]]:
    """(findings, notes) for one .prom file's text against the render its supervisor reads."""
    try:
        inst, iso, sup = render["instance"], render["isolation"], render["supervisor"]
        mp, sha, total = sup["metric_prefix"], render["source_sha256"], render["max_total"]
        pools = [(p["repo"], p.get("min", 0)) for p in render["pools"]]
    except (KeyError, TypeError) as e:
        raise VerifyError(f"the render has no complete supervisor section ({type(e).__name__}: {e})") from e
    if iso not in ("socket", "secure"):
        raise VerifyError(f"isolation {iso!r}: sh/supervisor.sh runs socket and secure instances only")
    got = samples(text, inst)
    if not got:
        return [f'no sample carries supervisor="{inst}": not this instance\'s file, or not its script'], []
    findings: list[str] = []
    notes: list[str] = []

    def val(name: str, **labels: str) -> str | None:
        return got.get((mp + name, tuple(sorted(labels.items()))))

    def want(name: str, expect: int | None = None, **labels: str) -> None:
        have = val(name, **labels)
        v = _int(have)
        if v is None:
            findings.append(f"{_shown(mp + name, labels)}: absent or not a non-negative integer (have {have!r})")
        elif expect is not None and v != expect:
            findings.append(f"{_shown(mp + name, labels)}: want {expect}, have {v}")

    want("runner_collect_ok", 1)
    want("runner_config_info", 1, source_sha256=sha)
    want("runner_max_total", total)
    for name in REQUIRED:
        want(name)
    for repo, floor in pools:
        for name in PER_POOL:
            want(name, repo=repo)
        if floor >= 1:
            want("runner_mem_working_set_max_bytes", repo=repo)
    if not any(floor >= 1 for _, floor in pools):
        notes.append("no pool keeps a floor: the working-set gauge is not checked (only a live runner is sampled)")
    if iso == "secure":
        for name, expect in SECURE:
            want(name, expect)
        if (_int(val("runner_isolation_checked")) or 0) < 1:
            findings.append(f"{mp}runner_isolation_checked: want >= 1 (no container inspected: a pass on nothing)")
    elif sup.get("registry_cache"):
        if _int(val("registry_disabled")) == 1:
            notes.append("the registry cache is OFF (its kill switch): every pull goes upstream direct")
        else:
            for name in REGISTRY:
                want(name)
            want("registry_mirror_configured", 1)
            if _int(val("registry_cache_up")) != 1:
                notes.append(f"the registry cache is not answering (upstream_ok {val('registry_upstream_ok')}):"
                             " pulls go upstream direct until it is")
    return findings, notes


def verify_files(prom: pathlib.Path, render: pathlib.Path) -> tuple[list[str], list[str]]:
    """`verify` over two files; VerifyError when either cannot be read."""
    try:
        text = prom.read_text(encoding="utf-8")
        doc = json.loads(render.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise VerifyError(f"{type(e).__name__}: {e}") from e
    if not isinstance(doc, dict):
        raise VerifyError(f"{render}: not a JSON object")
    return verify(text, doc)
