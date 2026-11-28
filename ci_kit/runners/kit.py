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
    supervisor: {...}           # optional: the supervisors' own wiring -- supervisor.py
    renders:                    # extra GENERATED files keyed on the same source_sha256 (optional)
      expected-config:
        output: monitoring/runner-config.yml   # relative to --root (--out-dir: its basename there)
        template: |             # string.Template: ${source_sha256}, ${config}; $$ is a literal $
          # GENERATED from ${config} -- DO NOT EDIT
          expected_source_sha256: "${source_sha256}"

`renders` is how a consumer of the hash that is NOT a supervisor -- an alert rule holding every
installed supervisor's published hash to main's, a dashboard variable -- is generated from the
SAME text as the JSONs, so it cannot name a different hash. --check holds each to a fresh render.

Python callers build a `Kit` directly and may add `rules`: callables `(cfg, kit) -> [finding]`
run after every built-in check passes -- the extension point for an invariant a declaration cannot
express.
"""

from __future__ import annotations

import pathlib
import re
import string
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ci_kit.runners._checks import _unknown
from ci_kit.runners.supervisor import (
    BlockSupervisor,
    Supervisor,
    block_problems,
    block_supervisor_from_dict,
    supervisor_from_dict,
    supervisor_problems,
)

HASH_MODES = ("raw", "canonical")
_KIT_KEYS = {"generated", "write_command", "source_hash", "out_dir", "alloc_jq", "blocks", "share_duties",
             "renders", "supervisor"}
# what a `renders` template may name: the hash every JSON carries, and the capacity file's name
RENDER_VARS = ("source_sha256", "config")
_BLOCK_KEYS = {"required", "instance", "prefix", "isolation", "label", "variable", "output", "one_job_per_boot",
               "supervisor"}
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
    # this instance's own supervisor wiring (supervisor.py); None = the render carries none
    supervisor: BlockSupervisor | None = None


@dataclass(frozen=True)
class Duty:
    """A shared reserve's other owner must yield: `doc` (relative to --root) must carry `text`."""

    doc: str
    text: str


@dataclass(frozen=True)
class Render:
    """An extra generated file: `template` (string.Template over RENDER_VARS) written to `output`."""

    name: str
    output: str
    template: str

    def text(self, **values: str) -> str:
        return string.Template(self.template).substitute(values)


@dataclass(frozen=True)
class Kit:
    blocks: tuple[Block, ...]
    generated: str = "by ci-runners -- DO NOT EDIT; run ci-runners --write"
    write_command: str = "ci-runners --write"
    source_hash: str = "raw"
    out_dir: str = "."
    alloc_jq: str | None = None
    share_duties: Mapping[str, Duty] = field(default_factory=dict)
    renders: tuple[Render, ...] = ()
    rules: tuple[Rule, ...] = ()
    # the wiring every supervisor instance shares (supervisor.py); None = renders carry none
    supervisor: Supervisor | None = None

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
    if not isinstance(kit.out_dir, str) or not kit.out_dir:
        p.append("out_dir must be a non-empty string")
    if kit.alloc_jq is not None and (not isinstance(kit.alloc_jq, str) or not kit.alloc_jq):
        p.append("alloc_jq must be a non-empty string")
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
    return p + _render_problems(kit) + _supervisor_problems(kit)


def _supervisor_problems(kit: Kit) -> list[str]:
    p = supervisor_problems(kit.supervisor) if kit.supervisor is not None else []
    wired = [b for b in kit.blocks if b.supervisor is not None]
    for b in wired:
        p += block_problems(b.key, b.supervisor, kit.supervisor)
    # two instances sharing a state dir, a metrics file or an image tag overwrite each other's
    for k in ("state_dir", "prom_file", "image"):
        vals = [getattr(b.supervisor, k) for b in wired]
        if len(set(vals)) != len(vals):
            p.append(f"blocks: every supervisor needs its own {k}")
    return p


def _render_problems(kit: Kit) -> list[str]:
    p: list[str] = []
    for r in kit.renders:
        if not isinstance(r.output, str) or not r.output:
            p.append(f"renders.{r.name}.output must be a non-empty string")
        if not isinstance(r.template, str) or not r.template:
            p.append(f"renders.{r.name}.template must be a non-empty string")
            continue
        t = string.Template(r.template)
        if not t.is_valid():
            p.append(f"renders.{r.name}.template: a bare $ (write $$ for a literal $)")
            continue
        bad = sorted(set(t.get_identifiers()) - set(RENDER_VARS))
        if bad:
            p.append(f"renders.{r.name}.template names {', '.join(bad)}; it may name {', '.join(RENDER_VARS)}")
    # --out-dir writes every render under its basename, so two must never share one
    names = [pathlib.PurePosixPath(r.output).name for r in kit.renders if isinstance(r.output, str)]
    names += [pathlib.PurePosixPath(b.output).name for b in kit.blocks if isinstance(b.output, str)]
    if len(set(names)) != len(names):
        p.append("renders: every render and block output needs its own file name")
    if len({r.name for r in kit.renders}) != len(kit.renders):
        p.append("renders: every render needs its own name")
    return p


def kit_from_dict(d: Any) -> Kit:
    """A `Kit` from its parsed YAML/JSON form. Raises KitError naming every problem."""
    p = _unknown("kit", d, _KIT_KEYS)
    if p:
        raise KitError("; ".join(p))
    blocks_in = d.get("blocks")
    if not isinstance(blocks_in, dict) or not blocks_in:
        raise KitError("blocks: at least one runner block is required")
    wiring: dict[str, BlockSupervisor] = {}
    for key, b in blocks_in.items():
        p += _unknown(f"blocks.{key}", b, _BLOCK_KEYS)
        if isinstance(b, dict):
            missing = sorted({"instance", "prefix", "isolation", "label", "variable", "output"} - set(b))
            p += [f"blocks.{key}: missing {k!r}" for k in missing]
            if "supervisor" in b:
                ws, wp = block_supervisor_from_dict(str(key), b["supervisor"])
                p += wp
                if ws is not None:
                    wiring[str(key)] = ws
    sup = None
    if d.get("supervisor") is not None:
        sup, sp = supervisor_from_dict(d["supervisor"])
        p += sp
    duties_in = d.get("share_duties") or {}
    if not isinstance(duties_in, dict):
        p.append("share_duties: must be a mapping")
        duties_in = {}
    for name, x in duties_in.items():
        p += _unknown(f"share_duties.{name}", x, {"doc", "text"})
        if isinstance(x, dict):
            p += [f"share_duties.{name}: missing {k!r}" for k in sorted({"doc", "text"} - set(x))]
    renders_in = d.get("renders") or {}
    if not isinstance(renders_in, dict):
        p.append("renders: must be a mapping")
        renders_in = {}
    for name, x in renders_in.items():
        p += _unknown(f"renders.{name}", x, {"output", "template"})
        if isinstance(x, dict):
            p += [f"renders.{name}: missing {k!r}" for k in sorted({"output", "template"} - set(x))]
    if p:
        raise KitError("; ".join(p))
    blocks = tuple(Block(key=str(k), **{**b, "supervisor": wiring.get(str(k))}) for k, b in blocks_in.items())
    opt = {k: d[k] for k in ("generated", "write_command", "source_hash", "out_dir", "alloc_jq") if k in d}
    return Kit(blocks=blocks, share_duties={str(n): Duty(**x) for n, x in duties_in.items()},
               renders=tuple(Render(name=str(n), **x) for n, x in renders_in.items()), supervisor=sup, **opt)


def load_kit(path: pathlib.Path) -> Kit:
    """Read a kit file (YAML; JSON is YAML). Raises KitError, OSError or yaml.YAMLError."""
    import yaml

    return kit_from_dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
