"""The KIT: what a domain declares about its runner pools, apart from the capacity file itself.

The capacity file (`runners.yml`) holds the numbers an operator tunes: the host, its guests, each
pool's floor, ceiling and measured peak. The kit holds what the domain decides ONCE and the
library must not guess: which runner blocks exist, the supervisor instance each one drives (its
runner-name prefix and its isolation mode), the label family that marks its trust tier, the repo
variable its jobs read in `runs-on`, where each render is written, and the duties a shared host
reserve puts on its other owner.

WHY A SEPARATE FILE. Every supervisor reinstalls when the render's `source_sha256` changes, and
that hash covers the capacity file. Domain wiring kept beside the numbers would make a change to
the wiring indistinguishable from a change to the numbers; kept apart, the capacity file stays the
one thing whose edits move runners.

    generated: "by ci-runners from runners.yml -- DO NOT EDIT; run ci-runners --write"
    write_command: ci-runners --write --config runners.yml --kit runners-kit.yml
    source_hash: raw            # raw (default) | canonical -- see `source_hash` in render.py
    out_dir: generated          # where renders go, relative to --root (default ".")
    alloc_jq: generated/alloc.jq  # optional: --write copies the kit's allocator here, --check diffs it
    blocks:
      runners:                  # the capacity file's block key
        required: true
        instance: shared        # the supervisor instance this render drives
        prefix: ci-             # its runner-name prefix (unique: two instances sharing one reap each other)
        isolation: socket       # its docker-run shape, passed through to the supervisor
        label: ci               # the trust tier: `ci` or `ci-<class>` and no other tier's label
        variable: CI_RUNNER     # the repo variable a pool's jobs read in runs-on (its kill switch)
        output: runners.json
      sealed_runners:
        instance: sealed
        prefix: seal-
        isolation: sealed
        label: ci-sealed
        variable: CI_SEALED_RUNNER
        output: runners-sealed.json
        one_job_per_boot: true  # max_total must be 1 and the vm must be state on_demand
    share_duties:               # reserve -> the doc that builds its other owner and the text it must carry
      drill: {doc: docs/DRILL.md, text: "take the sealed hold first"}

Python callers build a `Kit` directly and may add `rules`: callables `(cfg, kit) -> [finding]`
run after every built-in check passes -- the extension point for an invariant a declaration cannot
express.
"""

from __future__ import annotations

import pathlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ci_kit.runners._checks import _unknown

HASH_MODES = ("raw", "canonical")
_KIT_KEYS = {"generated", "write_command", "source_hash", "out_dir", "alloc_jq", "blocks", "share_duties"}
_BLOCK_KEYS = {"required", "instance", "prefix", "isolation", "label", "variable", "output", "one_job_per_boot"}
_BLOCK_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
_LABEL = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_VARIABLE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_RESERVED = {"host", "guests"}

Rule = Callable[[dict, "Kit"], list[str]]


class KitError(ValueError):
    """The kit itself is malformed: the gate cannot judge the capacity file (exit 2)."""


@dataclass(frozen=True)
class Block:
    """One runner block: a key in the capacity file, one supervisor instance, one render."""

    key: str
    instance: str
    prefix: str
    isolation: str
    label: str
    variable: str
    output: str
    required: bool = False
    one_job_per_boot: bool = False


@dataclass(frozen=True)
class Duty:
    """A shared reserve's other owner must yield: `doc` (relative to --root) must carry `text`."""

    doc: str
    text: str


@dataclass(frozen=True)
class Kit:
    blocks: tuple[Block, ...]
    generated: str = "by ci-runners -- DO NOT EDIT; run ci-runners --write"
    write_command: str = "ci-runners --write"
    source_hash: str = "raw"
    out_dir: str = "."
    alloc_jq: str | None = None
    share_duties: Mapping[str, Duty] = field(default_factory=dict)
    rules: tuple[Rule, ...] = ()

    def __post_init__(self) -> None:
        problems = _kit_problems(self)
        if problems:
            raise KitError("; ".join(problems))

    def block(self, key: str) -> Block:
        return next(b for b in self.blocks if b.key == key)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(b.key for b in self.blocks)

    def tier(self, label: str) -> str | None:
        """The tier a runner label belongs to: the LONGEST block label it equals or extends with
        `-<class>` (so `ci-sealed-x` is ci-sealed, not ci), else None (a plain label like
        `self-hosted`). Every declared block's family counts, present in the file or not: a label
        of a tier the file does not run yet still marks a job as belonging to it."""
        best = None
        for b in self.blocks:
            if (label == b.label or label.startswith(b.label + "-")) and (best is None or len(b.label) > len(best)):
                best = b.label
        return best


def _kit_problems(kit: Kit) -> list[str]:
    p: list[str] = []
    if not kit.blocks:
        p.append("blocks: at least one runner block is required")
    if kit.source_hash not in HASH_MODES:
        p.append(f"source_hash must be one of {', '.join(HASH_MODES)}")
    for b in kit.blocks:
        if not _BLOCK_KEY.match(b.key) or b.key in _RESERVED:
            p.append(f"blocks.{b.key}: a block key is lower_snake_case and not host/guests")
        if not _LABEL.match(b.label):
            p.append(f"blocks.{b.key}.label must match {_LABEL.pattern}")
        if not _VARIABLE.match(b.variable):
            p.append(f"blocks.{b.key}.variable must match {_VARIABLE.pattern}")
        for k in ("instance", "prefix", "isolation", "output"):
            if not isinstance(getattr(b, k), str) or not getattr(b, k):
                p.append(f"blocks.{b.key}.{k} must be a non-empty string")
        for k in ("required", "one_job_per_boot"):
            if not isinstance(getattr(b, k), bool):
                p.append(f"blocks.{b.key}.{k} must be true or false")
    # two instances sharing a prefix delete each other's runners; two blocks sharing a label are one tier
    for k in ("key", "instance", "prefix", "label", "output"):
        vals = [getattr(b, k) for b in kit.blocks]
        if len(set(vals)) != len(vals):
            p.append(f"blocks: every block needs its own {k}")
    for name, d in kit.share_duties.items():
        if not (isinstance(d, Duty) and d.doc and d.text):
            p.append(f"share_duties.{name} needs a doc and the text it must carry")
    return p


def kit_from_dict(d: Any) -> Kit:
    """A `Kit` from its parsed YAML/JSON form. Raises KitError naming every problem."""
    p = _unknown("kit", d, _KIT_KEYS)
    if p:
        raise KitError("; ".join(p))
    blocks_in = d.get("blocks")
    if not isinstance(blocks_in, dict) or not blocks_in:
        raise KitError("blocks: at least one runner block is required")
    for key, b in blocks_in.items():
        p += _unknown(f"blocks.{key}", b, _BLOCK_KEYS)
        if isinstance(b, dict):
            missing = sorted({"instance", "prefix", "isolation", "label", "variable", "output"} - set(b))
            p += [f"blocks.{key}: missing {k!r}" for k in missing]
    duties_in = d.get("share_duties") or {}
    if not isinstance(duties_in, dict):
        p.append("share_duties: must be a mapping")
        duties_in = {}
    for name, x in duties_in.items():
        p += _unknown(f"share_duties.{name}", x, {"doc", "text"})
        if isinstance(x, dict):
            p += [f"share_duties.{name}: missing {k!r}" for k in sorted({"doc", "text"} - set(x))]
    if p:
        raise KitError("; ".join(p))
    blocks = tuple(Block(key=str(k), **b) for k, b in blocks_in.items())
    opt = {k: d[k] for k in ("generated", "write_command", "source_hash", "out_dir", "alloc_jq") if k in d}
    return Kit(blocks=blocks, share_duties={str(n): Duty(**x) for n, x in duties_in.items()}, **opt)


def load_kit(path: pathlib.Path) -> Kit:
    """Read a kit file (YAML; JSON is YAML). Raises KitError, OSError or yaml.YAMLError."""
    import yaml

    return kit_from_dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
