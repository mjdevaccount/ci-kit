"""pin_rehash -- move a `release_bin.Pin`'s sha256 table to the version its line now names (a heal-ladder tier-2 generator).

WHY (ci-kit#79/#80, 2026-11-14). Renovate moves a pinned release binary's VERSION line (the
`# renovate: datasource=github-releases` comment) and cannot move the six sha256 hashes beside it:
hosted Renovate runs no postUpgradeTasks, and no datasource digests one release's per-platform
assets as a table. So every jscpd / oasdiff / gitleaks bump arrived red (`sha256 X != pinned Y`,
exit 2) and waited for a hand push of the hashes. That is a generator's job, not a person's.

WHAT. For each `Pin(...)` in a Python file: resolve its version (a literal or a module constant),
fetch every platform's asset (`url.format(v=, asset=)`) through `release_bin.fetch` -- the same
retry and REST fallback the gate's own fetch uses -- hash it, and rewrite each sha256 literal in
place. Nothing else in the file moves. Hashing the asset itself, not a checksums file, is the same
bytes `release_bin.binary` will verify and needs no per-project checksums naming.

TRUST. The hash now follows what the release serves at bump time (trust on first use), exactly as
Renovate's version bump already does; the diff is reviewed by the review gate like any push, and
after merge the hash is frozen again, so a release re-tagged later is still refused. `only_hashes`
(pure) is what the heal pusher holds such a file to: same lines, every change a 64-hex token.

THE HEAL WIRING (ci_kit/pr_regen.py). A red PR whose own diff changes a file that declares a Pin
gets `python3 -m ci_kit.pin_rehash --since <base> <file>` as a built-in red-mode generator, no caller
map needed; only the pins whose version differs from the base's copy are touched (`bumped`).

EXIT 0 = every pin current (rewritten or already right); 2 = an asset could not be fetched
(`release_bin.blind`: did not finish looking); 1 = a file names a Pin this cannot read.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ci_kit import release_bin

GATE = "pin-rehash"
HEX = re.compile(r"\b[0-9a-f]{64}\b")
DECLARES = re.compile(r"(?:\brelease_bin\.|\b)Pin\(")


@dataclass(frozen=True)
class Found:
    name: str
    version: str
    url: str
    assets: dict[str, str]
    hashes: dict[str, tuple[str, int, int]]   # platform -> (old hex, line index, column of the literal)


def declares(text: str) -> bool:
    return bool(DECLARES.search(text))


def _const(node: ast.AST, consts: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


def pins(text: str) -> list[Found]:
    """Every Pin(...) call in `text`. Pure. A Pin whose fields are not literals raises ValueError:
    a table this cannot read must not be reported as current."""
    tree = ast.parse(text)
    consts = {t.id: n.value.value for n in tree.body if isinstance(n, ast.Assign)
              and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)
              for t in n.targets if isinstance(t, ast.Name)}
    out: list[Found] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) == "Pin"):
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        name, version, url = (_const(kw.get(k, ast.Constant(None)), consts) for k in ("name", "version", "url"))
        assets, table = kw.get("assets"), kw.get("sha256")
        if not (name and version and url and isinstance(assets, ast.Dict) and isinstance(table, ast.Dict)):
            raise ValueError(f"line {node.lineno}: a Pin whose name/version/url/assets/sha256 are not literals")
        amap = {_const(k, consts): _const(v, consts) for k, v in zip(assets.keys, assets.values, strict=True) if k}
        hashes = {}
        for k, v in zip(table.keys, table.values, strict=True):
            plat = _const(k, consts) if k else None
            if not plat or not isinstance(v, ast.Constant) or not isinstance(v.value, str) or not HEX.fullmatch(v.value):
                raise ValueError(f"line {node.lineno}: {name}'s sha256 table holds a non-literal or non-hex entry")
            hashes[plat] = (v.value, v.lineno - 1, v.col_offset)
        if set(hashes) - {k for k in amap if k}:
            raise ValueError(f"{name}: a hash for a platform with no asset: {sorted(set(hashes) - set(amap))}")
        out.append(Found(name, version, url, {str(k): str(v) for k, v in amap.items() if k and v}, hashes))
    return out


def rewrite(text: str, pin: Found, new: dict[str, str]) -> str:
    """`text` with each of the pin's hash literals replaced by `new[platform]`. Pure."""
    lines = text.splitlines(keepends=True)
    for plat, (old, i, col) in pin.hashes.items():
        if plat in new and new[plat] != old:
            line = lines[i]
            at = line.index(old, col)
            lines[i] = line[:at] + new[plat] + line[at + len(old):]
    return "".join(lines)


def only_hashes(old: str, new: str) -> bool:
    """True when `new` differs from `old` ONLY in 64-hex tokens, line for line, and differs at all. Pure:
    the bound the heal pusher holds a rehashed source file to."""
    a, b = old.splitlines(), new.splitlines()
    if len(a) != len(b) or a == b:
        return False
    return all(x == y or HEX.sub("#", x) == HEX.sub("#", y) for x, y in zip(a, b, strict=True))


Fetch = Callable[..., bytes]


def digests(pin: Found, fetch: Fetch = release_bin.fetch) -> dict[str, str]:
    """platform -> sha256 of that platform's asset at the pin's version (one fetch per distinct asset)."""
    seen: dict[str, str] = {}
    out: dict[str, str] = {}
    for plat in pin.hashes:
        asset = pin.assets.get(plat)
        if asset is None:
            continue
        url = pin.url.format(v=pin.version, asset=asset)
        if url not in seen:
            seen[url] = hashlib.sha256(fetch(url, gate=GATE)).hexdigest()
        out[plat] = seen[url]
    return out


def bumped(text: str, before: str | None) -> set[str]:
    """The pins in `text` whose version differs from `before`'s copy (None = no copy: every pin). Pure.
    Only those are rehashed: a pin whose version did NOT move keeps its frozen hashes, so a release
    re-tagged under the same version is still refused by the gate, never "healed" into the pin."""
    old = {p.name: p.version for p in pins(before)} if before else {}
    return {p.name for p in pins(text) if before is None or old.get(p.name) != p.version}


def rehash(path: Path, fetch: Fetch = release_bin.fetch, before: str | None = None) -> list[str]:
    """Rewrite the digests of `path`'s pins whose version moved since `before` (None = all of them).
    Returns what moved (one line per pin)."""
    text = path.read_text(encoding="utf-8")
    moved: list[str] = []
    todo = bumped(text, before)
    for pin in [p for p in pins(text) if p.name in todo]:
        new = digests(pin, fetch)
        changed = sorted(p for p, h in new.items() if pin.hashes[p][0] != h)
        if changed:
            text = rewrite(text, pin, new)   # a hash is 64 chars both ways: later positions hold
            moved.append(f"{path}: {pin.name} {pin.version}: {len(changed)} hash(es) moved ({', '.join(changed)})")
    if moved:
        path.write_bytes(text.encode("utf-8"))
    return moved


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ci_kit.pin_rehash", description=__doc__.split("\n", 1)[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--since", default="", help="a git rev: rehash only pins whose version differs from its copy")
    a = ap.parse_args(argv)
    rc = 0
    for f in a.files:
        try:
            before = None
            if a.since:
                r = subprocess.run(["git", "show", f"{a.since}:{f}"], capture_output=True, text=True, encoding="utf-8")
                before = r.stdout if r.returncode == 0 else ""      # a file new since then: every pin moved
            said = rehash(Path(f), before=before)
        except (ValueError, SyntaxError, OSError) as e:
            print(f"{GATE}: {f}: {e}", file=sys.stderr)
            rc = 1
            continue
        print("\n".join(said) if said else f"{GATE}: {f}: every pin already matches its version")
    return rc


if __name__ == "__main__":
    sys.exit(main())
