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

SHARED CACHE (2026-11-08). The home CI runners point `$CI_KIT_CACHE_DIR` at one directory every
runner mounts (home-host ci-runner-supervisor.sh), so two jobs can fetch the same pin at once. The
binary is written to a temp file beside it and `os.replace`d in: a reader sees no binary or the whole
one, never a half-written file that `exists()` already calls cached.

RETRY. A cold cache fetches from GitHub releases, which answered 504 twice running on edge-host#589
(2026-11-08). A 5xx, a timeout or a dropped connection is retried `FETCH_ATTEMPTS` times with
backoff; a 4xx is not (a wrong URL stays wrong). The two GitHub download paths fail APART -- the
`releases/download` URL 504'd on three jobs and a curl while the REST asset URL served the same bytes -- so each
failed attempt also tries the API path (`_via_api`). Stdlib only: the kit's core takes no dependency.

EXIT. Every failure to obtain the binary is `<gate>: DID NOT FINISH LOOKING -- ...` and exit 2:
a gate whose tool could not be fetched has looked at nothing. That holds after the retries too.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import pathlib
import platform
import re
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass

FETCH_ATTEMPTS = 3
FETCH_BACKOFF_S = (5.0, 15.0)  # sleep before attempt 2, attempt 3
GITHUB_WEB = "https://github.com"
GITHUB_API = "https://api.github.com"


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
        print(f"{gate}: {pin.name} {pin.version} from cache {exe}", file=sys.stderr)
        return exe
    url = pin.url.format(v=pin.version, asset=asset)
    print(f"{gate}: {pin.name} {pin.version} not in cache {cache}; fetching {url}", file=sys.stderr)
    blob = fetch(url, gate=gate)
    digest = hashlib.sha256(blob).hexdigest()
    want = pin.sha256.get(plat)
    if digest != want:
        blind(gate, f"{url}: sha256 {digest} != pinned {want} (a version bump moves every hash in the same edit)")
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        member = next((m for m in tar.getmembers() if m.name.rsplit("/", 1)[-1] == exe.name), None)
        if member is None:
            blind(gate, f"{url}: no {exe.name} in the tarball")
        fh = tar.extractfile(member)
        assert fh is not None
        data = fh.read()
    _install(exe, data, gate=gate, executable=not plat.startswith("windows"))
    return exe


def _install(exe: pathlib.Path, data: bytes, *, gate: str, executable: bool) -> None:
    """Write `data` to `exe` atomically. A failed write is blind (exit 2), never a traceback (exit 1):
    in a shared cache a full disk, a wipe mid-fetch or (Windows) a peer running the binary we are
    replacing is likelier than in a private one. If a peer already landed the binary, use theirs."""
    tmp: str | None = None
    try:
        exe.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=exe.parent, prefix=f".{exe.name}.")
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        if executable:
            os.chmod(tmp, 0o755)
        os.replace(tmp, exe)
    except OSError as exc:
        if tmp is not None:
            pathlib.Path(tmp).unlink(missing_ok=True)
        if not exe.exists():
            blind(gate, f"could not write {exe}: {exc}")


_FetchError = (OSError, http.client.HTTPException)  # HTTPException: IncompleteRead on a cut body


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    return True  # URLError, timeout, reset, a truncated body: the network, not the request


#: env names read for the API path's token, first set wins. The action passes the job's own
#: github.token as CI_KIT_GITHUB_TOKEN; GITHUB_TOKEN / GH_TOKEN cover a caller that exports one.
TOKEN_ENVS = ("CI_KIT_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")


def _token() -> str:
    return next((os.environ[e] for e in TOKEN_ENVS if os.environ.get(e)), "")


def _get(url: str, accept: str | None = None, *, auth: bool = False) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": accept} if accept else {})
    # api.github.com only, whatever URL the release JSON hands back (review 2026-11-08)
    tok = _token() if auth and url.startswith(GITHUB_API + "/") else ""
    if tok:
        # UNREDIRECTED: urllib copies req.headers onto a redirect but never unredirected_hdrs, so the
        # token reaches api.github.com and never the signed blob host the asset URL redirects to.
        req.add_unredirected_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - pinned https release
        return resp.read()


def _via_api(url: str) -> bytes | None:
    """The same asset through the REST API, or None when `url` is not a GitHub release download.

    On 2026-11-08 `github.com/<o>/<r>/releases/download/...` answered 504 in 0.15s, again and again,
    while `api.github.com/repos/<o>/<r>/releases/assets/<id>` served the same 9,773,190 bytes: the
    two paths fail apart. AUTHENTICATED when a token is in the env (`TOKEN_ENVS`): unauthenticated
    is 60/h per IP, and a hosted runner shares its IP, so during that same outage the fallback
    answered 403 rate-limited on ci-kit#44's own CI. The token rides an unredirected header (`_get`),
    so it never reaches the blob host. No token = unauthenticated, as before. The pinned sha256
    still judges whatever comes back."""
    m = re.fullmatch(re.escape(GITHUB_WEB) + r"/([^/]+)/([^/]+)/releases/download/([^/]+)/([^/]+)", url)
    if m is None:
        return None
    owner, repo, tag, name = m.groups()
    raw = _get(f"{GITHUB_API}/repos/{owner}/{repo}/releases/tags/{tag}", "application/vnd.github+json", auth=True)
    try:  # an unreadable payload is the network's fault here, not a finding: URLError -> exit 2 path
        assets = json.loads(raw)["assets"]
        asset_url = next((a["url"] for a in assets if a.get("name") == name), None)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise urllib.error.URLError(f"unreadable release metadata for {tag}: {exc!r}") from exc
    if asset_url is None:
        raise urllib.error.URLError(f"release {tag} has no asset {name}")
    return _get(asset_url, "application/octet-stream", auth=True)


def fetch(url: str, *, gate: str) -> bytes:
    """GET `url`, falling back to the REST API path for a GitHub release; a 5xx or network error is
    retried with backoff, then the gate is blind (exit 2)."""
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            return _get(url)
        except _FetchError as exc:
            err: BaseException = exc
        # The PRIMARY error decides whether to go on: an API 4xx (rate limit) after a download 5xx
        # must not stop the download's own retries (review 2026-11-08). Both errors are reported.
        why = f"{err}"
        if _retryable(err):
            try:
                blob = _via_api(url)
                if blob is not None:
                    print(f"{gate}: {url} failed ({err}); fetched the same asset through the API", file=sys.stderr)
                    return blob
            except _FetchError as api_exc:
                why = f"{err}; API path: {api_exc}"
        if attempt == FETCH_ATTEMPTS or not _retryable(err):
            blind(gate, f"could not fetch {url}: {why} (attempt {attempt}/{FETCH_ATTEMPTS})")
        wait = FETCH_BACKOFF_S[min(attempt, len(FETCH_BACKOFF_S)) - 1]
        print(f"{gate}: fetch {url} failed ({why}); retry {attempt + 1}/{FETCH_ATTEMPTS} in {wait:.0f}s",
              file=sys.stderr)
        time.sleep(wait)
    raise AssertionError("unreachable")
