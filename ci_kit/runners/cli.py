"""ci-runners: validate a self-hosted runner capacity file, render its supervisor JSON, check it.

    ci-runners --check --config ci/runners.yml --kit ci/runners-kit.yml   # CI: schema + budgets + fresh renders
    ci-runners --write --config ci/runners.yml --kit ci/runners-kit.yml   # regenerate every render
    ci-runners --write ... --out-dir /tmp/r      # render elsewhere (shadow a hand-rolled renderer, then diff)
    ci-runners --alloc < tick.json               # one admission verdict (alloc.jq's twin) on stdin's input
    ci-runners --verify-prom x.prom --render r.json   # did a tick publish every series its alerts read?
    ci-runners --selftest                        # both directions on built inputs

`--root` (default: `$CI_KIT_ROOT`, else the git toplevel) anchors the kit's `out_dir`, `alloc_jq`,
`scripts_dir` and share-duty docs; `--out-dir` overrides the kit's `out_dir`. Exit 0 clean, 1 findings or stale
renders, 2 could not look (unreadable or unparsable files, a malformed kit, a missing extra).
`--verify-prom` reads one supervisor `.prom` against the render it was installed with
(`ci_kit.runners.verify_prom`): 0 every requirement met, 1 one is not, 2 could not check.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from ci_kit import site_lint
from ci_kit.runners.alloc import JQ_PATH, AllocError, allocate
from ci_kit.runners.budget import pools
from ci_kit.runners.kit import KitError, load_kit
from ci_kit.runners.render import extras, freshness, outputs, render
from ci_kit.runners.schema import validate
from ci_kit.runners.scripts import scripts_sha256, write_scripts
from ci_kit.runners.verify_prom import VerifyError, verify_files

PROG = "ci-runners"


def _blind(msg: str) -> int:
    print(f"{PROG}: CANNOT CHECK -- {msg}", file=sys.stderr)
    return 2


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=PROG, description=(__doc__ or "").split("\n", 1)[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    for m in ("--check", "--write", "--alloc", "--selftest"):
        mode.add_argument(m, action="store_true")
    mode.add_argument("--verify-prom", type=pathlib.Path, metavar="PROM", help="a supervisor's .prom file")
    ap.add_argument("--render", type=pathlib.Path, help="with --verify-prom: the render that supervisor reads")
    ap.add_argument("--config", type=pathlib.Path, help="the capacity file (runners.yml)")
    ap.add_argument("--kit", type=pathlib.Path, help="the kit: blocks, renders, share duties")
    ap.add_argument("--root", type=pathlib.Path, default=None)
    ap.add_argument("--out-dir", type=pathlib.Path, default=None)
    return ap


def main(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    if a.selftest:
        from ci_kit.runners.selftest import selftest

        return site_lint.selftest_exit(PROG, selftest)
    if a.alloc:
        return _alloc()
    if a.verify_prom is not None:
        return _verify_prom(a.verify_prom, a.render)
    if a.config is None or a.kit is None:
        return _blind("--check and --write need --config and --kit")
    try:
        import yaml
    except ImportError:
        return _blind("pyyaml is not installed: install ci-kit[runners]")
    root = site_lint.gate_root(a.root)
    try:
        kit = load_kit(a.kit)
        text = a.config.read_text(encoding="utf-8")
        cfg = yaml.safe_load(text) or {}
    except (OSError, yaml.YAMLError, KitError, TypeError) as e:
        return _blind(f"{type(e).__name__}: {e}")
    try:
        return _run(a, kit, cfg, text, root)
    except (TypeError, AttributeError, KeyError, OSError) as e:
        return _blind(f"{type(e).__name__}: {e}")


def _run(a: argparse.Namespace, kit, cfg: dict, text: str, root: pathlib.Path) -> int:
    out_dir = a.out_dir if a.out_dir is not None else root / kit.out_dir
    name = a.config.name
    findings = [f"{name}: {x}" for x in validate(cfg, kit)]
    if findings:
        return _report(findings)
    if a.write:
        out_dir.mkdir(parents=True, exist_ok=True)
        for block, path in outputs(cfg, kit, out_dir).items():
            path.write_text(render(cfg, text, block, kit), encoding="utf-8", newline="\n")
            print(f"{PROG}: wrote {path} ({len(pools(cfg, kit, block))} pools)")
        for path, body in extras(cfg, text, kit, root=root, config_name=name, flat_dir=a.out_dir).items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body.encode("utf-8"))
            print(f"{PROG}: wrote {path}")
        if kit.alloc_jq:
            (root / kit.alloc_jq).parent.mkdir(parents=True, exist_ok=True)
            (root / kit.alloc_jq).write_bytes(JQ_PATH.read_bytes())
            print(f"{PROG}: wrote {kit.alloc_jq} (ci-kit's allocator)")
        if kit.scripts_dir:
            written = write_scripts(root / kit.scripts_dir)
            print(f"{PROG}: wrote {len(written)} scripts to {kit.scripts_dir} (scripts_sha256 {scripts_sha256()})")
        return 0
    findings = freshness(cfg, text, kit, root=root, out_dir=out_dir, config_name=name, flat_dir=a.out_dir)
    counts = ", ".join(f"{b}: {len(pools(cfg, kit, b))} pools" for b in kit.keys if b in cfg)
    return _report(findings, counts)


def _report(findings: list[str], counts: str = "") -> int:
    for x in findings:
        print(f"{PROG}: {x}", file=sys.stderr)
    print(f"{PROG}: {counts + ', ' if counts else ''}{len(findings)} finding(s)")
    return 1 if findings else 0


def _alloc() -> int:
    try:
        doc = json.loads(sys.stdin.read())
        print(json.dumps(allocate(doc), separators=(",", ":")))
    except (ValueError, KeyError, TypeError) as e:
        # AllocError is a ValueError: the input jq would refuse with error(...)
        kind = "refused" if isinstance(e, AllocError) else "unreadable input"
        return _blind(f"{kind}: {e}")
    return 0


def _verify_prom(prom: pathlib.Path, render: pathlib.Path | None) -> int:
    if render is None:
        return _blind("--verify-prom needs --render")
    try:
        findings, notes = verify_files(prom, render)
    except VerifyError as e:
        return _blind(str(e))
    for n in notes:
        print(f"{PROG}: NOTE {n}", file=sys.stderr)
    return _report(findings)


if __name__ == "__main__":
    sys.exit(main())
