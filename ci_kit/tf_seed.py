"""ci-tf-seed -- fill terraform's provider cache from the lock file, without GitHub's download path.

WHY (2026-11-08). A cold `terraform init` fetches each provider's SHA256SUMS, .sig and zip from
wherever the registry points, for bpg/proxmox `github.com/.../releases/download/...`. That endpoint
504'd for every repo for over an hour; terraform retries once and fails ("failed to retrieve
authentication checksums", home-host job 128699260134). A WARM init does not touch it: measured
with TF_LOG=TRACE, a cache holding the locked version makes init contact the registry only.
So this fills the cache first, through `release_bin.fetch` (retry + the REST asset fallback).

WHAT IT DOES. For each `provider` block in `.terraform.lock.hcl`: if
`$TF_PLUGIN_CACHE_DIR/<host>/<ns>/<type>/<version>/<os>_<arch>/` exists, nothing. Otherwise ask the
registry's download endpoint (service discovery, then `.../<version>/download/<os>/<arch>`) for the
zip URL and its sha256, fetch, refuse a sha256 mismatch, unpack beside the target and rename in
(a reader sees no entry or the whole one). With no TF_PLUGIN_CACHE_DIR it uses
`$RUNNER_TEMP/tf-plugin-cache` and exports it through `$GITHUB_ENV`, so a hosted runner gets the
same protection for that job.

TRUST. The registry's sha256 judges the download here; terraform (>= 1.4) then uses a cached
provider ONLY when its hash is in the committed lock file, so a wrong entry is refused, not run.
The registry signature (.sig) is NOT checked here: the lock file's hash is the review gate.

EXIT  0 seeded or already cached · 1 a sha256 mismatch · 2 did not finish (no lock file, a
fetch that failed after the retries, a registry answer it cannot read).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import pathlib
import platform
import re
import shutil
import sys
import tempfile
import urllib.error
import zipfile

from ci_kit import release_bin

GATE = "ci-tf-seed"
PROVIDER = re.compile(r'provider\s+"([^"/]+)/([^"/]+)/([^"/]+)"\s*\{(.*?)\n\}', re.S)
VERSION = re.compile(r'^\s*version\s*=\s*"([^"]+)"', re.M)


def locked(lock_text: str) -> list[tuple[str, str, str, str]]:
    """(host, namespace, type, version) per provider block in a .terraform.lock.hcl."""
    out = []
    for host, ns, typ, body in PROVIDER.findall(lock_text):
        m = VERSION.search(body)
        if m:
            out.append((host, ns, typ, m.group(1)))
    return out


def tf_platform() -> tuple[str, str]:
    osname = "linux" if sys.platform.startswith("linux") else {"darwin": "darwin", "win32": "windows"}.get(sys.platform, sys.platform)
    m = platform.machine().lower()
    return osname, {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(m, m)


def _json(url: str) -> dict:
    try:
        data = json.loads(release_bin.fetch(url, gate=GATE))
    except ValueError as exc:
        release_bin.blind(GATE, f"{url}: not JSON ({exc})")
    if not isinstance(data, dict):
        release_bin.blind(GATE, f"{url}: not a JSON object")
    return data


def download_meta(host: str, ns: str, typ: str, version: str, osname: str, arch: str) -> tuple[str, str]:
    """(zip url, sha256) from the registry, via its service discovery document."""
    base = _json(f"https://{host}/.well-known/terraform.json").get("providers.v1")
    if not isinstance(base, str):
        release_bin.blind(GATE, f"{host} names no providers.v1 service")
    if not base.startswith("http"):
        base = f"https://{host}{base}"
    meta = _json(f"{base.rstrip('/')}/{ns}/{typ}/{version}/download/{osname}/{arch}")
    url, sha = meta.get("download_url"), meta.get("shasum")
    if not (isinstance(url, str) and isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha)):
        release_bin.blind(GATE, f"{host}/{ns}/{typ} {version}: registry gave no download_url/shasum")
    return url, sha


def unpack(blob: bytes, target: pathlib.Path) -> None:
    """Unzip into a temp dir beside `target`, then rename it in; a peer that won the race wins."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = pathlib.Path(tempfile.mkdtemp(dir=target.parent, prefix=f".{target.name}."))
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            for info in zf.infolist():
                dest = (tmp / info.filename).resolve()
                if not dest.is_relative_to(tmp.resolve()):
                    raise OSError(f"zip member escapes the cache: {info.filename}")
                zf.extract(info, tmp)
                if not info.is_dir() and pathlib.PurePath(info.filename).name.startswith("terraform-provider-"):
                    os.chmod(dest, 0o755)
        os.replace(tmp, target)
    except OSError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        if not target.is_dir():
            release_bin.blind(GATE, f"could not unpack into {target}: {exc}")
    except zipfile.BadZipFile as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        release_bin.blind(GATE, f"not a zip: {exc}")


def cache_dir() -> pathlib.Path:
    env = os.environ.get("TF_PLUGIN_CACHE_DIR")
    if env:
        return pathlib.Path(env)
    runner_temp = os.environ.get("RUNNER_TEMP")
    if not runner_temp:
        release_bin.blind(GATE, "no TF_PLUGIN_CACHE_DIR and no RUNNER_TEMP to put one in")
    cache = pathlib.Path(runner_temp) / "tf-plugin-cache"
    gh_env = os.environ.get("GITHUB_ENV")
    if gh_env:
        with open(gh_env, "a", encoding="utf-8") as fh:
            fh.write(f"TF_PLUGIN_CACHE_DIR={cache}\n")
    return cache


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog=GATE, description=__doc__.split("\n", 1)[0])
    ap.add_argument("--dir", default=".", help="the terraform root holding .terraform.lock.hcl")
    a = ap.parse_args(argv)
    lock = pathlib.Path(a.dir) / ".terraform.lock.hcl"
    try:
        providers = locked(lock.read_text(encoding="utf-8"))
    except OSError as exc:
        release_bin.blind(GATE, f"no lock file: {exc}")
    if not providers:
        release_bin.blind(GATE, f"{lock} locks no provider -- nothing to seed is not the same as seeded")
    cache, (osname, arch) = cache_dir(), tf_platform()
    for host, ns, typ, version in providers:
        target = cache / host / ns / typ / version / f"{osname}_{arch}"
        name = f"{host}/{ns}/{typ} {version} {osname}_{arch}"
        if target.is_dir() and any(target.iterdir()):
            print(f"{GATE}: {name} from cache {target}", file=sys.stderr)
            continue
        url, want = download_meta(host, ns, typ, version, osname, arch)
        print(f"{GATE}: {name} not in cache; fetching {url}", file=sys.stderr)
        try:
            blob = release_bin.fetch(url, gate=GATE)
        except urllib.error.URLError as exc:  # fetch blinds on its own; belt and braces
            release_bin.blind(GATE, f"{url}: {exc}")
        got = hashlib.sha256(blob).hexdigest()
        if got != want:
            print(f"{GATE}: {url}: sha256 {got} != registry's {want} -- REFUSED", file=sys.stderr)
            return 1
        unpack(blob, target)
        print(f"{GATE}: {name} seeded into {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
