#!/usr/bin/env python3
"""ci-guard-selfproof -- every guard must be able to demonstrate that it can FAIL. Shrink-only.

WHY THIS EXISTS. An adversarial audit (2026-08-17) found three ratchets that could not fire: a
warning budget that ran a bare `npx` and reported "0 warning(s), baseline 54", a route lint blind
to 25 of 98 routes, and a lint ruleset sitting outside the CI change filter. All three were green
and all three passed human review. A passing check and a check that cannot fail are byte-identical
from the outside, which is why review does not catch it and CI actively hides it. Each was fixed by
a one-off mutation (break the protected thing, watch the guard go red, restore); nothing stopped
the fourth instance. This makes the question standing.

THE CONTRACT. A guard script may define a module-level `selftest() -> list[str]` returning
human-readable failures (empty list = proved). It must prove BOTH directions on inputs it builds
itself: a known-bad input it rejects, and a known-good one it accepts. One direction is not
enough -- "always fails" and "always passes" are both broken, and only the pair tells a working
guard from either. A JavaScript guard (`--js`) proves itself by handling `--selftest`: exit 0 when
proved, non-zero with the reasons on stdout.

Scripts without one are grandfathered in a baseline file that may only SHRINK. A NEW guard with no
selftest fails. Bulk-retrofitting a dozen selftests in one PR would produce a dozen unread
selftests -- the defect this exists to catch, wearing a hat.

THE RESULT SHAPE IS LOAD-BEARING. The result is read for TRUTHINESS and then JOINED as strings, so
a guard returning an exit code satisfies neither half: `0` is falsy and reads as PROVEN (as does a
guard returning `0` having checked nothing), and a failing `2` is truthy and crashes the join, so
the one run that mattered names no guard at all. Anything but `list[str]` is a failure by name.

THE LOADER REGISTERS THE MODULE IN `sys.modules` BEFORE `exec_module`. `dataclasses._is_type`
does `sys.modules.get(cls.__module__).__dict__` with no None guard, so a guard declaring a
`@dataclass` raised AttributeError here and was reported as "could not be imported": the checker
blaming the guard for its own loader. The registration SURVIVES a successful import (a dataclass
built inside `selftest()` looks the module up again); only a failed exec is unregistered.

Guards are found from `git ls-files`, never a filesystem walk: a walk sees your untracked files,
CI does not. Nothing is assumed about your repo: the scripts dir, name prefixes, baseline path and
whether JS guards exist are all flags.

Usage:
    ci-guard-selfproof                       prove every guard under scripts/ (prefix lint_)
    ci-guard-selfproof --prefix lint --prefix check --js
    ci-guard-selfproof --scripts tools --baseline tools/selfproof.txt
    ci-guard-selfproof --write-baseline      re-freeze the un-proven set
    ci-guard-selfproof @args.txt             read flags from a file, one per line

Exit codes: 0 every guard proved or baselined · 1 a guard failed, threw, broke the contract, could
not be imported, or is NEW and unproven · 2 DID NOT FINISH LOOKING (no git, no scripts dir, zero
guards found, or `node` missing while JS guards exist).
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import pathlib
import subprocess
import sys
import types

from ci_kit import discover
from ci_kit.ratchet import RatchetSet

HEADER = (
    "# guard self-proof baseline - guard scripts that cannot yet demonstrate their own\n"
    "# failure. SHRINK ONLY: a NEW guard with no selftest fails CI. Remove a line here by\n"
    "# writing a selftest that proves BOTH directions - rejects a known-bad input, accepts a\n"
    "# known-good one. Regenerate: ci-guard-selfproof --write-baseline\n"
)
JS_SUFFIXES = (".mjs", ".js")
NODE = "node"


class CouldNotLook(Exception):
    """Exit 2: the run cannot vouch for anything."""


def find_guards(root: pathlib.Path, scripts: str, prefixes: tuple[str, ...], js: bool,
                exclude: frozenset[str] = frozenset()) -> tuple[list[pathlib.Path], list[pathlib.Path]]:
    """(python guards, js guards) tracked directly under `scripts`, sorted by name.

    A guard is a file whose name starts with one of `prefixes` and NOT with `_` (a private helper
    module is not a guard). ONE prefix rule for every language: Python guards are `lint_*.py`
    while JS ones are often `lint-*.mjs` / `check_*.mjs`, and two separate globs drift the moment
    someone names a file the other way. Type declarations (`.d.ts`, `.d.mts`) are never executed.
    """
    rels = discover.tracked([scripts], suffix="", root=root)
    py, jsg = [], []
    for rel in rels:
        if rel.rsplit("/", 1)[0] != scripts.strip("/"):
            continue                          # directly under scripts/ only, as a glob would
        name = rel.rsplit("/", 1)[-1]
        if name in exclude or name.startswith("_") or not name.startswith(prefixes):
            continue
        p = root / rel
        if name.endswith(".py"):
            py.append(p)
        elif js and name.endswith(JS_SUFFIXES) and not name.endswith((".d.mts", ".d.ts")):
            jsg.append(p)
    return sorted(py, key=lambda p: p.name), sorted(jsg, key=lambda p: p.name)


def load(path: pathlib.Path) -> types.ModuleType:
    """Import by path, registered BEFORE exec (module docstring). Guard scripts are all
    `if __name__ == '__main__'`-gated, so importing one runs no checks and touches nothing.
    The guard's own directory is on sys.path during exec so its sibling imports resolve."""
    name = f"_guard_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise ImportError(f"cannot load {path.name}")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    finally:
        sys.path.pop(0)
    return mod


def contract_violation(name: str, result: object) -> str | None:
    """The failure text when `result` is not the contract's `list[str]`, else None."""
    if not isinstance(result, list) or not all(isinstance(x, str) for x in result):
        return (f"{name}: selftest() returned {result!r}, not the contract's list[str] of "
                f"failure strings. A falsy non-list (0, '', None) would read as PROVEN, so "
                f"the guard would be counted green without anything having been checked.")
    return None


def prove_py(path: pathlib.Path) -> tuple[bool, list[str]]:
    """(has_selftest, failures) for one Python guard."""
    try:
        mod = load(path)
    except Exception as e:  # noqa: BLE001 - an unimportable guard is a broken guard
        return True, [f"{path.name}: could not be imported ({e})"]
    fn = getattr(mod, "selftest", None)
    if not callable(fn):
        return False, []
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001 - a selftest that throws proves nothing
        return True, [f"{path.name}: selftest() raised {e!r}"]
    bad = contract_violation(path.name, result)
    if bad:
        return True, [bad]
    if result:
        return True, [f"{path.name}: selftest reported:\n    " + "\n    ".join(result)]
    return True, []


def prove_js(path: pathlib.Path, cwd: pathlib.Path) -> tuple[bool, list[str]]:
    """(has_selftest, failures) for one JS guard. "Has one" is read from the source -- the file
    handles `--selftest` -- so a guard without it is BASELINED as unproven, not run and crashed."""
    if "--selftest" not in path.read_text(encoding="utf-8", errors="replace"):
        return False, []
    try:
        r = subprocess.run([NODE, str(path), "--selftest"], capture_output=True, text=True,
                           timeout=120, cwd=str(cwd), check=False, encoding="utf-8",
                           errors="replace")
    except FileNotFoundError as e:
        raise CouldNotLook(f"`{NODE}` not found -- {path.name} and every other JS guard could "
                           f"not be proved") from e
    except subprocess.TimeoutExpired:
        return True, [f"{path.name}: selftest timed out"]
    if r.returncode != 0:
        detail = (r.stdout + r.stderr).strip() or f"exit {r.returncode}"
        return True, [f"{path.name}: selftest reported:\n    " + detail.replace("\n", "\n    ")]
    return True, []


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ci-guard-selfproof", fromfile_prefix_chars="@",
                                description="every guard proves it can fail; shrink-only baseline")
    p.add_argument("--root", type=pathlib.Path, help="checkout to gate (default: git toplevel of cwd)")
    p.add_argument("--scripts", default="scripts", help="dir holding the guards (default scripts)")
    p.add_argument("--baseline", help="baseline file (default <scripts>/guard_selfproof_baseline.txt)")
    p.add_argument("--prefix", action="append", help="guard name prefix, repeatable (default lint_)")
    p.add_argument("--exclude", action="append", default=[], help="a file name that is not a guard")
    p.add_argument("--js", action="store_true", help="also prove .mjs/.js guards via node --selftest")
    p.add_argument("--write-baseline", action="store_true", help="re-freeze the un-proven set")
    return p


def run(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    env = os.environ.get("CI_KIT_ROOT")
    root = (a.root or (pathlib.Path(env) if env else discover.repo_root())).resolve()
    if not (root / a.scripts).is_dir():
        raise CouldNotLook(f"no {a.scripts}/ under {root}")
    # For the WHOLE run, not just each exec: a guard run as `python scripts/x.py` has its own
    # directory at sys.path[0], and a selftest that imports a sibling lazily relies on that.
    sys.path.insert(0, str(root / a.scripts))
    prefixes = tuple(a.prefix or ["lint_"])
    py, jsg = find_guards(root, a.scripts, prefixes, a.js, frozenset(a.exclude))
    if not py and not jsg:
        raise CouldNotLook(f"found ZERO guards ({'/'.join(prefixes)}*) tracked under "
                           f"{root / a.scripts} -- a guard-of-guards that saw no guard has "
                           f"verified nothing")
    ratchet = RatchetSet(root / (a.baseline or f"{a.scripts}/guard_selfproof_baseline.txt"),
                         header=HEADER)

    unproven: set[str] = set()
    failures: list[str] = []
    proven = 0
    results = [prove_py(p) for p in py] + [prove_js(p, root) for p in jsg]
    for path, (has, errs) in zip(py + jsg, results, strict=True):
        if not has:
            unproven.add(path.name)
        elif errs:
            failures.extend(errs)
        else:
            proven += 1

    if a.write_baseline:
        n = ratchet.write(unproven)
        print(f"baseline written: {n} guard(s) without a selftest")
        return 0

    new = ratchet.regressions(unproven)
    if new:
        failures.append(
            "NEW guard(s) with no selftest: " + ", ".join(new) + "\n"
            "    A guard nobody has watched fail is indistinguishable from one that cannot.\n"
            "    Prove BOTH directions -- a known-bad input it rejects, and a known-good one it\n"
            "    accepts -- on inputs the guard builds itself:\n"
            "      .py   `def selftest() -> list[str]`  (empty list = proved)\n"
            "      .mjs  handle `--selftest`, exit 0 when proved, non-zero + reasons on stdout")
    shrunk = sorted(ratchet.load() - unproven)
    if shrunk:
        print(f"guard self-proof: baseline can shrink, no longer unproven: {', '.join(shrunk)} "
              f"(re-freeze with --write-baseline)")

    if failures:
        print("guard self-proof FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print(f"guard self-proof: OK ({proven} guard(s) prove themselves, "
          f"{len(unproven)} baselined as unproven)")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(argv)
    except CouldNotLook as e:
        print(f"guard self-proof: DID NOT FINISH LOOKING (exit 2): {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
