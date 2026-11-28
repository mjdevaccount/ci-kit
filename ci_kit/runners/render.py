"""The render (one JSON per runner block) and the check that every committed render is fresh.

Each render is what ONE supervisor instance reads with jq -- its pools, labels, runner VM and
memory budget -- and what a provisioner reads for every guest's size. Renders are GENERATED: the
check fails when a committed copy differs from a fresh render, so a hand edit to a consumer's
input cannot drift from the capacity file.

`source_sha256` is what the supervisor publishes as its loaded config, so "merged but not applied"
is visible, and a change of it is what makes a supervisor reinstall. Two modes (the kit's
`source_hash`):

  raw        (default) the capacity file's TEXT. A comment-only edit changes it, so it reinstalls
             every supervisor -- harmless, but a reinstall nobody needed.
  canonical  the PARSED data (sorted-key JSON). Comments and formatting no longer reinstall
             anything. Switching modes changes every render's hash once, so turning it on is one
             deliberate reinstall of every instance, done when the operator chooses.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

from ci_kit.runners.budget import pools, vm_budget
from ci_kit.runners.kit import Kit


def source_hash(cfg: dict, source_text: str, mode: str = "raw") -> str:
    if mode == "canonical":
        data = json.dumps(cfg, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        return hashlib.sha256(data.encode("utf-8")).hexdigest()[:12]
    if mode != "raw":
        raise ValueError(f"unknown source_hash mode {mode!r}")
    return hashlib.sha256(source_text.encode("utf-8")).hexdigest()[:12]


def render(cfg: dict, source_text: str, block: str, kit: Kit) -> str:
    """The JSON text for `block`'s supervisor instance (the file is valid: `validate` first)."""
    vm_name, meta, r = cfg[block]["vm"], kit.block(block), cfg[block]
    doc = {
        "_generated": kit.generated,
        "source_sha256": source_hash(cfg, source_text, kit.source_hash),
        "guests": {n: {"vmid": g["vmid"], "cores": g["cores"], "ram_mb": g["ram_mb"],
                       **({"state": g["state"]} if g.get("state", "running") != "running" else {}),
                       **({"budgeted": False} if g.get("budgeted") is False else {})}
                   for n, g in sorted(cfg["guests"].items()) if not g.get("planned")},
        # the host RAM budget the guests were checked against: published, so rules can hold the
        # DECLARED MemTotal and reserves to the measured ones
        "host": {"ram_mb": cfg["host"]["ram_mb"], "reserves_mb": dict(cfg["host"].get("reserves_mb") or {})},
        # the supervisor instance this file drives: its runner-name prefix and its docker-run shape
        "instance": meta.instance, "prefix": meta.prefix, "isolation": meta.isolation,
        "runner_vm": {"name": vm_name, "vmid": cfg["guests"][vm_name]["vmid"]},
        "max_total": r["max_total"],
        **vm_budget(cfg, block),
        # each kill switch is rendered only when OFF (the default renders nothing)
        **({"backfill": False} if r.get("backfill") is False else {}),
        **({"redecide": False} if r.get("redecide") is False else {}),
        "pools": pools(cfg, kit, block),
    }
    return json.dumps(doc, indent=2, sort_keys=False) + "\n"


def outputs(cfg: dict, kit: Kit, out_dir: pathlib.Path) -> dict[str, pathlib.Path]:
    """block -> its render path, for each runner block the file declares."""
    return {b.key: out_dir / b.output for b in kit.blocks if b.key in cfg}


def freshness(cfg: dict, text: str, kit: Kit, *, root: pathlib.Path, out_dir: pathlib.Path,
              config_name: str) -> list[str]:
    """Stale or orphaned renders, a stale allocator copy, and unmet share duties (file valid)."""
    from ci_kit.runners.alloc import JQ_PATH

    findings = []
    for block, path in outputs(cfg, kit, out_dir).items():
        have = path.read_text(encoding="utf-8") if path.exists() else ""
        if have != render(cfg, text, block, kit):
            findings.append(f"{path.name} is stale: run {kit.write_command}")
    # a shared reserve has a second owner that must yield: the duty is written where it is built
    for name, g in cfg["guests"].items():
        duty = kit.share_duties.get(g.get("shares"))
        if duty is None:
            continue
        doc = root / duty.doc
        if not doc.is_file() or duty.text not in doc.read_text(encoding="utf-8"):
            findings.append(f"guests.{name} shares {g['shares']}, but {duty.doc} does not carry the "
                            f"duty {duty.text!r} (its other owner must yield to it)")
    # a render for a block the file no longer declares would be installed and run stale
    for b in kit.blocks:
        path = out_dir / b.output
        if b.key not in cfg and path.exists():
            findings.append(f"{path.name} exists but {config_name} has no {b.key}: delete it")
    if kit.alloc_jq:
        path = root / kit.alloc_jq
        if not path.exists() or path.read_bytes() != JQ_PATH.read_bytes():
            findings.append(f"{kit.alloc_jq} is not ci-kit's allocator: run {kit.write_command}")
    return findings
