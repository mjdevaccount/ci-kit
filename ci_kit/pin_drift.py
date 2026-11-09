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
    <name>==1.1.0  <name>[extra,...]==1.1.0      a PEP 508 exact pin of a kit on an index (PyPI),
                                                 read as tag v1.1.0; the name PEP 503-normalized

Moving refs (`@v1`) and SHA pins are deliberately NOT pins here: `v1` cannot lag by construction,
and a SHA says nothing comparable. Neither is a range (`>=1.1.0`) or a pre-release (`==1.1.0rc1`).
The newest release is the highest `vX.Y.Z` tag on `--remote` (`git ls-remote --tags`), so a
runner reads it with the same credential that installs the package.

GRACE (`--grace-hours`, default 24; 2026-11-09). A pin is STALE only once the FIRST release past
it is older than the grace; until then it is PENDING: printed, exit 0. Renovate bumps a kit pin
"at any time" (renovate/default.json5 "first-party kits"), but hosted Renovate runs on its own
cadence and the bump PR then waits in the CI queue, so a daily cron read the lag the day after
every kit release (trade-quant pin-drift red 11-08 and 11-09, each a fresh tag Renovate had not
reached yet) -- red that cleared itself is noise that teaches a reader to skip the red. Moving
the cron after Renovate cannot fix that: Renovate's run time is not ours. The age is the
release's (an annotated tag's tagger date, a lightweight tag's commit date), read by a blobless
depth-1 fetch of just those tags into a throwaway repo. A pin is PENDING only if EVERY release
newer than it has a date and every one is younger than the grace -- by DATE, not by version: a
tag made late or re-made (llm-ports v1.19.0 is dated after v1.20.0) must not let a fresh low
version hide a stale pin. A release date it cannot read, or one in the FUTURE (clock skew), gets
NO grace: unknown is not "young". A pin naming no release is never graced. The throwaway repo
holds no credential, so a PRIVATE kit's dates are unreadable and its lag fails closed (strict).
`--grace-hours 0` is the old strict check.

EXIT. 0 every pin is the newest tag, or lags only within the grace - 1 a pin is stale, or names
no release tag (each printed with file:line) - 2 could not look: a file missing, NO pin found in
any file (a check that found nothing to compare has verified nothing), the remote unreachable,
or no semver tag on it.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
import tempfile
import time

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


def index_pin_re(name: str) -> re.Pattern[str]:
    """A PEP 508 exact pin of `name` on a package index: `name==1.1.0` or `name[a,b]==1.1.0`.
    The name is matched PEP 503-normalized (`-`, `_`, `.` runs equal, any case), so
    `llm_ports==1.0.0` pins `llm-ports`. Only `==` with an EXACT X.Y.Z counts: a range, a
    pre-release (`==1.0.0rc1`) or a local/post version is not a release pin."""
    parts = [re.escape(p) for p in re.split(r"[-_.]+", name) if p]
    return re.compile(r"(?<![\w.-])" + r"[-_.]+".join(parts) + r"(?:\s*\[[^\]]*\])?\s*==\s*"
                      + r"(\d+\.\d+\.\d+)(?![\w.+!-])", re.IGNORECASE)


def _ver(tag: str) -> tuple[int, ...]:
    return tuple(int(x) for x in tag[1:].split("."))


def pins_in(text: str, name: str) -> list[tuple[int, str]]:
    """(line number, pinned tag) for every exact-version pin of `name` in `text`, in line order.
    An index pin's `1.1.0` is reported as the tag it was released from, `v1.1.0`."""
    rx, ix = pin_re(name), index_pin_re(name)
    out: list[tuple[int, int, str]] = []
    for i, ln in enumerate(text.splitlines(), 1):
        out += [(i, m.start(), m.group(1) or m.group(2)) for m in rx.finditer(ln)]
        out += [(i, m.start(), "v" + m.group(1)) for m in ix.finditer(ln)]
    return [(i, tag) for i, _, tag in sorted(out)]


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


def tag_dates(remote: str, tags: list[str]) -> dict[str, int]:
    """Unix creation time of each of `tags` on `remote` (annotated: tagger date; lightweight: the
    commit's), from a blobless depth-1 fetch of only those tags into a throwaway repo. A tag it
    cannot read is simply absent: the caller gives it no grace."""
    if not tags:
        return {}
    try:  # the temp dir itself (create AND cleanup) is inside: a crash here is no grace, never exit 1
        with tempfile.TemporaryDirectory(prefix="ci-pin-drift-", ignore_cleanup_errors=True) as d:
            def g(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
                return subprocess.run(["git", "-C", d, *args], capture_output=True, check=False,
                                      timeout=timeout)
            g("init", "-q", "--bare")
            r = g("fetch", "-q", "--no-tags", "--depth=1", "--filter=blob:none", remote,
                  *[f"+refs/tags/{t}:refs/tags/{t}" for t in tags], timeout=120)
            if r.returncode != 0:
                detail = r.stderr.decode("utf-8", errors="replace").strip().splitlines()
                print(f"ci-pin-drift: could not fetch release dates from {remote} "
                      f"({detail[-1] if detail else 'no output'}) -- no grace applied", file=sys.stderr)
                return {}
            out = g("for-each-ref", "--format=%(refname:short) %(creatordate:unix)", "refs/tags")
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"ci-pin-drift: could not read release dates: {exc} -- no grace applied", file=sys.stderr)
        return {}
    dates: dict[str, int] = {}
    for ln in out.stdout.decode("utf-8", errors="replace").splitlines():
        tag, _, ts = ln.strip().partition(" ")
        if tag in tags and ts.isdigit():
            dates[tag] = int(ts)
    return dates


def newer_than(pin: str, tags: list[str]) -> list[str]:
    """Every release this pin is missing."""
    return [t for t in tags if _ver(t) > _ver(pin)]


def pending_age_h(pin: str, tags: list[str], dates: dict[str, int], grace_h: float,
                  now: float) -> float | None:
    """Hours since the OLDEST (by date) release `pin` is missing, if EVERY missing release is
    dated and in [0, grace) -- else None: stale, or a date that cannot be trusted (unread, future)."""
    ages = [(now - dates[t]) / 3600 if t in dates else None for t in newer_than(pin, tags)]
    if not ages or any(h is None or not 0 <= h < grace_h for h in ages):
        return None
    return max(h for h in ages if h is not None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ci-pin-drift", description=__doc__.splitlines()[0])
    ap.add_argument("--file", action="append", required=True,
                    help="file to scan for <name>@vX.Y.Z pins (repeatable)")
    ap.add_argument("--remote", required=True, help="the kit's git remote (its tags are the releases)")
    ap.add_argument("--name", default="", help="the kit's name in a pin (default: from --remote)")
    ap.add_argument("--grace-hours", type=float, default=24.0,
                    help="a lag whose every missing release is younger than this is PENDING, "
                         "not a finding (default 24; 0 = strict)")
    a = ap.parse_args(argv)
    if a.grace_hours < 0:
        print("ci-pin-drift: --grace-hours must be >= 0", file=sys.stderr)
        return 2
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
    pending: list[tuple[str, int, str, float]] = []
    if lag and a.grace_hours > 0:
        missing = sorted({t for _, _, tag in lag for t in newer_than(tag, tags)}, key=_ver)
        dates, now = tag_dates(a.remote, missing), time.time()
        ages = {tag: pending_age_h(tag, tags, dates, a.grace_hours, now) for _, _, tag in lag}
        pending = [(f, ln, tag, ages[tag]) for f, ln, tag in lag if ages[tag] is not None]
        lag = [(f, ln, tag) for f, ln, tag in lag if ages[tag] is None]
    if pending:
        print(f"ci-pin-drift: PENDING - {len(pending)} pin(s) behind {name} {newest}, within the "
              f"{a.grace_hours:g}h grace (the bump is Renovate's; red if still behind after it):")
        for f, ln, tag, age in pending:
            print(f"  {f}:{ln}: {tag} (oldest missing release {age:.1f}h ago)")
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
    if not pending:
        print(f"ci-pin-drift: OK - {len(found)} pin(s), all at the newest {name} tag {newest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
