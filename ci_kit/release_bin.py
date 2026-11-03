"""release_bin -- a third-party CLI pinned by version AND sha256, fetched once into a cache.

WHY ONE MODULE. jscpd (ci-dup-code) was the first pinned release binary in the kit and carried its
own fetch; oasdiff (ci-api-diff, 2026-11-03) is the second. The fetch is the part that must not
drift between them -- platform naming, the sha256 refusal, the cache layout, the could-not-look
exit -- so it lives here and each gate states only its `Pin`.

THE PIN. `version` plus one sha256 per platform asset, copied from the release's own checksums
file. Each gate's version line carries a `# renovate: datasource=github-releases depName=...`
comment, which edge-ci's renovate preset reads; Renovate moves the VERSION only, so a bump PR
fails its own selftest with `sha256 X != pinned Y` (exit 2) until the hashes move in the same
PR -- a new binary never runs on an unreviewed digest.

THE CACHE. `$CI_KIT_CACHE_DIR` (default `~/.cache/ci-kit`) / `<name>-<version>-<platform>/<exe>`,
so a version bump is a new directory, never an overwrite. `$<ENV>` (e.g. `CI_KIT_OASDIFF`) points
a gate at a binary you already have and skips the fetch.

EXIT. Every failure to obtain the binary is `<gate>: DID NOT FINISH LOOKING -- ...` and exit 2:
a gate whose tool could not be fetched has looked at nothing.
"""

from __future__ import annotations

import hashlib
import io
import os
import pathlib
import platform
import sys
import tarfile
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Pin:
    """One release binary. `url` takes `{v}` (version) and `{asset}` (the platform's asset token)."""

    name: str
    version: str
    url: str
    assets: Mapping[str, str]  # canonical platform ("linux-x64") -> the release's own token
    sha256: Mapping[str, str]  # canonical platform -> sha256 of that tarball
    env: str  # override: a path to an existing binary


def blind(gate: str, msg: str) -> None:
    print(f"{gate}: DID NOT FINISH LOOKING -- {msg}", file=sys.stderr)
    raise SystemExit(2)


def canonical_platform() -> str:
    """`linux-x64`, `linux-arm64`, `darwin-x64`, `darwin-arm64`, `windows-x64`, `windows-arm64`."""
    m = platform.machine().lower()
    arch = "arm64" if m in ("arm64", "aarch64") else "x64"
    osname = {"linux": "linux", "darwin": "darwin", "win32": "windows"}.get(
        "linux" if sys.platform.startswith("linux") else sys.platform, "")
    return f"{osname}-{arch}" if osname else f"{sys.platform}-{arch}"


def binary(pin: Pin, *, gate: str) -> pathlib.Path:
    """The pinned binary: `$<pin.env>` if set, else the cached release, fetched and verified once."""
    override = os.environ.get(pin.env)
    if override:
        return pathlib.Path(override)
    plat = canonical_platform()
    asset = pin.assets.get(plat)
    if asset is None:
        blind(gate, f"no {pin.name} {pin.version} release for {plat} (set {pin.env} to a local binary)")
    cache = pathlib.Path(os.environ.get("CI_KIT_CACHE_DIR") or pathlib.Path.home() / ".cache" / "ci-kit")
    exe = cache / f"{pin.name}-{pin.version}-{asset}" / (pin.name + (".exe" if plat.startswith("windows") else ""))
    if exe.exists():
        return exe
    url = pin.url.format(v=pin.version, asset=asset)
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 - pinned https release
            blob = resp.read()
    except OSError as exc:
        blind(gate, f"could not fetch {url}: {exc}")
    digest = hashlib.sha256(blob).hexdigest()
    want = pin.sha256.get(plat)
    if digest != want:
        blind(gate, f"{url}: sha256 {digest} != pinned {want} (a version bump moves every hash in the same edit)")
    exe.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        member = next((m for m in tar.getmembers() if m.name.rsplit("/", 1)[-1] == exe.name), None)
        if member is None:
            blind(gate, f"{url}: no {exe.name} in the tarball")
        fh = tar.extractfile(member)
        assert fh is not None
        exe.write_bytes(fh.read())
    if not plat.startswith("windows"):
        exe.chmod(0o755)
    return exe
