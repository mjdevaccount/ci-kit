"""ci-gitleaks -- the secret scan from a pinned gitleaks release, no docker, no third-party action.

WHY (2026-11-08). gitleaks/gitleaks-action@v3 downloads its own gitleaks tarball from
`github.com/<o>/<r>/releases/download/...` on every run, with no retry and no other path. That
endpoint answered 504 for every repo for over an hour while the REST asset API served the same
bytes, and every PR whose required check ran the action went red without scanning (trade-data
#2880-2884, run 42464597214). The binary now comes through `release_bin`: pinned by version AND
sha256, cached (`$CI_KIT_CACHE_DIR`, shared on the home runners), retried, with the REST fallback.
No docker either, so it runs on a pool without a docker socket (the secure pool).

MODES
  range    what gitleaks-action scanned: a PR's own commits (base.sha..head.sha), a push's commits
           (before..after; a new branch scans after's ancestry), a merge group's (base..head),
           and every ref (gitleaks' default log) on any other event (dispatch, schedule).
  history  HEAD's ancestry, never `--all` (ci-kit gitleaks.yml, 2026-10-31: an open PR branch's
           fixture failed every other PR in the repo).
  tree     the working tree only (--no-git).
A scan that looked at nothing is exit 2, never a pass. `range` asks git (`rev-list --count` over
the same log opts) whether the rev spec matched any commit: gitleaks' own "N commits scanned"
counts only commits that ADDED text, so a delete/rename/binary-only PR reads 0 there with nothing
to find (review 2026-11-08). `history` keeps gitleaks' count (0, or no such line, is exit 2:
its diff filter makes git's count the wrong question). A range needs the commits in the checkout:
`fetch-depth: 0`. A shallow clone fails gitleaks' git log, which is exit 2, loudly.

A BASELINE ENTRY THAT CAN NEVER MATCH IS exit 2, before any scan (2026-11-09). gitleaks matches a
.gitleaksignore fingerprint by exact string, and a finding's file path is relative to the repo (git
modes) or to --source (tree). The old edge-ci scan ran in docker with the repo mounted at /repo,
so its fingerprints read `/repo/tools/...`; under ci-gitleaks no finding ever carries that path, the
entry silently stopped baselining anything, and TradingEdge.Scraper#156 went red on an item the
file said was handled. An absolute path that the scan cannot produce is refused, by name, and so is
an --ignore-path that does not exist: a baseline nobody reads is the same dead guard.

EXIT  0 clean · 1 a leak (rotate it; never baseline it away) · 2 did not finish looking.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import subprocess
import sys

from ci_kit import release_bin

GATE = "ci-gitleaks"
# renovate: datasource=github-releases depName=gitleaks/gitleaks
GITLEAKS_VERSION = "8.30.1"
#: sha256 of each release tarball, from the release's own gitleaks_8.30.1_checksums.txt (read through
#: the REST asset API, 2026-11-08). The version gitleaks.yml's docker image ran before v1.17.0. Windows
#: ships a .zip, which release_bin does not unpack, so it has no row (exit 2 there, by name).
GITLEAKS = release_bin.Pin(
    name="gitleaks", version=GITLEAKS_VERSION, env="CI_KIT_GITLEAKS",
    url="https://github.com/gitleaks/gitleaks/releases/download/v{v}/gitleaks_{v}_{asset}.tar.gz",
    assets={"darwin-arm64": "darwin_arm64", "darwin-x64": "darwin_x64",
            "linux-arm64": "linux_arm64", "linux-x64": "linux_x64"},
    sha256={
        "darwin-arm64": "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5",
        "darwin-x64": "dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709",
        "linux-arm64": "e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080",
        "linux-x64": "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
    },
)
LEAK_RC = 99  # gitleaks' --exit-code on a finding; any OTHER nonzero is gitleaks failing to run
ZERO_SHA = "0" * 40
# HEAD's ancestry, the diff filter restated: gitleaks replaces its default log opts wholesale
HISTORY_OPTS = "--full-history --diff-filter=tuxdb HEAD"
SCANNED = re.compile(r"([0-9]+) commits scanned")


def range_opts(event_name: str, event: dict) -> str | None:
    """gitleaks --log-opts for `range` mode; None = gitleaks' default log (every ref)."""
    def sha(*path: str) -> str:
        node = event
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        return node if isinstance(node, str) and re.fullmatch(r"[0-9a-f]{40}", node) else ""

    if event_name in ("pull_request", "pull_request_target"):
        base, head = sha("pull_request", "base", "sha"), sha("pull_request", "head", "sha")
    elif event_name == "merge_group":
        base, head = sha("merge_group", "base_sha"), sha("merge_group", "head_sha")
    elif event_name == "push":
        base, head = sha("before"), sha("after")
        if head and (not base or base == ZERO_SHA):
            return f"--no-merges --first-parent {head}"  # a new branch: everything it carries
    else:
        return None
    if not (base and head):
        release_bin.blind(GATE, f"{event_name} event carries no base/head sha -- cannot scope a range")
    return f"--no-merges --first-parent {base}..{head}"


def scan_args(mode: str, source: str, config: str, ignore: str, log_opts: str | None) -> list[str]:
    args = ["detect", f"--source={source}", "--no-banner", "--redact", f"--exit-code={LEAK_RC}"]
    if mode == "tree":
        args.append("--no-git")
    if config:
        args += ["--config", config]
    if ignore:
        args += ["--gitleaks-ignore-path", ignore]
    if mode == "history":
        args.append(f"--log-opts={HISTORY_OPTS}")
    elif mode == "range" and log_opts is not None:
        args.append(f"--log-opts={log_opts}")
    return args


def verdict(mode: str, rc: int, output: str, matched: int | None = None) -> tuple[int, str]:
    """(exit code, message) from gitleaks' rc and its combined output.

    `matched` = commits git itself says the range holds (`range` mode). gitleaks counts only a
    commit that ADDED text, so a delete-only, rename-only or binary-only PR reports "0 commits
    scanned" with nothing to find (gitleaks 8.30.1 detect/detect.go): judged on that count it
    went red on legitimate PRs (review 2026-11-08). git's count asks the real question -- did
    the rev spec match anything -- and 0 there is still exit 2."""
    if rc == LEAK_RC:
        return 1, "LEAK FOUND -- rotate the secret; it may not be baselined away"
    if rc != 0:
        return 2, f"gitleaks exited {rc} without a verdict (a bad rev spec, a shallow clone, a bad config?)"
    if matched is not None:
        if matched == 0:
            return 2, "the range holds no commits (git rev-list): NOTHING WAS SCANNED"
        return 0, f"no leaks found ({matched} commit(s) in range)"
    if mode != "tree":
        m = SCANNED.search(output)
        if m is None or int(m.group(1)) == 0:
            return 2, "gitleaks reported no commits scanned: NOTHING WAS SCANNED"
    return 0, "no leaks found"


_SHA = re.compile(r"[0-9a-f]{40}")


def dead_entries(text: str, mode: str, source: str) -> list[str]:
    """The .gitleaksignore entries no finding of this scan can ever match (module doc).

    A fingerprint is `[commit:]file:rule:line`. Its file is absolute only when the scan reports
    absolute paths, which is tree mode over an absolute --source -- and then it must sit under it.
    Git modes report repo-relative paths whatever --source is."""
    out = []
    # gitleaks writes forward-slash paths on every OS, so the root is judged as a POSIX path.
    root = posixpath.normpath(source) if mode == "tree" and source.startswith("/") else None
    for raw in text.splitlines():
        entry = raw.strip()
        if not entry or entry.startswith("#"):
            continue
        parts = entry.split(":")
        if len(parts) < 3:
            out.append(f"{entry!r}: not a fingerprint ([commit:]file:rule:line)")
            continue
        start = 1 if len(parts) >= 4 and _SHA.fullmatch(parts[0]) else 0
        path = ":".join(parts[start:-2])
        absolute = path.startswith("/") or bool(re.match(r"[A-Za-z]:[\\/]", path))
        if absolute and (root is None or not (path == root or path.startswith(root.rstrip("/") + "/"))):
            out.append(f"{entry!r}: file {path!r} is absolute, and this scan reports paths relative to "
                       "the repo -- it can never match (a leading /repo/ is the old docker scan's mount: drop it)")
    return out


def check_ignore(path: str, mode: str, source: str) -> None:
    """Refuse (exit 2) an --ignore-path that is missing, unreadable, or holds a dead entry."""
    if not path:
        return
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        release_bin.blind(GATE, f"--ignore-path {path!r} cannot be read ({exc}): a baseline nobody reads")
    dead = dead_entries(text, mode, source)
    if dead:
        release_bin.blind(GATE, f"{path}: {len(dead)} baseline entr{'y' if len(dead) == 1 else 'ies'} "
                                "can never match -- fix the file:\n  " + "\n  ".join(dead))


def _run(cmd: list[str]) -> tuple[int, str]:
    """Run gitleaks, echoing its output live and returning it for the verdict."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert proc.stdout is not None
    lines = []
    for line in proc.stdout:
        sys.stdout.write(line)
        lines.append(line)
    return proc.wait(), "".join(lines)


def commits_in(source: str, log_opts: str | None) -> int:
    """How many commits git says the range holds; `None` opts = every ref (gitleaks' default)."""
    spec = log_opts.split() if log_opts else ["--all"]
    try:
        out = subprocess.run(["git", "-C", source, "rev-list", "--count", *spec],
                             capture_output=True, text=True, check=True).stdout
        return int(out.strip())
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        detail = getattr(exc, "stderr", "") or exc
        release_bin.blind(GATE, f"git rev-list could not count the range ({log_opts}): {detail}")
        raise  # blind() exits 2; never reached


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog=GATE, description=__doc__.split("\n", 1)[0])
    ap.add_argument("--mode", choices=("range", "history", "tree"), required=True)
    ap.add_argument("--source", default=".")
    ap.add_argument("--config", default="", help="gitleaks config path ('' = the built-in ruleset)")
    ap.add_argument("--ignore-path", default="", help=".gitleaksignore path ('' = none)")
    a = ap.parse_args(argv)
    check_ignore(a.ignore_path, a.mode, a.source)
    log_opts = None
    if a.mode == "range":
        name = os.environ.get("GITHUB_EVENT_NAME", "")
        try:
            with open(os.environ.get("GITHUB_EVENT_PATH", ""), encoding="utf-8") as fh:
                event = json.load(fh)
        except (OSError, ValueError) as exc:
            release_bin.blind(GATE, f"range mode needs the Actions event payload: {exc}")
        log_opts = range_opts(name, event if isinstance(event, dict) else {})
        print(f"{GATE}: {name} -> log opts {log_opts or '(gitleaks default: every ref)'}", file=sys.stderr)
    matched = commits_in(a.source, log_opts) if a.mode == "range" else None
    exe = release_bin.binary(GITLEAKS, gate=GATE)
    rc, out = _run([str(exe), *scan_args(a.mode, a.source, a.config, a.ignore_path, log_opts)])
    code, msg = verdict(a.mode, rc, out, matched)
    print(f"{GATE}: {msg}" if code != 2 else f"{GATE}: DID NOT FINISH LOOKING -- {msg}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
