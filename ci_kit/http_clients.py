"""ci-http-clients: a hand-built HTTP client outside the repo's outbound-HTTP seam may not GROW.

MOVED FROM trade-data (v1.50.0, `scripts/lint_http_clients.py`). A seam module builds the one
client whose transport is counted and whose logger redacts query strings; an inventory there
found 49 hand-built httpx constructions in 37 files, `requests` in 9 and `urllib.request` in 2,
each with its own timeout literal and auth f-string, none counted. This stops the next one while
the rest move onto the seam on touch.

WHAT COUNTS (AST, never a regex: a docstring naming `httpx.Client(` is not a client):
  * a CALL of `httpx.Client` / `httpx.AsyncClient` (module attribute, or the bare name from
    `from httpx import Client`, aliases followed);
  * a CALL of an httpx module-level helper (`get post put patch delete head options request
    stream`) -- each opens a one-shot client of its own, uncounted;
  * an IMPORT of `requests`, `urllib.request` or `urllib3` (`import x`, `from x import ...`,
    `from urllib import request`), one per statement: holding the library IS the client.
Not counted: httpx types (`Timeout`, `Response`, `HTTPError`), a client's own `.get`, a local
class named `Client`, `urllib.parse`.

Why not ruff TID251: banned-api is a BAN with per-file ignores, so a grandfathered file could add
a second client unseen; this is a per-file COUNT ratchet. `--seam PATH` files are not scanned
(the client belongs there); `--use TEXT` is what the failure tells the author to call instead.

  ci-http-clients --dir DIR[:FLOOR] [--dir ...] [--seam PATH ...] [--use TEXT]
                  [--baseline ci/http_clients_baseline.txt] [--write-baseline] [--root DIR] [--selftest]

Exit 0 clean, 1 a new or grown file, 2 could not look (a dir under its floor, a file that will not
decode or parse).
"""
from __future__ import annotations

import ast
import sys
from collections.abc import Sequence

from ci_kit import astread, discover, site_lint
from ci_kit.ratchet import RatchetCount, kept_header

GATE = "http-clients"
HEADER = (
    "# http-clients baseline: per-file count of hand-built HTTP clients outside the seam\n"
    "# (httpx.Client/AsyncClient and httpx helper calls; requests/urllib.request/urllib3 imports).\n"
    "# SHRINK-ONLY (`ci-http-clients`): a grown count or an unlisted file fails CI. Re-freeze: --write-baseline.\n"
)
_CLIENT_NAMES = frozenset({"Client", "AsyncClient"})
_HELPER_NAMES = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "request", "stream"})
_HTTPX_CALLABLES = _CLIENT_NAMES | _HELPER_NAMES
_LIBS = ("requests", "urllib.request", "urllib3")


def _is_lib(module: str) -> bool:
    return any(module == lib or module.startswith(lib + ".") for lib in _LIBS)


def client_sites(tree: ast.Module) -> list[int]:
    """Line numbers of every hand-built client: httpx client/helper calls + HTTP-lib imports."""
    httpx_mods: set[str] = set()
    bare: set[str] = set()
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            httpx_mods.update(a.asname or "httpx" for a in node.names if a.name == "httpx")
            if any(_is_lib(a.name) for a in node.names):
                lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            if node.module == "httpx":
                bare.update(a.asname or a.name for a in node.names if a.name in _HTTPX_CALLABLES)
            if _is_lib(node.module) or (node.module == "urllib" and any(a.name == "request" for a in node.names)):
                lines.append(node.lineno)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if (isinstance(f, ast.Attribute) and f.attr in _HTTPX_CALLABLES
                and isinstance(f.value, ast.Name) and f.value.id in httpx_mods) or (
                isinstance(f, ast.Name) and f.id in bare):
            lines.append(node.lineno)
    return sorted(lines)


def findings_in(source: str, origin: str) -> list[int]:
    return client_sites(astread.parse(source, origin, gate=GATE))


_SHAPES = (
    "import httpx\nimport httpx as hx\nimport requests\nimport urllib.request\nimport urllib3\n"
    "from httpx import AsyncClient as AC, Client\nfrom requests.adapters import HTTPAdapter\n"
    "from urllib import request\n"
    "a = httpx.Client(timeout=1)\nb = hx.AsyncClient()\nc = AC()\nd = Client()\n"
    "from httpx import post as hpost\ne = httpx.get('u')\nf = hx.stream('GET', 'u')\ng = hpost('u')\n"
)  # 5 lib imports + 4 client calls + 3 helper calls
_NOT_CLIENTS = (
    '"""httpx.Client( and import requests in prose are not clients."""\n'
    "import httpx\nfrom app.net import seam\n"
    "t = httpx.Timeout(3)\nr = httpx.Response(200)\nc = seam.sync_client(label='x', timeout_s=1)\n"
    "j = c.get('u')\nm = {}.get('k')\n"
    "e = httpx.HTTPError\nfrom urllib.parse import urlencode\nimport urllib.parse\n"
    "class Client: ...\nk = Client()\n"
)


def selftest() -> list[str]:
    fails: list[str] = []
    if (n := len(findings_in(_SHAPES, "<shapes>"))) != 12:
        fails.append(f"the client shapes counted {n}, not 12 (5 HTTP-lib imports + 7 httpx calls)")
    if got := findings_in(_NOT_CLIENTS, "<notclients>"):
        fails.append(f"a seam call, an httpx type, a client .get, urllib.parse, prose or a local Client counted: {got}")
    return fails


def main(argv: Sequence[str] | None = None) -> int:
    ap = site_lint.gate_parser("ci-http-clients", __doc__, baseline="ci/http_clients_baseline.txt")
    ap.add_argument("--seam", action="append", default=[], metavar="PATH")
    ap.add_argument("--use", default="the repo's HTTP seam", metavar="TEXT")
    a, root, floors = site_lint.parse_gate(ap, argv, gate=GATE, selftest=selftest)
    baseline = site_lint.under(root, a.baseline)
    scanned = [0]

    def scan() -> dict[str, int]:
        seams = [site_lint.norm_dir(s) for s in a.seam]
        discover.tracked_floored(floors, gate=GATE, root=root, keep=lambda rel: rel not in seams)  # floors only
        counts, scanned[0] = site_lint.count_sites(root, list(floors), seams, findings_in, gate=GATE)
        return counts

    def describe(rel: str, now: int, base: int) -> str:
        if base == 0:
            return f"NEW: {rel} builds {now} HTTP client(s) by hand and is not in the baseline. Use {a.use} instead."
        return f"GREW: {rel} now {now} hand-built HTTP client(s), baseline {base}. It may not gain any."

    return site_lint.run(
        ["--write"] if a.write else [], gate=GATE, scan=scan, scanned=lambda: scanned[0],
        scan_dirs=list(floors), ratchet=RatchetCount(baseline, kept_header(baseline, HEADER)),
        selftest=selftest, floor=1, gained="hand-built HTTP client(s) off the seam", describe=describe,
        advice=(f"The seam: {a.use}.",))


if __name__ == "__main__":
    sys.exit(main())
