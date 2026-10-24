#!/usr/bin/env python3
"""env_reads -- a shell script reads KEY from a .env file through the repo's reader, not a hand pipeline.

2026-10-24: one estate had ~26 hand copies of `grep '^KEY=' FILE | cut -d= -f2- | tr -d '"'` and
`sed -n 's/^KEY=//p' FILE`, and they disagreed in RESULTS, not style. One took the last duplicate
(`tail -1`), one the first while claiming to "mirror compose"; one kept the quotes; one cut a
value at any `#`; every one returned a `$$` doubled where docker compose -- which reads the same
file for the containers -- yields one `$`. The fix is ONE reader per language with compose's
semantics; this gate keeps a new hand copy from appearing beside it.

A finding is a logical shell line (backslash and trailing-`|` continuations joined) that pulls a
value for an anchored key straight out of a FILE OPERAND:

  sed   `sed -n 's/^KEY=//p' FILE`
  grep  `grep [-flags] '^KEY=' FILE | ... cut -d= ...` (or `| sed 's/...=//'`)
  awk   `awk -F= '/^KEY=/ {...}' FILE`

KEY may be a literal name or a shell expansion (`$1`, `${key}`, `${1:-SVC_ACTIVE}`). Reading from
a PIPE or here-string is quiet on purpose: `docker inspect ... | sed -n 's/^GIT_SHA=//p'` parses a
container's environment, and `printf '%s' "$out" | sed -n 's/^rc=//p'` parses a tool's output --
neither is a dotenv file a compose reader should answer. Comment lines are exempt.

WAIVER, in the line itself: `# env-read-ok: <why>` (the reason is required; a bare marker is not a
waiver). Use it for a KEY=VALUE file that is not a dotenv (`/etc/os-release`) -- never to keep a
.env read off the reader.

KNOWN BLIND (stated, not hidden): `. .env` / `source .env` (bash, not compose, interprets it),
`while IFS== read -r k v; do ...; done < .env`, a presence check (`grep -q '^KEY=' FILE`), a key
read through a variable holding the whole pattern (`grep "$pat" FILE`), Python and PowerShell
parsers, and a `<(...)` / `$(...)` that splits the pipeline across physical lines without a
continuation. Also not matched (none in the estate at 2026-10-24; review round 2): an UNQUOTED
pattern (`grep ^K= .env`), `'^K=.*'`, `'^export K='`, `--regexp=`, sed capture groups
(`s/^K=\\(.*\\)/\\1/p`), address forms (`/^K=/s/^K=//p`), a trailing `;` in the sed script, awk
field tests (`$1=="K"`) or any awk script holding a `"`, and the file on the LEFT (`cat .env |
grep`, `< .env grep`).

A per-file count ratchet: a NEW site, or a file whose count grows, fails; migrating one shrinks it.
Exit 0 clean / 1 findings / 2 could not look (git ls-files failed, a file would not decode, no
shell file under the scan dirs).

  ci-env-reads [--root DIR] [--scan-dir DIR ...] [--baseline FILE] [--exempt PATH ...]
               [--write-baseline | --selftest]
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
from collections.abc import Sequence

from ci_kit import discover, site_lint
from ci_kit.ratchet import RatchetCount

GATE = "env-reads"
SHELL_SUFFIX = (".sh", ".bash")
SHEBANG = re.compile(r"^#!.*\b(?:ba|da|k|z)?sh\b")

# a literal key, or a shell expansion that names it at run time
KEY = r"(?:[A-Za-z_]\w*|\$\{[^}]*\}|\$\w+)"
# A FILE operand: the next word is not a pipe, a redirection, a list operator or the end of the
# command -- or a plain `< FILE` input redirect, which reads the file just the same. `2>/dev/null`
# before the operand is skipped; `<<<` (a here-string) and `<(...)` are not a file.
_REDIRS = r"(?:\s+\d*>{1,2}\s*\S+)*"
_WORD = r"[^\s|;&)<>]"
OPERAND = _REDIRS + r"(?:\s+(?![|;&)<>]|\d*>)" + _WORD + r"|\s*<(?![<(])\s*" + _WORD + r")"
# the program's own flags may sit before its script: `sed -n -E`, `grep -m1 -E`, `grep -m 1`,
# `--max-count=1`, `--`
FLAGS = r"(?:\s+(?:--[\w-]+(?:=\S+)?|--|-[A-Za-z0-9]+(?:\s+\d+)?))*\s+"
_Q = r"(['\"])"

# `s/^K=//p`, with any of the usual delimiters (`s|^K=||p`, `s#^K=##p`)
SED = re.compile(r"\bsed" + FLAGS + _Q + r"s([/|#,:])\^" + KEY + r"=\2\2p\1" + OPERAND)
GREP = re.compile(r"\bgrep" + FLAGS + _Q + r"\^" + KEY + r"=\1" + OPERAND)
# grep -oP that prints only the value: `(?<=^K=).*` and `^K=\K.*`
GREP_P = re.compile(r"\bgrep" + FLAGS + _Q + r"(?:\(\?<=\^" + KEY + r"=\)|\^" + KEY + r"=\\K)[^'\"]*\1" + OPERAND)
_CUT_EQ = r"cut\s+(?:-\S+\s+)*(?:-d\s*['\"]?=|--delimiter[=\s]['\"]?=)"
GREP_SPLITS = re.compile(r"\|\s*(?:" + _CUT_EQ + r"|sed\b[^|]*\bs([/|#,:])[^/|#,:]*=\1\1|awk\s+-F\s*['\"]?=)")
# the pipeline a grep feeds ends at a list operator; a later `; x=$(echo a=b | cut -d= -f2)` is not it
PIPELINE_END = re.compile(r";|&&|\|\||(?<![|&>])&(?![&>])")


def pipeline_end(line: str, start: int) -> int:
    """Index of the first list operator at or after `start` OUTSIDE quotes, else len(line).
    Quote-blind, `sed 's|^K=||'` or `sed 's/;.*//'` further down the pipe ended it early."""
    quote = ""
    i = start
    while i < len(line):
        c = line[i]
        if quote:
            if c == quote:
                quote = ""
            elif c == "\\" and quote == '"':
                i += 1
        elif c in "'\"":
            quote = c
        elif c == "\\":
            i += 1
        else:
            m = PIPELINE_END.match(line, i)
            if m:
                return i
        i += 1
    return len(line)
AWK = re.compile(r"\bawk\s+-F\s*['\"]?=['\"]?\s+(['\"])[^'\"]*/\^" + KEY + r"=/[^'\"]*\1" + OPERAND)

WAIVER = re.compile(r"#\s*env-read-ok:\s*\S")


def is_shell(rel: str, text: str) -> bool:
    if rel.endswith(SHELL_SUFFIX):
        return True
    if "." in rel.rsplit("/", 1)[-1]:
        return False
    return bool(SHEBANG.match(text.split("\n", 1)[0]))


def wants_scan(root: pathlib.Path, rel: str) -> bool:
    """Shell by suffix, or an extensionless file whose first line is a shell shebang -- decided
    from the first bytes only, so a tracked binary (a .png, an age-encrypted .env) is never decoded."""
    if rel.endswith(SHELL_SUFFIX):
        return True
    if "." in rel.rsplit("/", 1)[-1]:
        return False
    if not (root / rel).is_file():
        return False  # a submodule gitlink, a symlink to a directory: not a script
    try:
        with open(root / rel, "rb") as fh:
            head = fh.readline(256)
    except OSError:
        return True  # a regular file we cannot open: let read_source say it could not look
    return bool(SHEBANG.match(head.decode("utf-8", errors="replace")))


def logical_lines(text: str) -> list[tuple[int, str]]:
    """(first physical line number, joined text). A trailing backslash or `|` continues a line."""
    out: list[tuple[int, str]] = []
    start, buf = 0, ""
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\r")
        if not buf:
            start = i
        stripped = line.rstrip()
        if stripped.endswith("\\") and not stripped.endswith("\\\\"):
            buf += stripped[:-1] + " "
            continue
        if stripped.endswith("|") and not stripped.endswith("||") and not stripped.lstrip().startswith("#"):
            buf += stripped + " "
            continue
        out.append((start, buf + line))
        buf = ""
    if buf:
        out.append((start, buf))
    return out


def is_hand_read(line: str) -> bool:
    if line.lstrip().startswith("#") or WAIVER.search(line):
        return False
    if SED.search(line) or AWK.search(line) or GREP_P.search(line):
        return True
    for m in GREP.finditer(line):
        if GREP_SPLITS.search(line[: pipeline_end(line, m.end())], m.end()):
            return True
    return False


def findings_in(text: str, rel: str = "") -> list[int]:
    return [n for n, line in logical_lines(text) if is_hand_read(line)]


# ── selftest: both directions, on inline cases (each BAD must fire, each GOOD must stay quiet) ──
BAD = [
    "slot=$(grep -E '^SVC_ACTIVE=' \"$ENV_FILE\" | tail -1 | cut -d= -f2- | tr -d '\"')",
    "PW=\"$(grep '^APP_RW_PASSWORD=' /home/u/edge/.env | cut -d= -f2-)\"",
    "envget() { grep -m1 \"^$1=\" \"$ENVF\" 2>/dev/null | cut -d= -f2- | tr -d '\\r'; }",
    "_hb(){ sed -n \"s/^$1=//p\" \"${EDGE_ENV:-$HOME/.env}\" 2>/dev/null | sed -n 1p; }",
    "a=$(sed -n \"s/^${1:-SVC_ACTIVE}=//p\" \"${BG_STATE}\" 2>/dev/null | sed -n 1p)",
    "CUR=\"$(sed -n 's/^OPS_CF_AUD=//p' .env | sed -n 1p)\"",
    "tok=$(grep -E \"^$2=\" \"$1\" 2>/dev/null | sed -n 1p | sed 's/^[^=]*=//')",
    "ip=$(awk -F= '/^OBSERVER_LAN_IP=/ {print $2}' \"$ENV\")",
    "ip=$(sed -n 's/^OBSERVER_LAN_IP=//p' \"$F\" 2>/dev/null |\n  sed -n 1p)",
    "x=$(grep '^K=' \\\n  \"$F\" | cut -d= -f2-)",
    "t=$(grep -oP '(?<=^WINDMILL_TOKEN=).*' \"$ENVF\" | tail -1)",
    "t=$(grep -Po '^K=\\K.*' .env)",
    "v=$(grep -m 1 '^K=' .env | cut -d= -f2-)",
    "v=$(grep --max-count=1 -- '^K=' .env | cut -f2- -d=)",
    "v=$(grep '^K=' < .env | cut --delimiter== -f2-)",
    "v=$(sed -n 's|^K=||p' .env)",
    "v=$(sed -n 's/^K=//p' < .env)",
    "v=$(grep '^K=' .env | sed 's|^K=||')",
    "v=$(grep '^K=' .env | sed 's/;.*//' | cut -d= -f2-)",
]
GOOD = [
    "baked=$(docker inspect c --format '{{.Config.Env}}' | sed -n 's/^GIT_SHA=//p' | sed -n 1p)",
    "rc=$(printf '%s\\n' \"$out\" | sed -n 's/^rc=//p' | tail -1)",
    "v=$(sed -n 's/^K=//p' <<<\"$env\")",
    "# slot=$(grep -E '^SVC_ACTIVE=' \"$ENV_FILE\" | cut -d= -f2-)",
    "id=$(sed -n 's/^ID=//p' /etc/os-release)  # env-read-ok: os-release is not a dotenv",
    "grep -q '^K=' \"$F\" && echo present",
    "slot=$(env_get SVC_ACTIVE \"$ENV_FILE\")",
    "n=$(grep -c '^K=' \"$F\")",
    "echo \"$line\" | cut -d= -f2-",
    "n=$(grep -c '^K=' .env); m=$(echo $s | cut -d= -f2)",
    "grep -q '^K=' .env && x=$(echo a=b | cut -d= -f2)",
    "grep -q '^K=' .env 2>&1 && x=$(echo a=b | cut -d= -f2)",
]
WAIVER_NO_REASON = "id=$(sed -n 's/^ID=//p' /etc/os-release)  # env-read-ok:"


def selftest() -> list[str]:
    fails = [f"MISSED: {b!r}" for b in BAD if findings_in(b) != [1]]
    fails += [f"FALSE POSITIVE: {g!r}" for g in GOOD if findings_in(g)]
    if not findings_in(WAIVER_NO_REASON):
        fails.append("a waiver with no reason waived the line")
    multi = "a=1\n" + BAD[0] + "\nb=2\n" + BAD[1] + "\n"
    if findings_in(multi) != [2, 4]:
        fails.append(f"line numbers wrong: {findings_in(multi)} != [2, 4]")
    if not is_shell("bin/tool", "#!/usr/bin/env bash\n") or is_shell("README.md", "#!/bin/sh\n"):
        fails.append("shebang detection wrong")
    return fails


HEADER = (
    "# env-reads baseline: hand .env pipelines per file (`ci-env-reads`). SHRINK-ONLY.\n"
    "# A new site fails CI. Migrate it to the repo's .env reader instead of re-freezing;\n"
    "# re-freeze with --write-baseline only after a migration made a count smaller.\n"
)
ADVICE = [
    "read the key through the repo's .env reader (compose's semantics: last duplicate wins,",
    "quotes, `export`, ` #` comments, `$$`) instead of a grep/sed/cut copy.",
    "not a dotenv at all? waive the line: `# env-read-ok: <why>`.",
]


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="ci-env-reads", description=__doc__.split("\n", 1)[0])
    ap.add_argument("--root", type=pathlib.Path, default=None)
    ap.add_argument("--scan-dir", action="append", default=[], metavar="DIR")
    ap.add_argument("--baseline", type=pathlib.Path, default=pathlib.Path("ci/env_reads_baseline.txt"),
                    help="relative to --root (default ci/env_reads_baseline.txt)")
    ap.add_argument("--exempt", action="append", default=[], metavar="PATH",
                    help="a tracked file never scanned (the reader itself)")
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    env_root = os.environ.get("CI_KIT_ROOT")  # same order as the kit's other gates
    root = (args.root or (pathlib.Path(env_root) if env_root else None) or discover.repo_root()).resolve()
    scan_dirs = args.scan_dir or ["."]
    baseline = args.baseline if args.baseline.is_absolute() else root / args.baseline
    seen = {"n": 0}

    def scan() -> dict[str, int]:
        counts: dict[str, int] = {}
        skip = set(args.exempt)
        for rel in discover.tracked(scan_dirs, suffix="", root=root):
            if rel in skip:
                continue
            if not wants_scan(root, rel):
                continue
            text = site_lint.read_source(root / rel, rel, gate=GATE)
            seen["n"] += 1
            n = len(findings_in(text, rel))
            if n:
                counts[rel] = n
        return counts

    flags = [f for f, on in (("--selftest", args.selftest), ("--write-baseline", args.write_baseline)) if on]
    if args.write_baseline:
        baseline.parent.mkdir(parents=True, exist_ok=True)
    return site_lint.run(
        flags, gate=GATE, scan=scan, scanned=lambda: seen["n"], scan_dirs=scan_dirs,
        ratchet=RatchetCount(baseline, HEADER), selftest=selftest,
        gained="hand .env pipeline site(s)", advice=ADVICE, floor=1,
        write_flags=("--write-baseline",))


if __name__ == "__main__":
    raise SystemExit(main())
