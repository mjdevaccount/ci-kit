"""ci-api-diff -- a committed OpenAPI contract may grow, never break: oasdiff over the PR's diff.

WHY. A repo that commits its OpenAPI document (trade-data `docs/api/openapi.json`) makes every
wire change a reviewable diff -- but a diff is not a verdict. A removed route, a response field
dropped, a request parameter made required all look like any other hunk in a 900 KB JSON file,
and the client generated from it (trade-ui's openapi-typescript types) breaks at runtime, three
components later. This turns the diff into ERR / WARN / INFO and fails the PR on ERR.

THE DIFFER IS oasdiff (github.com/oasdiff/oasdiff), not a hand-rolled walk. It knows OpenAPI 3.0
and 3.1, ~300 classified checks (`api-path-removed-without-deprecation`,
`response-property-removed`, `new-required-request-parameter`, ...), and a native ignore file. It
is a Go binary with no PyPI build, so it is pinned the way jscpd is (ci_kit/release_bin.py:
version + sha256 per platform, cached, `$CI_KIT_OASDIFF` overrides).

THE VERDICT COMES FROM THE DATA, never from oasdiff's exit code: one `oasdiff changelog --format
json` call, parsed. A level-3 entry is ERR -> exit 1. Anything that kept us from reading both
specs or the JSON -- no merge base, a spec that does not parse, a crashed binary -- is exit 2.

WHAT IS COMPARED. Default: the spec at `git merge-base <base-ref> HEAD` (the PR's fork point)
against the working-tree file -- exactly what the PR changes, and zero changes on a push to main.
`--base FILE --revision FILE` compares two files directly (fixtures, other tooling).
A path absent at the merge base is a NEW contract: nothing in it can break, exit 0, said so.

A DELIBERATE BREAK is oasdiff's own ignore file (`--err-ignore FILE`, one line per accepted
change: `GET /old/route api path removed without deprecation`), committed in the PR that breaks,
where review reads it. There is no flag that skips the check.

Usage:
  ci-api-diff [--path docs/api/openapi.json] [--base-ref origin/main] [--err-ignore FILE]
  ci-api-diff --base OLD.json --revision NEW.json [--err-ignore FILE]
  ci-api-diff --selftest

Exit 0 no breaking change / 1 breaking change(s) / 2 could not look.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from typing import Any

from ci_kit import discover, release_bin, site_lint

GATE = "api-diff"
# renovate: datasource=github-releases depName=oasdiff/oasdiff
OASDIFF_VERSION = "1.37.0"
#: sha256 of each release tarball, from the release's checksums.txt (v1.35.0, 2026-11-03). A bump
#: moves the version AND every hash in one edit; a tarball that does not match is exit 2.
OASDIFF = release_bin.Pin(
    name="oasdiff", version=OASDIFF_VERSION, env="CI_KIT_OASDIFF",
    url="https://github.com/oasdiff/oasdiff/releases/download/v{v}/oasdiff_{v}_{asset}.tar.gz",
    assets={"darwin-arm64": "darwin_all", "darwin-x64": "darwin_all", "linux-arm64": "linux_arm64",
            "linux-x64": "linux_amd64", "windows-arm64": "windows_arm64", "windows-x64": "windows_amd64"},
    sha256={
        "darwin-arm64": "964ad2c0331768c4515fc49779827094bc89a65b1d309dac28886d1c3faf305c",
        "darwin-x64": "964ad2c0331768c4515fc49779827094bc89a65b1d309dac28886d1c3faf305c",
        "linux-arm64": "1f8b5aa033cd0240ae6c53460268bcd6e578526666a4487a781ece8cef39fbdd",
        "linux-x64": "a8023002fcb2eaa7f5758148a9303cb63d33b939886df312268bc0fc7b939140",
        "windows-arm64": "da981a1d29092d43e692e65f90c5ce90232849e2aa9a7c1e417bd85108e58d98",
        "windows-x64": "5cdea2b64bd083d16dff19dba3aae011a0b7388ab33df81051cfc76800bf73e0",
    },
)
LEVEL = {3: "ERR", 2: "WARN", 1: "INFO"}


def _blind(msg: str) -> None:
    release_bin.blind(GATE, msg)


def _oasdiff_json(*args: str) -> Any:
    try:
        r = subprocess.run([str(release_bin.binary(OASDIFF, gate=GATE)), *args, "--format", "json"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
                           timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _blind(f"could not run oasdiff: {exc}")
    if r.returncode != 0:
        tail = (r.stderr.strip() or r.stdout.strip()).splitlines()[-1:] or ["no output"]
        _blind(f"oasdiff {args[0]} exited {r.returncode}: {tail[0]}")
    out = r.stdout.strip()
    if not out or out == "null":
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        _blind(f"oasdiff {args[0]} printed no JSON: {exc}: {out[:200]}")


def changes(base: pathlib.Path, revision: pathlib.Path, *, err_ignore: pathlib.Path | None = None) -> list[dict[str, Any]]:
    """Every classified change base -> revision (oasdiff changelog, all levels), or exit 2."""
    extra: list[str] = []
    if err_ignore is not None:
        if not err_ignore.exists():
            _blind(f"--err-ignore {err_ignore} does not exist")
        extra = ["--err-ignore", str(err_ignore)]
    data = _oasdiff_json("changelog", str(base), str(revision), *extra)
    if data is None:  # identical specs print an empty list or nothing
        return []
    if not isinstance(data, list):
        _blind(f"oasdiff changelog JSON is a {type(data).__name__}, not a list of changes")
    return data


def structural(base: pathlib.Path, revision: pathlib.Path) -> list[str]:
    """WHAT MOVED, by operation and component, including what `changes` does not classify
    (a summary or description edit is a change to the committed contract, not a break). Empty =
    the two documents are the same API. Lines: `operation added|removed|changed: GET /x`,
    `path added|removed: /x`, `component added|removed|changed: Name`, `document changed: info`."""
    d = _oasdiff_json("diff", str(base), str(revision)) or {}
    out: list[str] = []
    paths = d.get("paths") or {}
    out += [f"path added: {p}" for p in paths.get("added") or []]
    out += [f"path removed: {p}" for p in paths.get("deleted") or []]
    for p, body in (paths.get("modified") or {}).items():
        ops = (body or {}).get("operations") or {}
        out += [f"operation added: {m.upper()} {p}" for m in ops.get("added") or []]
        out += [f"operation removed: {m.upper()} {p}" for m in ops.get("deleted") or []]
        out += [f"operation changed: {m.upper()} {p}" for m in (ops.get("modified") or {})]
        if set(body or {}) - {"operations"}:
            out.append(f"path changed: {p} ({', '.join(sorted(set(body) - {'operations'}))})")
    schemas = (d.get("components") or {}).get("schemas") or {}
    out += [f"component added: {c}" for c in schemas.get("added") or []]
    out += [f"component removed: {c}" for c in schemas.get("deleted") or []]
    out += [f"component changed: {c}" for c in schemas.get("modified") or {}]
    other = sorted(set(d) - {"paths", "components", "endpoints"})
    other += [f"components.{k}" for k in sorted(set(d.get("components") or {}) - {"schemas"})]
    out += [f"document changed: {k}" for k in other]
    return out


def breaking(found: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in found if int(c.get("level", 0)) >= 3]


def describe(c: dict[str, Any]) -> str:
    where = f"{c.get('operation', '')} {c.get('path', '')}".strip() or c.get("section", "?")
    return f"{LEVEL.get(int(c.get('level', 0)), '?')} {where}: {c.get('text', '').strip()} [{c.get('id', '?')}]"


def _git(root: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False, timeout=120)


def base_spec(root: pathlib.Path, path: str, base_ref: str, dest: pathlib.Path) -> bool:
    """Write the spec at merge-base(base_ref, HEAD) to `dest`; False = the path is new there."""
    mb = _git(root, "merge-base", base_ref, "HEAD")
    sha = mb.stdout.decode().strip()
    if mb.returncode != 0 or not sha:
        _blind(f"no merge base between {base_ref} and HEAD ({mb.stderr.decode().strip() or 'empty'}) -- a "
               f"shallow clone or a missing ref; CI needs fetch-depth: 0")
    shown = _git(root, "show", f"{sha}:{path}")
    if shown.returncode != 0:
        exists = _git(root, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0
        if exists and _git(root, "ls-tree", "--name-only", sha, "--", path).stdout.strip() == b"":
            return False
        _blind(f"could not read {path} at {sha[:12]}: {shown.stderr.decode().strip()}")
    dest.write_bytes(shown.stdout)
    return True


# ── selftest: both directions, on specs small enough to read ───────────────────────────────
def _spec(paths: dict[str, Any]) -> dict[str, Any]:
    return {"openapi": "3.1.0", "info": {"title": "selftest", "version": "1"}, "paths": paths}


_ITEM = {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}
_GET = {"get": {"responses": {"200": {"description": "ok", "content": {"application/json": {"schema": _ITEM}}}}}}
_WIDER = {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}, "note": {"type": "string"}}}
FIXTURES = {
    "base": _spec({"/items": _GET, "/other": _GET}),
    # a route gone and a response field gone: the two breaks a generated client dies on
    "breaking": _spec({"/items": {"get": {"responses": {"200": {"description": "ok", "content": {
        "application/json": {"schema": {"type": "object", "properties": {}}}}}}}}}),
    # a new route and a new optional response field: a client keeps working
    "additive": _spec({"/items": {"get": {"responses": {"200": {"description": "ok", "content": {
        "application/json": {"schema": _WIDER}}}}}}, "/other": _GET, "/new": _GET}),
}


def selftest() -> list[str]:
    fails: list[str] = []
    with tempfile.TemporaryDirectory() as d:
        files = {k: pathlib.Path(d) / f"{k}.json" for k in FIXTURES}
        for k, f in files.items():
            f.write_text(json.dumps(FIXTURES[k]), encoding="utf-8")
        brk = changes(files["base"], files["breaking"])
        ids = {c.get("id") for c in breaking(brk)}
        if not {"api-path-removed-without-deprecation", "response-required-property-removed"} <= ids:
            fails.append(f"breaking fixture: ERR ids {sorted(ids)} miss the removed route / removed field")
        add = changes(files["base"], files["additive"])
        if breaking(add):
            fails.append(f"additive fixture reported ERR: {[describe(c) for c in breaking(add)]}")
        if not add:
            fails.append("additive fixture: zero changes -- oasdiff did not see the new route (blind)")
        if changes(files["base"], files["base"]) or structural(files["base"], files["base"]):
            fails.append("identical specs reported changes")
        moved = structural(files["base"], files["additive"])
        for want in ("path added: /new", "operation changed: GET /items"):
            if want not in moved:
                fails.append(f"structural diff did not name `{want}`: {moved}")
        ignore = pathlib.Path(d) / "ignore.txt"
        ignore.write_text("GET /other api path removed without deprecation\n", encoding="utf-8")
        left = {c.get("id") for c in breaking(changes(files["base"], files["breaking"], err_ignore=ignore))}
        if "api-path-removed-without-deprecation" in left:
            fails.append("--err-ignore did not accept the listed break")
    return fails


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="ci-api-diff", description=__doc__.split("\n", 1)[0])
    ap.add_argument("--path", default="docs/api/openapi.json", help="the committed spec, repo-relative")
    ap.add_argument("--base-ref", default=os.environ.get("CI_KIT_API_BASE_REF") or "origin/main")
    ap.add_argument("--base", type=pathlib.Path, help="compare this file ...")
    ap.add_argument("--revision", type=pathlib.Path, help="... to this one (skips git)")
    ap.add_argument("--err-ignore", type=pathlib.Path, help="oasdiff ignore file: accepted breaks, one per line")
    ap.add_argument("--root", type=pathlib.Path, default=None)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    state: dict[str, Any] = {"all": [], "note": ""}

    def scan() -> list[dict[str, Any]]:
        files = args.base or args.revision
        if files and not (args.base and args.revision):
            _blind("--base and --revision go together")
        root = None if files else discover.repo_root((args.root or pathlib.Path.cwd()).resolve())
        err_ignore = args.err_ignore
        if err_ignore is not None and root is not None and not err_ignore.is_absolute():
            err_ignore = root / err_ignore
        if files:
            base, rev = args.base, args.revision
            state["what"] = f"{base} -> {rev}"
        else:
            rev = root / args.path
            if not rev.exists():
                _blind(f"{args.path} does not exist in the working tree")
            base = pathlib.Path(tempfile.mkdtemp()) / "base.json"
            state["what"] = f"{args.path} vs merge-base({args.base_ref}, HEAD)"
            if not base_spec(root, args.path, args.base_ref, base):
                state["note"] = " -- NEW at the merge base, nothing in it can break"
                return []
        state["all"] = changes(base, rev, err_ignore=err_ignore)
        return breaking(state["all"])

    def ok() -> str:
        found = state["all"]
        lines = [f"{GATE}: OK -- {state.get('what', '')}: {len(found)} change(s), none breaking "
                 f"(oasdiff {OASDIFF_VERSION}){state['note']}"]
        lines += [f"  {describe(c)}" for c in found[:40]]
        return "\n".join(lines)

    return site_lint.check(
        ["--selftest"] if args.selftest else [], gate=GATE, scan=scan, selftest=selftest,
        header=lambda n: f"{GATE}: {n} BREAKING change(s) in {state.get('what', '')} (oasdiff {OASDIFF_VERSION}):",
        describe=describe,
        advice=["a client generated from the committed spec breaks on these. Make the change additive (keep the",
                "old route/field, deprecate it, remove it in a later PR), or accept it deliberately: add the line",
                "to the --err-ignore file in THIS PR (`<METHOD> <path> <text>`), where review reads it."],
        ok=ok)


if __name__ == "__main__":
    raise SystemExit(main())
