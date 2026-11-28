"""Self-hosted GitHub Actions runner capacity: schema, budgets, render, check, and admission.

The MECHANISM is here; the domain supplies its specifics in two files:

  * the CAPACITY file (`runners.yml`): the host, its guests, and each runner block's pools with
    their floors, ceilings and MEASURED peaks -- the numbers an operator tunes;
  * the KIT (`ci_kit.runners.kit`): which runner blocks exist, the supervisor instance each one
    drives, its trust-tier label, the variable its jobs read in `runs-on`, where renders go, and
    the duties a shared host reserve puts on its other owner.

`validate` returns every way the capacity file is wrong; `render` writes one JSON per block for a
supervisor to read with jq; `freshness` says whether the committed renders are current; and
`allocate` is the runtime admission rule (`alloc.jq`, which the supervisor runs, is its twin).
Needs the `runners` extra (pyyaml) only to READ YAML files; every function here takes parsed data.
"""

from ci_kit.runners.alloc import JQ_PATH, AllocError, allocate
from ci_kit.runners.budget import derived_ceiling, pools, vm_budget
from ci_kit.runners.kit import Block, Duty, Kit, KitError, kit_from_dict, load_kit
from ci_kit.runners.render import freshness, outputs, render, source_hash
from ci_kit.runners.schema import validate

__all__ = [
    "JQ_PATH", "AllocError", "Block", "Duty", "Kit", "KitError", "allocate", "derived_ceiling",
    "freshness", "kit_from_dict", "load_kit", "outputs", "pools", "render", "source_hash",
    "validate", "vm_budget",
]
