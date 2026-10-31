#!/usr/bin/env python3
"""dup_code -- the same code written twice fails the PR, mechanically (jscpd behind a shrink-only baseline).

WHY (2026-10-31). A rule that says "integrate before you write" is read by a session with ~67%
adherence (45% while editing; McMillan 2026, 1,650 sessions), so the same helper kept landing a
third time beside its two siblings: `_log()` in three hooks, `_dsn()` in two lab scripts, one
`TickerMatcher` nearly written per news source. Prose cannot hold that line. A check that reads
the code can: every PR runs a clone detector, today's clones are frozen, a NEW clone fails.

THE DETECTOR IS jscpd (kucherenko/jscpd, v5 is a Rust binary released with checksums), not pylint
`duplicate-code`. pylint R0801 reads Python only (this estate is Python, bash, PowerShell, C#,
TypeScript, SQL, YAML), reports a clone as a message with no stable identity to baseline, and
compares only the modules of one invocation. jscpd tokenizes 150+ formats, writes a JSON report
with `lines`/`tokens`/both positions per clone, and keeps a fingerprint baseline of its own
(`--baseline`, `--update-baseline`): the fingerprint is the clone's TOKENS, so a line shift or a
file rename changes nothing, while an edit inside a duplicated block splits it into new clones --
which is the moment to consolidate, not to re-freeze. Measured 2026-10-31 (fingerprint probes on a
fixture; `jscpd --help` 5.4.2).

WHAT THIS MODULE ADDS, AND ONLY THIS. (1) The binary: fetched once per version from the GitHub
release, verified against the sha256 pinned below, cached -- no node, no npm, no network on a
warm cache. (2) The verdict from the JSON report, never from jscpd's exit code, which is 0 on a
missing path, an empty scan and a missing baseline (probed 2026-10-31): no report, or no file
analyzed, is exit 2 (DID NOT FINISH LOOKING), never a clean 0. (3) The tracked filter: a clone is
a finding only when BOTH files are in `git ls-files`, so a stray untracked file cannot fail a PR
and CI (which has only tracked files) and a laptop agree. (4) The shrink-only rule the estate's
other ratchets have: `--write-baseline` REFUSES a baseline whose clone count would grow; a
re-freeze that adds clones is a deliberate act -- delete the file first, in the PR, where review
sees it. (5) The report format every ci-kit gate shares (`site_lint.check`).

THRESHOLDS, MEASURED 2026-10-31 on three live repos at jscpd's floor (30 tokens / 3 lines), then
re-cut offline: trade-data 6,520 clones, trade-infra 1,339, mcp 291. By FORMAT the noise was json
(grafana dashboards, golden fixtures, openapi) and markdown; by SIZE, 5-line test-setup repeats
(a monkeypatch prologue, a `_Client` fake) were ~25% of python hits at 50/5 and gone at 70/7,
where what remains is lab-script twins, copied `_dsn()`/`_log()` helpers and shared test fakes --
the complaint itself. So the defaults are 70 tokens / 7 lines over CODE formats
(python, bash, powershell, csharp, javascript, typescript, sql, yaml, ...), and lockfiles,
migrations, fixtures/golden/snapshots are ignored as a kit default. A repo adds `--ignore GLOB`
or `--format LIST`; the baseline absorbs what exists.

  ci-dup-code [--root DIR] [--baseline FILE] [--min-tokens N] [--min-lines N]
              [--format a,b,c] [--ignore GLOB ...] [--floor N] [--write-baseline | --selftest]

Exit 0 clean / 1 new clone(s) (or a refused re-freeze) / 2 could not look (binary unavailable,
jscpd wrote no report, nothing analyzed).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from collections.abc import Sequence
from typing import Any

from ci_kit import discover, site_lint

GATE = "dup-code"
JSCPD_VERSION = "5.4.2"
RELEASE = "https://github.com/kucherenko/jscpd/releases/download/v{v}/jscpd-{plat}.tar.gz"
#: sha256 of each release tarball, from the release's signed checksums.txt (2026-10-20). A bump
#: moves the version AND every hash in one edit; a tarball that does not match is exit 2.
SHA256 = {
    "darwin-arm64": "b51d22d7c7610d27d8fd022577021ff3266a10f13295a2e545a8b8e126133a88",
    "darwin-x64": "ae8d24aba2719df7682b1bb2c5021ab096116b8240c45fdae88654fc1ff36b17",
    "linux-arm64-gnu": "f796d57334c34e9361575588ee130f8b0a00388e7e42f3b27f7d50d4aecf85ae",
    "linux-x64-gnu": "216bae5de2b30c57f893286585c7a348e8af7cd36810949831f569d29f495a3b",
    "windows-arm64-msvc": "283a4a021570370ee145ed692455290f516d1e528cdcb1747ffe72ff9814d159",
    "windows-x64-msvc": "a7927e3bbdd341a139459c684d3f5e0b6409b7b1660bae275d44523f63128d87",
}
MIN_TOKENS, MIN_LINES = 70, 7
FORMATS = ("python,bash,powershell,csharp,javascript,jsx,typescript,tsx,sql,yaml,hcl,docker,"
           "go,rust,java,kotlin")
IGNORE = ("**/.claude/worktrees/**", "**/node_modules/**", "**/.venv/**", "**/venv/**", "**/__pycache__/**",
          "**/migrations/**", "**/fixtures/**", "**/testdata/**", "**/golden/**", "**/__snapshots__/**",
          "**/*.lock", "**/*.min.*")
DEFAULT_BASELINE = pathlib.Path("ci/jscpd-baseline.json")


def _blind(msg: str) -> None:
    print(f"{GATE}: DID NOT FINISH LOOKING -- {msg}", file=sys.stderr)
    raise SystemExit(2)


def _platform() -> str:
    m = platform.machine().lower()
    arch = "arm64" if m in ("arm64", "aarch64") else "x64"
    if sys.platform.startswith("linux"):
        return f"linux-{arch}-gnu"
    if sys.platform == "darwin":
        return f"darwin-{arch}"
    if sys.platform == "win32":
        return f"windows-{arch}-msvc"
    _blind(f"no jscpd release for {sys.platform}/{m}")
    raise AssertionError  # unreachable


def jscpd_binary(version: str = JSCPD_VERSION) -> pathlib.Path:
    """The pinned jscpd: `$CI_KIT_JSCPD` if set, else the cached release binary, fetched and
    sha256-verified once per version into `$CI_KIT_CACHE_DIR` (default `~/.cache/ci-kit`)."""
    override = os.environ.get("CI_KIT_JSCPD")
    if override:
        return pathlib.Path(override)
    plat = _platform()
    cache = pathlib.Path(os.environ.get("CI_KIT_CACHE_DIR") or pathlib.Path.home() / ".cache" / "ci-kit")
    exe = cache / f"jscpd-{version}-{plat}" / ("jscpd.exe" if sys.platform == "win32" else "jscpd")
    if exe.exists():
        return exe
    url = RELEASE.format(v=version, plat=plat)
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 - pinned https release
            blob = resp.read()
    except OSError as exc:
        _blind(f"could not fetch {url}: {exc}")
    digest = hashlib.sha256(blob).hexdigest()
    want = SHA256.get(plat)
    if digest != want:
        _blind(f"{url}: sha256 {digest} != pinned {want}")
    exe.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        member = next((m for m in tar.getmembers() if m.name.rsplit("/", 1)[-1] == exe.name), None)
        if member is None:
            _blind(f"{url}: no {exe.name} in the tarball")
        fh = tar.extractfile(member)
        assert fh is not None
        exe.write_bytes(fh.read())
    if sys.platform != "win32":
        exe.chmod(0o755)
    return exe


def _rel(name: str) -> str:
    return name.replace("\\", "/").removeprefix("./")


class Scan:
    """One jscpd run over `root`; the verdict is read from the JSON report it writes."""

    def __init__(self, root: pathlib.Path, baseline: pathlib.Path, *, min_tokens: int, min_lines: int,
                 formats: str, ignore: Sequence[str]) -> None:
        self.root, self.baseline = root, baseline
        self.flags = ["--min-tokens", str(min_tokens), "--min-lines", str(min_lines), "--format", formats,
                      "--ignore", ",".join([*IGNORE, *ignore]), "--reporters", "json,silent", "--no-tips"]
        self.sources = 0

    def run(self, extra: Sequence[str]) -> dict[str, Any]:
        exe = jscpd_binary()
        with tempfile.TemporaryDirectory() as out:
            cmd = [str(exe), *self.flags, "--output", out, *extra, "."]
            try:
                r = subprocess.run(cmd, cwd=self.root, capture_output=True, text=True, check=False,
                                   encoding="utf-8", errors="replace", timeout=1800)
            except (OSError, subprocess.TimeoutExpired) as exc:
                _blind(f"could not run jscpd: {exc}")
            report = pathlib.Path(out) / "jscpd-report.json"
            if not report.exists():
                tail = (r.stderr.strip() or r.stdout.strip()).splitlines()[-1:] or ["no output"]
                _blind(f"jscpd wrote no report (rc={r.returncode}): {tail[0]}")
            data = json.loads(report.read_text(encoding="utf-8"))
        self.sources = int(data.get("statistics", {}).get("total", {}).get("sources", 0))
        return data

    def findings(self) -> list[dict[str, Any]]:
        """New clones between TRACKED files: every clone when no baseline exists yet."""
        has_base = self.baseline.exists()
        data = self.run(["--baseline", str(self.baseline)] if has_base else [])
        tracked = set(discover.tracked(["."], suffix="", root=self.root))
        out = []
        for c in data.get("duplicates", []):
            a, b = _rel(c["firstFile"]["name"]), _rel(c["secondFile"]["name"])
            if a not in tracked or b not in tracked:
                continue
            if has_base and not c.get("isNew"):
                continue
            out.append(c)
        self.grandfathered = sum(_load(self.baseline).values()) if has_base else 0
        self.clones = len(data.get("duplicates", []))
        return out

    def write_baseline(self) -> int:
        """Re-freeze through jscpd's own --update-baseline, REFUSING a count that grew."""
        old = _load(self.baseline) if self.baseline.exists() else None
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = pathlib.Path(tmpd) / "baseline.json"
            if old is not None:
                shutil.copyfile(self.baseline, tmp)
            self.run(["--baseline", str(tmp), "--update-baseline"])
            discover.require_scanned(self.sources, gate=GATE, dirs=[str(self.root)], floor=1)
            new = _load(tmp)
        before, after = sum(old.values()) if old else 0, sum(new.values())
        if old is not None and after > before:
            print(f"{GATE}: REFUSING to grow the baseline {before} -> {after} clone(s). The gate exists to make "
                  f"a new clone a PR failure, not a baseline entry: consolidate it. If the growth is "
                  f"deliberate, delete {self.baseline.name} first (in the PR, where review sees it).")
            return 1
        self.baseline.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"version": 1, "fingerprints": dict(sorted(new.items()))}, indent=2) + "\n"
        self.baseline.write_text(body, encoding="utf-8", newline="\n")  # LF on every platform (ratchet.py)
        print(f"{GATE}: baseline written -> {self.baseline.name}: {after} clone(s) in {len(new)} fingerprint(s) "
              f"over {self.sources} file(s)" + (f" (was {before})" if old is not None else ""))
        return 0


def _load(path: pathlib.Path) -> dict[str, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: int(v) for k, v in data.get("fingerprints", {}).items()}


def describe(c: dict[str, Any]) -> str:
    a, b = c["firstFile"], c["secondFile"]
    return (f"{c['format']} {c['lines']}L/{c['tokens']}T  {_rel(a['name'])}:{a['start']}-{a['end']}  <->  "
            f"{_rel(b['name'])}:{b['start']}-{b['end']}")


ADVICE = [
    "the same code now exists twice. Move the shared body behind ONE function or module and import it",
    "from both sites (or delete the copy). Edited inside a baselined clone? That split it: consolidate.",
    "a deliberate re-freeze is `ci-dup-code --write-baseline` -- it refuses to grow the clone count.",
]

# ── selftest: both directions on a planted repo (a clone jscpd must find, and a clean pair) ──
_BODY = "\n".join(f"    v{i} = compute({i}, {i + 1}, {i + 2}) + other(v{i - 1 if i else 0}, {i})" for i in range(14))
PLANTED = {"a/one.py": "def f():\n" + _BODY + "\n    return v0\n",
           "a/two.py": "import os\n\n\ndef g():\n" + _BODY + "\n    return v1\n"}
# the clean pair is longer than MIN_LINES: jscpd does not count a shorter file as a source, and a
# pass over zero sources would be the blind pass this gate exists to refuse
CLEAN = {"a/one.py": "def f(x):\n" + "\n".join(f"    x = step{i}(x, {i})" for i in range(10)) + "\n    return x\n",
         "a/two.py": "def g(y):\n" + "\n".join(f"    y = walk{i}(y) - {i * 3}" for i in range(10)) + "\n    return y\n"}


def selftest() -> list[str]:
    fails: list[str] = []
    for name, files, want in (("planted clone", PLANTED, 1), ("clean pair", CLEAN, 0)):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            for rel, text in files.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text, encoding="utf-8", newline="\n")
            subprocess.run(["git", "-C", d, "init", "-q"], check=False, capture_output=True)
            subprocess.run(["git", "-C", d, "add", "--", *files], check=False, capture_output=True)
            s = Scan(root, root / "ci" / "b.json", min_tokens=MIN_TOKENS, min_lines=MIN_LINES, formats=FORMATS,
                     ignore=())
            got = len(s.findings())
            if got != want:
                fails.append(f"{name}: {got} finding(s), expected {want}")
            if s.sources < 2:
                fails.append(f"{name}: jscpd analyzed {s.sources} file(s), expected both")
            if name == "planted clone" and s.write_baseline() != 0:
                fails.append("planted clone: first --write-baseline refused")
            elif name == "planted clone" and s.findings():
                fails.append("planted clone: still a finding after its baseline was written")
    return fails


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="ci-dup-code", description=__doc__.split("\n", 1)[0])
    ap.add_argument("--root", type=pathlib.Path, default=None)
    ap.add_argument("--baseline", type=pathlib.Path, default=DEFAULT_BASELINE,
                    help=f"relative to --root (default {DEFAULT_BASELINE.as_posix()})")
    ap.add_argument("--min-tokens", type=int, default=MIN_TOKENS)
    ap.add_argument("--min-lines", type=int, default=MIN_LINES)
    ap.add_argument("--format", default=FORMATS, help="jscpd formats, comma-separated")
    ap.add_argument("--ignore", action="append", default=[], metavar="GLOB", help="added to the kit's ignores")
    ap.add_argument("--floor", type=int, default=1, help="fewest files jscpd must have analyzed")
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    env_root = os.environ.get("CI_KIT_ROOT")
    root = (args.root or (pathlib.Path(env_root) if env_root else None) or discover.repo_root()).resolve()
    baseline = args.baseline if args.baseline.is_absolute() else root / args.baseline
    scan = Scan(root, baseline, min_tokens=args.min_tokens, min_lines=args.min_lines, formats=args.format,
                ignore=args.ignore)
    flags = [f for f, on in (("--selftest", args.selftest), ("--write-baseline", args.write_baseline)) if on]

    def ok() -> str:
        gone = scan.grandfathered - scan.clones
        tail = f", {gone} baselined clone(s) gone -- tighten with --write-baseline" if gone > 0 else ""
        return (f"{GATE}: OK -- {scan.sources} file(s) analyzed, {scan.clones} grandfathered clone(s), none new "
                f"(jscpd {JSCPD_VERSION}, >= {args.min_tokens} tokens / {args.min_lines} lines){tail}")

    return site_lint.check(
        flags, gate=GATE, scan=scan.findings, selftest=selftest, modes={"--write-baseline": scan.write_baseline},
        scanned=lambda: scan.sources, scan_dirs=[str(root)], floor=args.floor,
        header=lambda n: f"{GATE}: {n} new clone(s) the baseline does not list "
                         f"(jscpd {JSCPD_VERSION}, >= {args.min_tokens} tokens / {args.min_lines} lines):",
        describe=describe, advice=ADVICE, ok=ok)


if __name__ == "__main__":
    raise SystemExit(main())
