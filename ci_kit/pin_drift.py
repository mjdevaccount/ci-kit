"""ci-pin-drift: does this repo pin the NEWEST release of a kit? Exit 1 when it lags.

WHY. Once a kit lives in one place, a fix lands there and reaches a consumer only when that
consumer bumps its pin. Nothing fails when it does not: the consumer keeps running the old copy,
green. That is the drift the per-repo copies had (two copies byte-identical only by luck, a third
already diverged), moved from the code into the pin. So a consumer runs this on a schedule, and a
red scheduled run is what a schedule audit already watches.

WHAT IT READS. Every `--file` given is scanned for exact-version pins of the kit named by
`--remote` (its last path component, `.git` stripped; override with `--name`):

    <name>@v1.1.0                                pyproject / requirements / a workflow `uses:`
    <name>?tag=v1.1.0  <name>.git?tag=v1.1.0     uv.lock's git source
    <name>/releases/download/v1.1.0/<asset>      a release-asset wheel URL (pyproject / uv.lock)

Moving refs (`@v1`) and SHA pins are deliberately NOT pins here: `v1` cannot lag by construction,
and a SHA says nothing comparable. The newest release is the highest `vX.Y.Z` tag on `--remote`
(`git ls-remote --tags`), so a runner reads it with the same credential that installs the package.

EXIT. 0 every pin is the newest tag - 1 a pin lags, or names no release tag (each printed with file:line) -
2 could not look: a file missing, NO pin found in any file (a check that found nothing to compare
has verified nothing), the remote unreachable, or no semver tag on it.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

TAG = re.compile(r"refs/tags/(v\d+\.\d+\.\d+)$")


def name_of(remote: str) -> str:
    """The kit's repo name as it appears in a pin: the remote's last path component, no `.git`."""
    tail = remote.replace(chr(92), "/").rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def pin_re(name: str) -> re.Pattern[str]:
    """`name` as a whole word (not `my-name`), optionally `.git`, then EITHER a GitHub release
    asset (`name/releases/download/v1.1.0/<wheel>`: a kit no index publishes) OR an optional path
    inside the repo (a reusable workflow: `name/.github/workflows/x.yml@v1.1.0`) and `@` or
    `?tag=` -- then an EXACT vX.Y.Z, not a pre-release (`-rc1`), not a longer dotted string."""
    tag = r"(v\d+\.\d+\.\d+)"
    return re.compile(r"(?<![\w.-])" + re.escape(name) + r"(?:\.git)?(?:"
                      + r"/releases/download/" + tag + r"/"
                      + r"|(?:/[^@\s?#\"']*)?(?:@|\?tag=)" + tag + r"(?![\w.-]))")


def _ver(tag: str) -> tuple[int, ...]:
    return tuple(int(x) for x in tag[1:].split("."))


def pins_in(text: str, name: str) -> list[tuple[int, str]]:
    """(line number, pinned tag) for every exact-version pin of `name` in `text`."""
    rx = pin_re(name)
    return [(i, m.group(1) or m.group(2))
            for i, ln in enumerate(text.splitlines(), 1) for m in rx.finditer(ln)]


def release_tags(remote: str) -> list[str]:
    """Every vX.Y.Z tag on `remote`, or exit 2."""
    try:
        r = subprocess.run(["git", "ls-remote", "--tags", "--refs", remote],
                           capture_output=True, check=False, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"ci-pin-drift: could not run git ls-remote on {remote}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if r.returncode != 0:
        detail = r.stderr.decode("utf-8", errors="replace").strip().splitlines()
        print(f"ci-pin-drift: git ls-remote {remote} failed "
              f"({detail[-1] if detail else 'no output'}) -- exit 2, could not look", file=sys.stderr)
        raise SystemExit(2)
    tags = [m.group(1) for ln in r.stdout.decode("utf-8", errors="replace").splitlines()
            if (m := TAG.search(ln.strip()))]
    if not tags:
        print(f"ci-pin-drift: no vX.Y.Z tag on {remote} -- exit 2, nothing to compare against",
              file=sys.stderr)
        raise SystemExit(2)
    return tags


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ci-pin-drift", description=__doc__.splitlines()[0])
    ap.add_argument("--file", action="append", required=True,
                    help="file to scan for <name>@vX.Y.Z pins (repeatable)")
    ap.add_argument("--remote", required=True, help="the kit's git remote (its tags are the releases)")
    ap.add_argument("--name", default="", help="the kit's name in a pin (default: from --remote)")
    a = ap.parse_args(argv)
    name = a.name or name_of(a.remote)
    if not name:
        print(f"ci-pin-drift: cannot derive a kit name from {a.remote!r}; pass --name", file=sys.stderr)
        return 2

    found: list[tuple[str, int, str]] = []
    for f in a.file:
        p = pathlib.Path(f)
        if not p.is_file():
            print(f"ci-pin-drift: {f} does not exist -- exit 2, could not look", file=sys.stderr)
            return 2
        try:
            text = p.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"ci-pin-drift: could not read {f}: {exc} -- exit 2, could not look", file=sys.stderr)
            return 2
        found += [(f, ln, tag) for ln, tag in pins_in(text, name)]
    if not found:
        print(f"ci-pin-drift: no {name}@vX.Y.Z pin in {a.file} -- exit 2: a drift check that "
              f"found no pin has verified nothing (did the pin move to a file not listed?)", file=sys.stderr)
        return 2

    tags = release_tags(a.remote)
    newest = max(tags, key=_ver)
    # A pin that names no release (a typo, a bump past the newest tag, a deleted tag) is a
    # finding too: nothing installs from a workflow comment or a doc, so nothing else would fail.
    phantom = [(f, ln, tag) for f, ln, tag in found if tag not in tags]
    lag = [(f, ln, tag) for f, ln, tag in found if tag in tags and _ver(tag) < _ver(newest)]
    if phantom:
        print(f"ci-pin-drift: {len(phantom)} pin(s) name no {name} release tag (newest is {newest}):")
        for f, ln, tag in phantom:
            print(f"  {f}:{ln}: {tag}")
    if lag:
        print(f"ci-pin-drift: {len(lag)} pin(s) behind {name} {newest}:")
        for f, ln, tag in lag:
            print(f"  {f}:{ln}: {tag}")
        print("  bump the pin (and the lockfile) -- a kit fix that never reaches this repo is not a fix here")
    if phantom or lag:
        return 1
    print(f"ci-pin-drift: OK - {len(found)} pin(s), all at the newest {name} tag {newest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
