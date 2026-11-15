#!/usr/bin/env python3
"""declared_literal -- a literal that must live in ONE declared place appears nowhere else.

THE CLASS. Some values belong to exactly one file: an address range, a hostname, a port, an
account id. Everything else reads them from that file (an env file, a config template) -- so
moving the value is one edit, not a grep-and-pray sweep where one miss leaves a target pointing at
the old value. This gate holds the count of copies at ZERO: a line matching a rule's `--pattern`
in any tracked file that is not one of that rule's `--declared` files is a finding.

THE POLICY IS THE CALLER'S. The kit ships no pattern, no declared file and no message: with no
rule it is exit 2, never a clean pass over nothing. A rule is opened by `--rule LABEL`, and the
`--pattern` (Python `re`, repeatable, OR'd), `--declared PATH` (repo-relative, repeatable) and
`--message TEXT` flags after it belong to it. Flags before any `--rule` belong to one implicit
rule. `--exempt-suffix .EXT` (repeatable, every rule) exempts a file kind wholesale, e.g. `.md`
when prose may name the value. A regex cannot travel in the action's word-split `args`, so the
policy lives in an ARGS FILE: `ci-declared-literal @ci/literals.args`, one argument per line
(`--pattern=<regex>` keeps a regex with spaces whole); blank lines and lines starting `#` are
skipped, so the file can say why each rule exists.

EXEMPT LINES. A line whose first non-blank characters open a comment (`COMMENT_STARTS`) may
carry the value for history. In PowerShell (`POWERSHELL_SUFFIXES`) a `<# ... #>` block comment is
STRIPPED and what is left is scanned: code before a block, code after one closes on the same line,
and a `<#` inside a quoted string (not a comment) are all still read. Only PowerShell: `<#` means
nothing in other languages, and honouring it there would let one line blind the rest of a file.

INPUT: `git ls-files` (never a filesystem walk), every tracked file, decoded UTF-8 with
replacement so a value inside any file is still seen. Symlinks and gitlinks are skipped.

Exit 0 clean, 1 findings, 2 could not look: no rule, a rule with no pattern, a pattern that does
not compile or matches the empty string (it would match every line), git failing, zero files
read, a tracked path missing on disk. The last line always says how many files were scanned and
which rules were loaded, so two runs (or a repoint) can be compared.

  ci-declared-literal [--root DIR] [--exempt-suffix .EXT ...]
                      [--rule LABEL] --pattern REGEX [--declared PATH ...] [--message TEXT] ...
  ci-declared-literal --selftest
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import os
import pathlib
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence

from ci_kit import discover, site_lint

GATE = "declared-literal"
IMPLICIT = "literal"
COMMENT_STARTS = ("#", "//", "<!--", ";", "*", "--")
POWERSHELL_SUFFIXES = frozenset({".ps1", ".psm1", ".psd1"})
BLOCK_OPEN, BLOCK_CLOSE = "<#", "#>"
DEFAULT_MESSAGE = "Read the value from the declared source instead; a comment line may keep it for history."


class ConfigError(Exception):
    """The policy as given cannot be checked -- exit 2, never a pass."""


@dataclasses.dataclass
class Rule:
    label: str
    patterns: list[str] = dataclasses.field(default_factory=list)
    declared: list[str] = dataclasses.field(default_factory=list)
    message: str = ""
    compiled: list[re.Pattern[str]] = dataclasses.field(default_factory=list)

    def hits(self, code: str) -> bool:
        return any(p.search(code) for p in self.compiled)


def _rules(ns: argparse.Namespace) -> list[Rule]:
    if not hasattr(ns, "rules") or ns.rules is None:
        ns.rules = []
    return ns.rules


class _OpenRule(argparse.Action):
    def __call__(self, parser, ns, value, option_string=None):  # type: ignore[override]
        _rules(ns).append(Rule(label=str(value)))


class _ToRule(argparse.Action):
    """`--pattern` / `--declared` / `--message` attach to the rule opened last (or the implicit one)."""

    def __call__(self, parser, ns, value, option_string=None):  # type: ignore[override]
        rules = _rules(ns)
        if not rules:
            rules.append(Rule(label=IMPLICIT))
        rule = rules[-1]
        if self.dest == "message":
            rule.message = str(value)
        else:
            getattr(rule, self.dest).append(str(value))


class _Parser(argparse.ArgumentParser):
    def convert_arg_line_to_args(self, arg_line: str) -> list[str]:
        """An args file: one argument per line; blank lines and `#` lines are notes, not args."""
        stripped = arg_line.strip()
        return [] if not stripped or stripped.startswith("#") else [arg_line.rstrip("\r")]


def compile_rules(rules: list[Rule]) -> list[Rule]:
    """Validate and compile, or raise ConfigError naming exactly what is wrong."""
    if not rules:
        raise ConfigError("no --pattern given: the kit holds no policy of its own, so with no rule "
                          "there is nothing to check (pass an args file: @ci/literals.args)")
    seen: set[str] = set()
    for rule in rules:
        if rule.label in seen:
            raise ConfigError(f"rule {rule.label!r} is opened twice")
        seen.add(rule.label)
        if not rule.patterns:
            raise ConfigError(f"rule {rule.label!r} has no --pattern")
        for pat in rule.patterns:
            if not pat:
                raise ConfigError(f"rule {rule.label!r}: an empty --pattern matches every line")
            try:
                rx = re.compile(pat)
            except re.error as exc:
                raise ConfigError(f"rule {rule.label!r}: --pattern {pat!r} does not compile: {exc}") from exc
            if rx.search(""):
                raise ConfigError(f"rule {rule.label!r}: --pattern {pat!r} matches the empty string, "
                                  f"so it matches every line")
            rule.compiled.append(rx)
        rule.declared = [d.replace("\\", "/").removeprefix("./") for d in rule.declared]
    return rules


def strip_ps_block_comments(line: str, in_block: bool) -> tuple[str, bool]:
    """(the line's code outside `<# #>`, whether a block is still open after it).

    A block can open and close several times on one line, with code before the first open and
    after the last close; a `<#` inside a quoted string is text, not a comment."""
    out: list[str] = []
    quote = ""
    i, n = 0, len(line)
    while i < n:
        if in_block:
            close = line.find(BLOCK_CLOSE, i)
            if close < 0:
                return "".join(out), True
            i, in_block = close + len(BLOCK_CLOSE), False
            continue
        ch = line[i]
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif line.startswith(BLOCK_OPEN, i):
            in_block = True
            i += len(BLOCK_OPEN)
            continue
        out.append(ch)
        i += 1
    return "".join(out), in_block


def findings_in(text: str, rel: str, rules: Sequence[Rule]) -> list[tuple[int, str, str]]:
    """[(line, rule label, the line)] for each non-comment line a rule matches outside its declared files."""
    live = [r for r in rules if rel not in r.declared]
    if not live:
        return []
    ps = pathlib.PurePosixPath(rel).suffix.lower() in POWERSHELL_SUFFIXES
    in_block = False
    out: list[tuple[int, str, str]] = []
    for n, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip("\r")
        code = line
        if ps:
            code, in_block = strip_ps_block_comments(line, in_block)
        if code.lstrip().startswith(COMMENT_STARTS):
            continue
        out.extend((n, r.label, line.strip()[:120]) for r in live if r.hits(code))
    return out


@dataclasses.dataclass
class Scan:
    findings: list[tuple[str, int, str, str]] = dataclasses.field(default_factory=list)  # rel, line, rule, text
    scanned: int = 0
    exempt: int = 0
    skipped: int = 0


def scan(root: pathlib.Path, rules: Sequence[Rule], exempt_suffixes: frozenset[str]) -> Scan:
    out = Scan()
    missing: list[str] = []
    for rel in discover.tracked((".",), suffix="", root=root):
        path = root / rel
        if path.is_symlink() or path.is_dir():
            out.skipped += 1
            continue
        if not path.is_file():
            missing.append(rel)
            continue
        out.scanned += 1
        if pathlib.PurePosixPath(rel).suffix.lower() in exempt_suffixes:
            out.exempt += 1
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        out.findings.extend((rel, ln, label, line) for ln, label, line in findings_in(text, rel, rules))
    if missing:
        print(f"{GATE}: DID NOT FINISH LOOKING -- {len(missing)} tracked path(s) not on disk "
              f"(deleted or sparse in this checkout): {', '.join(missing[:10])}", file=sys.stderr)
        raise SystemExit(2)
    return out


def _summary(seen: Scan, rules: Sequence[Rule], root: pathlib.Path) -> str:
    present = sum(1 for r in rules for d in r.declared if (root / d).is_file())
    declared = sum(len(r.declared) for r in rules)
    return (f"{seen.scanned} tracked file(s) scanned ({seen.exempt} by exempt suffix, {seen.skipped} "
            f"symlink/gitlink skipped); {len(rules)} rule(s) loaded: {', '.join(r.label for r in rules)}; "
            f"{present}/{declared} declared file(s) present")


def _report(found: Sequence[tuple[str, int, str, str]], seen: Scan, rules: Sequence[Rule],
            root: pathlib.Path) -> None:
    for rel, ln, label, line in found:
        print(f"  {rel}:{ln}: [{label}] {line}")
    if found:
        print(f"{GATE}: {len(found)} literal(s) outside the declared source -- {_summary(seen, rules, root)}")
        hit = {f[2] for f in found}
        for r in rules:
            if r.label in hit:
                where = ", ".join(r.declared) or "no file"
                print(f"  [{r.label}] declared in {where}. {r.message or DEFAULT_MESSAGE}")
    else:
        print(f"{GATE}: OK -- 0 literals outside the declared source; {_summary(seen, rules, root)}")


def _gate(argv: Sequence[str], root: pathlib.Path | None, rules: list[Rule],
          exempt_suffixes: frozenset[str]) -> int:
    seen = Scan()
    base: list[pathlib.Path] = []

    def run_scan() -> list[tuple[str, int, str, str]]:
        base.append(root or discover.repo_root())
        got = scan(base[0], rules, exempt_suffixes)
        seen.scanned, seen.exempt, seen.skipped = got.scanned, got.exempt, got.skipped
        return got.findings

    return site_lint.check(list(argv), gate=GATE, selftest=selftest, scanned=lambda: seen.scanned,
                           scan_dirs=(".",), scan=run_scan,
                           report=lambda found: _report(found, seen, rules, base[0]))


# ---------------------------------------------------------------- selftest (RFC 5737 addresses)
NET = "192.0." + "2."   # assembled: a documentation range, never a real one
_SELF_RULES = ["--rule", "net", f"--pattern=\\b{re.escape(NET)}\\d{{1,3}}\\b", "--declared", "conf/values.env",
               "--message", "read it from conf/values.env"]
_SELF_FILES = {
    "conf/values.env": f"GATEWAY={NET}1\n",
    "a.sh": f"# history: {NET}1 retired\nGW={NET}1\n",
    "b.yml": f"targets: ['{NET}61:9835']\n",
    "c.md": f"the hub is http://{NET}5:8088\n",
    # docstring (quiet), one-line block (quiet), code after the block closes (LOUD, line 6)
    "d.ps1": f"<#\n  held {NET}5 once\n#>\n<# one-liner {NET}61 #>\nparam()\n$gw = '{NET}1'\n",
    "e.ps1": f"<#\n$gw = '{NET}1'\n",   # unclosed: genuinely a comment to EOF, but still READ
    # code then a trailing block; a block closing with code after it; `<#` inside a string
    "g.ps1": f"$a = '{NET}1'  <# trailing #>\n<# opened\n#> $b = '{NET}1'\n"
             f"Write-Host \"use <# to open a block\"\n$c = '{NET}1'\n",
    "f.sh": f"N=1 <#\nGW='{NET}1'\n",   # `<#` opens nothing outside PowerShell
}
_SELF_WANT = {"a.sh:2", "b.yml:1", "d.ps1:6", "f.sh:2", "g.ps1:1", "g.ps1:3", "g.ps1:5", "c.md:1"}


def _run_quiet(root: pathlib.Path, args: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            rc = main([*args, "--root", str(root)])
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 2
    return rc, buf.getvalue()


def _repo(tmp: pathlib.Path, name: str, files: dict[str, str]) -> pathlib.Path:
    root = tmp / name
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body, encoding="utf-8", newline="\n")
    root.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    return root


def _cases(tmp: pathlib.Path) -> list[str]:
    fails: list[str] = []
    full = _repo(tmp, "full", _SELF_FILES)
    rc, out = _run_quiet(full, _SELF_RULES)
    got = {m.group(1) for m in re.finditer(r"^  (\S+:\d+): \[net\]", out, re.M)}
    if rc != 1 or got != _SELF_WANT:
        fails.append(f"planted tree: exit {rc} (want 1), flagged {sorted(got)}, want {sorted(_SELF_WANT)}")
    if "8 tracked file(s) scanned" not in out:
        fails.append(f"planted tree: the scanned count is not 8: {out.strip()[-200:]}")
    rc, out = _run_quiet(full, [*_SELF_RULES, "--exempt-suffix", ".md"])
    if rc != 1 or "c.md" in out:
        fails.append(f"--exempt-suffix .md: exit {rc}, c.md still flagged={('c.md' in out)}")
    clean = _repo(tmp, "clean", {"conf/values.env": f"GATEWAY={NET}1\n", "a.sh": "GW=$GATEWAY\n"})
    for label, args, want in (("clean tree", _SELF_RULES, 0), ("no rule", [], 2),
                              ("bad regex", ["--pattern", "(unclosed"], 2),
                              ("empty-match regex", ["--pattern", "x*"], 2),
                              ("rule without pattern", ["--rule", "lonely"], 2)):
        rc, out = _run_quiet(clean, args)
        if rc != want:
            fails.append(f"{label}: exit {rc} (want {want}): {out.strip()[:200]}")
    empty = _repo(tmp, "empty", {})
    rc, _ = _run_quiet(empty, _SELF_RULES)
    if rc != 2:
        fails.append(f"zero tracked files: exit {rc} (want 2 -- it did not look)")
    return fails


def selftest() -> list[str]:
    """Both directions on throwaway git repos: every planted shape fires, every exemption is quiet,
    and every way of not looking is 2. Mutation: drop the PowerShell suffix gate and f.sh:2 is missed;
    drop the string-quote check and g.ps1:5 is missed."""
    with tempfile.TemporaryDirectory() as d:
        try:
            return _cases(pathlib.Path(d))
        except (OSError, subprocess.CalledProcessError) as exc:
            return [f"could not build the selftest repos: {type(exc).__name__}: {exc}"]


def _suffix(value: str) -> str:
    s = value.strip().lower()
    if not re.fullmatch(r"\.[a-z0-9_.+-]+", s):
        raise argparse.ArgumentTypeError(f"{value!r} is not a suffix like .md")
    return s


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="ci-declared-literal", fromfile_prefix_chars="@",
                description=__doc__.split("\n", 1)[0])
    p.add_argument("--root", type=pathlib.Path, default=None)
    p.add_argument("--rule", action=_OpenRule, metavar="LABEL", help="open a rule; the flags after it belong to it")
    p.add_argument("--pattern", dest="patterns", action=_ToRule, metavar="REGEX", help="repeatable, OR'd")
    p.add_argument("--declared", dest="declared", action=_ToRule, metavar="PATH",
                   help="repo-relative file allowed to hold the literal (repeatable)")
    p.add_argument("--message", dest="message", action=_ToRule, metavar="TEXT", help="the remediation printed on a finding")
    p.add_argument("--exempt-suffix", type=_suffix, action="append", default=[], metavar=".EXT")
    p.add_argument("--selftest", action="store_true", help="prove every rule shape fires and stays quiet")
    p.set_defaults(rules=None)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = _parser().parse_args(argv)
    if args.selftest:
        return _gate(["--selftest"], None, [], frozenset())
    try:
        rules = compile_rules(args.rules or [])
    except ConfigError as exc:
        print(f"{GATE}: CANNOT CHECK (exit 2) -- {exc}", file=sys.stderr)
        return 2
    env_root = os.environ.get("CI_KIT_ROOT")  # same order as the kit's other gates
    root = args.root or (pathlib.Path(env_root) if env_root else None)
    return _gate([], root.resolve() if root else None, rules, frozenset(args.exempt_suffix))


if __name__ == "__main__":
    sys.exit(main())
