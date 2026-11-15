#!/usr/bin/env python3
"""declared_literal -- a literal that must live in ONE declared place appears nowhere else.

THE CLASS. Some values belong to exactly one file: an address range, a hostname, a port, an
account id. Everything else reads them from that file (an env file, a config template) -- so
moving the value is one edit, not a grep-and-pray sweep where one miss leaves a target pointing at
the old value. This gate holds the count of copies at ZERO: a line matching a rule's `--pattern`
in any tracked file that is not one of that rule's `--declared` files is a finding.

THE POLICY IS THE CALLER'S. The kit ships no pattern, no declared file and no message. A rule is
opened by `--rule LABEL`; the `--pattern` (Python `re`, repeatable, OR'd), `--example TEXT`
(repeatable, REQUIRED), `--declared PATH` (repo-relative, repeatable) and `--message TEXT` flags
after it belong to it. Flags before any `--rule` belong to one implicit rule. `--exempt-suffix
.EXT` (every rule) exempts a file kind from findings, e.g. `.md` when prose may name the value.

A RULE MUST PROVE IT CAN FIRE. A pattern that cannot match the value it guards (a stray trailing
space, a doubled backslash) would scan the tree and report clean forever. So: every `--example`
must be flagged as a live line by the rule, and every `--pattern` must match at least one example;
a `--pattern` with leading/trailing whitespace is refused; and a declared file that is PRESENT
must hold at least one match (an absent one is fine -- the repo may not carry the source). Each is
exit 2. There is no opt-out: a declared file that holds no match is a wrong path or a pattern
that cannot fire, and deleting the declaration costs nothing.

THE ARGS FILE. A regex cannot travel in the action's word-split `args`, so the policy lives in a
file: `ci-declared-literal @ci/literals.args`, resolved against the WORKING DIRECTORY (the
checkout root in the reusable workflow), not `--root`. One argument per line, read as UTF-8 (a BOM
is fine; UTF-16/32 or a NUL byte is exit 2 naming the encoding). Blank lines and lines starting
`#` are notes; a note directly after a flag that takes a value is exit 2 (it would silently shift
the next line into that flag), so a VALUE starting with `#` uses the `--opt=value` form.

EXEMPT LINES -- NEVER LOOSER THAN THE PREDECESSOR. A line whose first non-blank characters open a
comment may carry the value for history. The prefixes depend on the file kind
(`COMMENT_FAMILIES`), each a SUBSET of the six the predecessor honoured everywhere (`OLD_OPENERS`);
a kind the table does not know gets `#` only. Every LF line is also split at each other Unicode
line break (CR, form feed, NEL, U+2028, ...) and judged piece by piece, as the predecessor's
`splitlines` did. `--comment-prefix KEY=PREFIX` adds an opener for a suffix, a basename, or `*`
(every file but PowerShell) -- the caller's own, deliberate loosening. POWERSHELL runs TWO models
over the same pieces and a line is a finding if EITHER flags it: the predecessor's (`<# #>`
blocks, a same-line quote protects `<#`, then the six openers) and a stricter one that also knows
a backtick escape, a quote carried across lines, an `@"`/`@'` here-string (terminator at column 0
only) and a `#` line comment (at column 0 or after whitespace) that therefore opens no block. The
second can only ADD findings; when it is unsure, the first still decides. So nothing the
predecessor flagged is exempt here (a differential test in the suite holds this).

INPUT: `git ls-files` (never a filesystem walk), EVERY tracked path (`__pycache__` included).
Each file is read as UTF-8 with replacement (so a value inside a binary is seen), plus by its BOM
when it has one (UTF-8/16/32), plus as UTF-16-LE and UTF-16-BE when it holds a NUL byte: a match
in any reading is a finding. No suffix table and no threshold decide what is text.

Exit 0 ONLY after a scan that read at least one file under at least one live rule and found
nothing; 1 = findings; everything else is 2 (`CANNOT CHECK` / `DID NOT FINISH LOOKING`): no rule,
any policy fault above, `--selftest` mixed with a policy, git failing, nothing read, a tracked path
missing on disk, any unexpected error. There is no `--help`. With `CI_KIT_DECLARED_LITERAL_RECEIPT`
set, a completed scan writes `read=N rules=R findings=F` there, so a workflow can assert the gate
step SCANNED rather than trusting its exit code.

  ci-declared-literal [--root DIR] [--exempt-suffix .EXT ...] [--comment-prefix KEY=PREFIX ...]
                      [--rule LABEL] --pattern REGEX --example TEXT [--declared PATH ...]
                      [--message TEXT] ...
  ci-declared-literal --selftest
"""
from __future__ import annotations

import argparse
import codecs
import contextlib
import dataclasses
import io
import os
import pathlib
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence

from ci_kit import discover

GATE = "declared-literal"
IMPLICIT = "literal"
RECEIPT_ENV = "CI_KIT_DECLARED_LITERAL_RECEIPT"
POWERSHELL_SUFFIXES = frozenset({".ps1", ".psm1", ".psd1"})
DEFAULT_MESSAGE = "Read the value from the declared source instead; a comment line may keep it for history."
VALUE_FLAGS = frozenset({"--root", "--rule", "--pattern", "--example", "--declared", "--message",
                         "--exempt-suffix", "--comment-prefix"})

#: The openers the predecessor honoured in EVERY file. Each family below is a subset of it.
OLD_OPENERS = ("#", "//", "<!--", ";", "*", "--")
_HASH, _C, _DASH, _SEMI, _XML = ("#",), ("//", "*"), ("--",), (";",), ("<!--",)
COMMENT_FAMILIES: dict[str, tuple[str, ...]] = {
    **dict.fromkeys((".sh", ".bash", ".zsh", ".ksh", ".fish", ".py", ".pyi", ".rb", ".pl", ".pm",
                     ".r", ".yml", ".yaml", ".toml", ".conf", ".env", ".example", ".properties",
                     ".service", ".timer", ".socket", ".rules", ".mk", ".cmake", ".nix", ".tfvars",
                     ".gitignore", ".dockerignore", ".gitattributes", ".editorconfig", ".txt"), _HASH),
    **dict.fromkeys((".ini", ".cfg"), _HASH + _SEMI),
    **dict.fromkeys((".tf", ".hcl"), _HASH + _C),
    **dict.fromkeys((".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx", ".jsx", ".c", ".h", ".cc",
                     ".cpp", ".hpp", ".cs", ".java", ".kt", ".kts", ".go", ".rs", ".swift", ".scala",
                     ".dart", ".groovy", ".gradle", ".proto", ".jsonc", ".json5", ".scss", ".less"), _C),
    ".css": ("*",),
    **dict.fromkeys((".sql", ".lua", ".hs"), _DASH),
    **dict.fromkeys((".html", ".htm", ".xml", ".svg", ".xaml", ".csproj", ".props", ".targets",
                     ".md", ".markdown"), _XML),
    **dict.fromkeys((".vue", ".svelte"), _XML + _C),
    ".json": (),
    **dict.fromkeys(("Dockerfile", "Makefile", "Caddyfile", "Vagrantfile", "Gemfile", "Procfile",
                     "crontab", ".env"), _HASH),
}
FALLBACK_COMMENTS = _HASH

_BOMS = ((codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"),
         (codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"))


class ConfigError(Exception):
    """The policy as given cannot be checked -- exit 2, never a pass."""


@dataclasses.dataclass
class Rule:
    label: str
    patterns: list[str] = dataclasses.field(default_factory=list)
    examples: list[str] = dataclasses.field(default_factory=list)
    declared: list[str] = dataclasses.field(default_factory=list)
    message: str = ""
    compiled: list[re.Pattern[str]] = dataclasses.field(default_factory=list)

    def hits(self, code: str) -> bool:
        return any(p.search(code) for p in self.compiled)


# ---------------------------------------------------------------- the policy (argv + args files)
def _rules(ns: argparse.Namespace) -> list[Rule]:
    if getattr(ns, "rules", None) is None:
        ns.rules = []
    return ns.rules


class _OpenRule(argparse.Action):
    def __call__(self, parser, ns, value, option_string=None):  # type: ignore[override]
        _rules(ns).append(Rule(label=str(value)))


class _ToRule(argparse.Action):
    """`--pattern` / `--example` / `--declared` / `--message` attach to the rule opened last."""

    def __call__(self, parser, ns, value, option_string=None):  # type: ignore[override]
        rules = _rules(ns)
        if not rules:
            rules.append(Rule(label=IMPLICIT))
        if self.dest == "message":
            rules[-1].message = str(value)
        else:
            getattr(rules[-1], self.dest).append(str(value))


def read_args_file(path: str) -> list[str]:
    """One argument per line, UTF-8 (BOM allowed); blank and `#` lines are notes. Raises
    ConfigError naming the encoding (UTF-16/32, NUL bytes) or a note that would shift a value."""
    try:
        data = pathlib.Path(path).read_bytes()
    except OSError as exc:
        raise ConfigError(f"args file {path}: {exc}") from exc
    for bom, enc in _BOMS:
        if data.startswith(bom) and enc != "utf-8":
            raise ConfigError(f"args file {path} is {enc.upper()} (a PowerShell 5.1 `>` or Out-File "
                              f"default); save it as UTF-8")
    if b"\0" in data:
        raise ConfigError(f"args file {path} holds NUL bytes (UTF-16 without a BOM?); save it as UTF-8")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"args file {path} is not UTF-8 ({exc.reason} at byte {exc.start})") from exc
    out: list[str] = []
    for n, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip("\r")
        if line.strip() and not line.strip().startswith("#"):
            out.append(line)
        elif out and out[-1] in VALUE_FLAGS:
            raise ConfigError(f"args file {path}:{n}: a note or blank line right after {out[-1]} -- the "
                              f"NEXT line would become its value; write {out[-1]}=<value> on one line")
    return out


def expand_args(argv: Sequence[str], depth: int = 0) -> list[str]:
    """`@file` arguments replaced by the file's arguments (nested files allowed, 10 deep)."""
    if depth > 10:
        raise ConfigError("args files nest more than 10 deep (a file including itself?)")
    out: list[str] = []
    for a in argv:
        if not a.startswith("@"):
            out.append(a)
            continue
        got = expand_args(read_args_file(a[1:]), depth + 1)
        if "--selftest" in got:
            raise ConfigError(f"args file {a[1:]} holds --selftest: a policy file must describe the "
                              f"check of the tree, never replace it with the kit's own proof")
        out.extend(got)
    return out


def compile_rules(rules: list[Rule], comments: Mapping[str, tuple[str, ...]] | None = None) -> list[Rule]:
    """Validate and compile, or raise ConfigError naming exactly what is wrong."""
    if not rules:
        raise ConfigError("no --pattern given: the kit holds no policy of its own, so with no rule "
                          "there is nothing to check (pass an args file: @ci/literals.args)")
    seen: set[str] = set()
    for rule in rules:
        name = f"rule {rule.label!r}"
        if rule.label in seen:
            raise ConfigError(f"{name} is opened twice")
        seen.add(rule.label)
        if not rule.patterns:
            raise ConfigError(f"{name} has no --pattern")
        for pat in rule.patterns:
            if not pat:
                raise ConfigError(f"{name}: an empty --pattern matches every line")
            if pat != pat.strip():
                raise ConfigError(f"{name}: --pattern {pat!r} has leading/trailing whitespace "
                                  f"(a stray space in the args file?); it would never match the value")
            try:
                rx = re.compile(pat)
            except (re.error, OverflowError, RecursionError) as exc:
                raise ConfigError(f"{name}: --pattern {pat!r} does not compile: {exc}") from exc
            if rx.search(""):
                raise ConfigError(f"{name}: --pattern {pat!r} matches the empty string, so every line")
            rule.compiled.append(rx)
        _prove(rule, comments or {})
        rule.declared = [d.replace("\\", "/").removeprefix("./") for d in rule.declared]
    return rules


def _prove(rule: Rule, comments: Mapping[str, tuple[str, ...]]) -> None:
    """The rule's own must-fire proof: every example is a finding, every pattern fires on one."""
    name = f"rule {rule.label!r}"
    if not rule.examples:
        raise ConfigError(f"{name} has no --example: give a line the rule MUST flag (the value as "
                          f"it would be written), so a pattern that cannot fire is caught here")
    probe = Rule(label=rule.label, compiled=rule.compiled)
    for ex in rule.examples:
        if not findings_in(ex, "<example>", [probe], comments):
            raise ConfigError(f"{name}: --example {ex!r} is not flagged -- the pattern cannot fire "
                              f"on it (escaping? whitespace?), or the example is a comment line")
    for pat, rx in zip(rule.patterns, rule.compiled, strict=True):
        if not any(rx.search(ex) for ex in rule.examples):
            raise ConfigError(f"{name}: --pattern {pat!r} matches none of the rule's --example lines")


def _comment_prefix(value: str) -> tuple[str, str]:
    key, sep, prefix = value.partition("=")
    if not sep or not key or not prefix.strip():
        raise argparse.ArgumentTypeError(f"{value!r} is not KEY=PREFIX (KEY = .ext, a basename, or *)")
    key = key.lower() if key.startswith(".") else key
    if key in POWERSHELL_SUFFIXES:
        raise argparse.ArgumentTypeError(f"{key}: PowerShell comments are parsed, not prefix-matched; "
                                         f"--comment-prefix cannot change them")
    return key, prefix.strip()


def comment_table(extra: Sequence[tuple[str, str]]) -> dict[str, tuple[str, ...]]:
    table = dict(COMMENT_FAMILIES)
    for key, prefix in extra:
        table[key] = table.get(key, FALLBACK_COMMENTS if key != "*" else ()) + (prefix,)
    return table


def prefixes_for(rel: str, table: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
    p = pathlib.PurePosixPath(rel)
    base = table.get(p.name, table.get(p.suffix.lower(), FALLBACK_COMMENTS))
    return base + table.get("*", ())


# ---------------------------------------------------------------- PowerShell: two models, either flags
class _PsFloor:
    """The predecessor's model, kept as the FLOOR: `<#` opens a block unless inside a quote opened
    on the same line; `#>` closes it; the code left is judged against all six old openers."""

    def __init__(self) -> None:
        self.block = False

    def live(self, piece: str) -> str | None:
        kept, quote, i = [], "", 0
        while i < len(piece):
            if self.block:
                end = piece.find("#>", i)
                if end < 0:
                    break
                self.block, i = False, end + 2
                continue
            c = piece[i]
            if quote:
                quote = "" if c == quote else quote
            elif c in "'\"":
                quote = c
            elif piece.startswith("<#", i):
                self.block, i = True, i + 2
                continue
            kept.append(c)
            i += 1
        code = "".join(kept)
        return None if code.lstrip().startswith(OLD_OPENERS) else code


class _PsStrict:
    """A stricter model: it keeps MORE text live, never less than it can justify. A quote (with
    backtick escapes in `"`) carries across lines; `@"`/`@'` at end of line opens a here-string
    that only a terminator at COLUMN 0 closes; inside either, `<#` and `#` are text. `#` at column
    0 or after whitespace starts a line comment, which therefore opens no block."""

    def __init__(self) -> None:
        self.block, self.quote, self.here = False, "", ""

    def live(self, piece: str) -> str | None:
        i, kept = 0, []
        if self.here:
            if not piece.startswith(self.here[1] + "@"):
                return piece
            kept.append(piece[:2])
            self.here, i = "", 2
        while i < len(piece):
            c = piece[i]
            if self.block:
                end = piece.find("#>", i)
                if end < 0:
                    break
                self.block, i = False, end + 2
                continue
            if self.quote:
                kept.append(c)
                if self.quote == '"' and c == "`" and i + 1 < len(piece):
                    kept.append(piece[i + 1])
                    i += 2
                    continue
                self.quote = "" if c == self.quote else self.quote
            elif piece.startswith(("@\"", "@'"), i) and not piece[i + 2:].strip():
                self.here = piece[i:i + 2]
                kept.append(piece[i:])
                break
            elif c in "'\"":
                self.quote = c
                kept.append(c)
            elif piece.startswith("<#", i):
                self.block, i = True, i + 2
                continue
            elif c == "#" and (i == 0 or piece[i - 1].isspace()):
                break
            else:
                kept.append(c)
            i += 1
        code = "".join(kept)
        return None if not code.strip() else code


# ---------------------------------------------------------------- the predicate
def findings_in(text: str, rel: str, rules: Sequence[Rule],
                comments: Mapping[str, tuple[str, ...]] | None = None) -> list[tuple[int, str, str]]:
    """[(line, rule label, the line)] for each live line a rule matches outside its declared files.

    Lines are numbered by LF; each is split again at every other line break and judged per piece."""
    live = [r for r in rules if rel not in r.declared]
    if not live:
        return []
    table = COMMENT_FAMILIES if comments is None else comments
    openers = prefixes_for(rel, table)
    ps = pathlib.PurePosixPath(rel).suffix.lower() in POWERSHELL_SUFFIXES
    floor, strict = _PsFloor(), _PsStrict()
    out: list[tuple[int, str, str]] = []
    for n, raw in enumerate(text.split("\n"), 1):
        pieces = raw.splitlines() or [""]
        labels: list[str] = []
        for piece in pieces:
            if ps:
                codes = [c for c in (floor.live(piece), strict.live(piece)) if c is not None]
            else:
                codes = [] if openers and piece.lstrip().startswith(openers) else [piece]
            for r in live:
                if r.label not in labels and any(r.hits(c) for c in codes):
                    labels.append(r.label)
        shown = " ".join(p.strip() for p in pieces).strip()[:120]
        out.extend((n, label, shown) for label in labels)
    return out


# ---------------------------------------------------------------- the scan
@dataclasses.dataclass
class Scan:
    findings: list[tuple[str, int, str, str]] = dataclasses.field(default_factory=list)  # rel, line, rule, text
    read: int = 0
    exempt: int = 0
    declared_only: int = 0
    skipped: int = 0


def tracked_all(root: pathlib.Path) -> list[str]:
    """EVERY tracked path, repo-relative -- `discover.tracked` without its `__pycache__` skip, which
    is right for the code gates that call it and wrong for a gate that promises every file."""
    try:
        r = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=False,
                           timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(f"could not run git ls-files: {exc}") from exc
    if r.returncode != 0:
        raise ConfigError(f"git ls-files failed in {root}: {r.stderr.decode(errors='replace').strip()}")
    return sorted(p for p in r.stdout.decode("utf-8", errors="replace").split("\0") if p)


def readings(data: bytes) -> list[str]:
    """Every reading of the bytes a value could hide in: UTF-8 (replacement), the BOM's encoding,
    and UTF-16-LE/BE when a NUL byte is present (UTF-16 without a BOM, whatever the file's name)."""
    out = [data.decode("utf-8", errors="replace")]
    for bom, enc in _BOMS:
        if data.startswith(bom):
            out.append(data[len(bom):].decode(enc, errors="replace"))
            break
    if b"\0" in data:
        out += [data.decode("utf-16-le", errors="replace"), data.decode("utf-16-be", errors="replace")]
    return out


def scan(root: pathlib.Path, rules: Sequence[Rule], exempt_suffixes: frozenset[str],
         comments: Mapping[str, tuple[str, ...]]) -> Scan:
    out = Scan()
    blind: list[str] = []
    for rel in tracked_all(root):
        path = root / rel
        if path.is_symlink() or path.is_dir():
            out.skipped += 1
            continue
        if not path.is_file():
            blind.append(f"{rel} is tracked but not on disk (deleted or sparse in this checkout)")
            continue
        declared_by = [r for r in rules if rel in r.declared]
        exempt = pathlib.PurePosixPath(rel).suffix.lower() in exempt_suffixes
        if exempt and not declared_by:
            out.exempt += 1
            continue
        texts = readings(path.read_bytes())
        for r in declared_by:
            if not any(r.hits(t) for t in texts):
                blind.append(f"rule {r.label!r}: declared file {rel} is present but holds no match -- "
                             f"a wrong path, or a pattern that cannot fire on the value it guards")
        if exempt:
            out.exempt += 1
            continue
        if len(declared_by) == len(rules):
            out.declared_only += 1
            continue
        out.read += 1
        seen: set[tuple[int, str]] = set()
        for t in texts:
            for ln, label, line in findings_in(t, rel, rules, comments):
                if (ln, label) not in seen:
                    seen.add((ln, label))
                    out.findings.append((rel, ln, label, line))
    if blind:
        print(f"{GATE}: DID NOT FINISH LOOKING -- {len(blind)} problem(s):", file=sys.stderr)
        for b in blind[:20]:
            print(f"  {b}", file=sys.stderr)
        raise SystemExit(2)
    return out


def _summary(seen: Scan, rules: Sequence[Rule], root: pathlib.Path) -> str:
    present = sum(1 for r in rules for d in r.declared if (root / d).is_file())
    declared = sum(len(r.declared) for r in rules)
    return (f"{seen.read} tracked file(s) read ({seen.exempt} exempt by suffix, {seen.declared_only} "
            f"declared-only, {seen.skipped} symlink/gitlink skipped); {len(rules)} rule(s) loaded: "
            f"{', '.join(r.label for r in rules)}; {present}/{declared} declared file(s) present")


def _report(seen: Scan, rules: Sequence[Rule], root: pathlib.Path) -> None:
    found = seen.findings
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
    receipt = os.environ.get(RECEIPT_ENV)
    if receipt:
        pathlib.Path(receipt).write_text(f"read={seen.read} rules={len(rules)} findings={len(found)}\n",
                                         encoding="utf-8")


# ---------------------------------------------------------------- selftest (RFC 5737 addresses)
NET = "192.0." + "2."   # assembled: a documentation range, never a real one
_SELF_RULES = ["--rule", "net", f"--pattern=\\b{re.escape(NET)}\\d{{1,3}}\\b", f"--example=GW={NET}1",
               "--declared", "conf/values.env", "--message", "read it from conf/values.env"]
_SELF_FILES = {
    "conf/values.env": f"GATEWAY={NET}1\n",
    "a.sh": f"# history: {NET}1 retired\nGW={NET}1\n",
    "b.yml": f"targets: ['{NET}10:9100']\n",
    "c.md": f"the site is http://{NET}5\n",
    # docstring (quiet), one-line block (quiet), code after the block closes (LOUD, line 6)
    "d.ps1": f"<#\n  held {NET}5 once\n#>\n<# one-liner {NET}10 #>\nparam()\n$gw = '{NET}1'\n",
    "e.ps1": f"<#\n$gw = '{NET}1'\n",   # unclosed: genuinely a comment to EOF, but still READ
    # code then a trailing block; a block closing with code after it; `<#` inside a string
    "g.ps1": f"$a = '{NET}1'  <# trailing #>\n<# opened\n#> $b = '{NET}1'\n"
             f"Write-Host \"use <# to open a block\"\n$c = '{NET}1'\n",
    # a `#` line comment opens no block; a here-string's `<#` is text (the strict model's gain)
    "h.ps1": f"# see <# in docs\n$d = '{NET}1'\n$s = @\"\nuse <# here\n\"@\n$e = '{NET}1'\n",
    # `#` mid-word, in a URL, backtick-escaped, and inside a string carried across lines: all LIVE
    "r.ps1": f"ping gw#{NET}1\nStart-Process http://h/#/{NET}1\necho `#{NET}1\n$m = \"first\n"
             f"second # {NET}1\"\n",
    "f.sh": f"N=1 <#\nGW='{NET}1'\n",   # `<#` opens nothing outside PowerShell
    # a comment opener before a NON-LF line break shelters nothing (CR, form feed, U+2028)
    "k.sh": f"# note\rGW={NET}1\r\n# note\fGW={NET}1\n",
    "k.js": f"// note const h = '{NET}1'\n",
    # `--` / `*` / `#` are comments only where the file kind says so
    "m.sh": f"docker run \\\n  --add-host=gw:{NET}1 \\\n  img\n",
    "m.c": f"#define HOST \"{NET}1\"\n",
}
_SELF_WANT = {"a.sh:2", "b.yml:1", "d.ps1:6", "f.sh:2", "g.ps1:1", "g.ps1:3", "g.ps1:5", "c.md:1",
              "h.ps1:2", "h.ps1:6", "k.sh:1", "k.sh:2", "k.js:1", "m.sh:2", "m.c:1",
              "r.ps1:1", "r.ps1:2", "r.ps1:3", "r.ps1:5"}


def _run_quiet(root: pathlib.Path, args: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = main([*args, "--root", str(root)])
    return rc, buf.getvalue()


def _repo(tmp: pathlib.Path, name: str, files: dict[str, str]) -> pathlib.Path:
    root = tmp / name
    root.mkdir()
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(body.encode("utf-8"))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    return root


def _cases(tmp: pathlib.Path) -> list[str]:
    fails: list[str] = []
    full = _repo(tmp, "full", _SELF_FILES)
    rc, out = _run_quiet(full, _SELF_RULES)
    got = {m.group(1) for m in re.finditer(r"^  (\S+:\d+): \[net\]", out, re.M)}
    if rc != 1 or got != _SELF_WANT:
        fails.append(f"planted tree: exit {rc} (want 1), missed {sorted(_SELF_WANT - got)}, "
                     f"extra {sorted(got - _SELF_WANT)}")
    if f"{len(_SELF_FILES) - 1} tracked file(s) read" not in out:
        fails.append(f"planted tree: the read count is not {len(_SELF_FILES) - 1}: {out.strip()[-200:]}")
    rc, out = _run_quiet(full, [*_SELF_RULES, "--exempt-suffix", ".md"])
    if rc != 1 or "c.md" in out:
        fails.append(f"--exempt-suffix .md: exit {rc}, c.md still flagged={('c.md' in out)}")
    clean = _repo(tmp, "clean", {"conf/values.env": f"GATEWAY={NET}1\n", "a.sh": "GW=$GATEWAY\n"})
    bad_policies = (
        ("clean tree", _SELF_RULES, 0), ("no rule", [], 2), ("help", ["--help", *_SELF_RULES], 2),
        ("bad regex", ["--pattern", "(unclosed", "--example", "x"], 2),
        ("overflowing regex", ["--pattern", "a{99999999999999999999}", "--example", "a"], 2),
        ("empty-match regex", ["--pattern", "x*", "--example", "x"], 2),
        ("rule without pattern", ["--rule", "lonely"], 2),
        ("no example", ["--pattern", "GW"], 2),
        ("example the pattern cannot flag", [f"--pattern=\\\\b{re.escape(NET)}1", f"--example=GW={NET}1"], 2),
        ("trailing space", [f"--pattern={re.escape(NET)} ", f"--example=GW={NET} 1"], 2),
        ("declared file holding no match", ["--pattern", "GATEWAY=10", "--example", "GATEWAY=10",
                                             "--declared", "conf/values.env"], 2),
        ("selftest mixed with a policy", ["--selftest", *_SELF_RULES], 2),
    )
    for label, args, want in bad_policies:
        rc, out = _run_quiet(clean, args)
        if rc != want:
            fails.append(f"{label}: exit {rc} (want {want}): {out.strip()[:200]}")
    rc, _ = _run_quiet(_repo(tmp, "empty", {}), _SELF_RULES)
    if rc != 2:
        fails.append(f"zero tracked files: exit {rc} (want 2 -- it did not look)")
    return fails


def selftest() -> list[str]:
    """Both directions on throwaway git repos: every planted shape fires, every exemption is quiet,
    and every way of not looking or of a policy that cannot fire is 2."""
    receipt = os.environ.pop(RECEIPT_ENV, None)   # the proof's inner scans must never write a receipt
    try:
        with tempfile.TemporaryDirectory() as d:
            return _cases(pathlib.Path(d))
    except (OSError, subprocess.CalledProcessError) as exc:
        return [f"could not build the selftest repos: {type(exc).__name__}: {exc}"]
    finally:
        if receipt is not None:
            os.environ[RECEIPT_ENV] = receipt


# ---------------------------------------------------------------- CLI
def _suffix(value: str) -> str:
    s = value.strip().lower()
    if not re.fullmatch(r"\.[a-z0-9_.+-]+", s):
        raise argparse.ArgumentTypeError(f"{value!r} is not a suffix like .md")
    return s


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # type: ignore[override]
        raise ConfigError(f"usage: {message}")


def _parser() -> argparse.ArgumentParser:
    # add_help=False: `--help` exiting 0 is a green gate step that never scanned
    p = _Parser(prog="ci-declared-literal", allow_abbrev=False, add_help=False,
                description=__doc__.split("\n", 1)[0])
    p.add_argument("--root", type=pathlib.Path, default=None)
    p.add_argument("--rule", action=_OpenRule, metavar="LABEL")
    p.add_argument("--pattern", dest="patterns", action=_ToRule, metavar="REGEX")
    p.add_argument("--example", dest="examples", action=_ToRule, metavar="TEXT")
    p.add_argument("--declared", dest="declared", action=_ToRule, metavar="PATH")
    p.add_argument("--message", dest="message", action=_ToRule, metavar="TEXT")
    p.add_argument("--exempt-suffix", type=_suffix, action="append", default=[], metavar=".EXT")
    p.add_argument("--comment-prefix", type=_comment_prefix, action="append", default=[], metavar="KEY=PREFIX")
    p.add_argument("--selftest", action="store_true")
    p.set_defaults(rules=None)
    return p


def _selftest_mode() -> int:
    fails = selftest()
    for f in fails:
        print(f"  SELFTEST FAIL: {f}")
    print(f"{GATE}: selftest {'FAILED' if fails else 'OK'}")
    return 1 if fails else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0 is returned from ONE place: after a scan that read >= 1 file under a live rule and
    found nothing. Every other path -- a usage error, any exception -- is 1 (findings) or 2."""
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    try:
        args = _parser().parse_args(expand_args(argv))
        if args.selftest:
            if args.rules or args.exempt_suffix or args.comment_prefix or args.root:
                raise ConfigError("--selftest takes no policy: a run that only proves itself must "
                                  "never stand in for the check of the tree")
            return _selftest_mode()
        comments = comment_table(args.comment_prefix)
        rules = compile_rules(args.rules or [], comments)
        env_root = os.environ.get("CI_KIT_ROOT")  # same order as the kit's other gates
        root = (args.root or (pathlib.Path(env_root) if env_root else None) or discover.repo_root()).resolve()
        seen = scan(root, rules, frozenset(args.exempt_suffix), comments)
        if seen.read < 1:
            raise ConfigError(f"read 0 files under a live rule ({seen.exempt} exempt, {seen.declared_only} "
                              f"declared-only): a gate that read nothing has verified nothing")
        _report(seen, rules, root)
    except SystemExit as exc:   # discover / scan say "could not look" this way; never let it be 0
        code = exc.code if isinstance(exc.code, int) and exc.code else 2
        return 2 if code == 0 else code
    except ConfigError as exc:
        print(f"{GATE}: CANNOT CHECK (exit 2) -- {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - an unexpected error is "could not look", never 1 or 0
        print(f"{GATE}: CANNOT CHECK (exit 2) -- {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 1 if seen.findings else 0


if __name__ == "__main__":
    sys.exit(main())
