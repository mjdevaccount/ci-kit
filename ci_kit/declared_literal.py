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
.EXT` (every rule) exempts a file kind wholesale, e.g. `.md` when prose may name the value.

A RULE MUST PROVE IT CAN FIRE. A pattern that cannot match the value it guards (a stray trailing
space, a doubled backslash) would scan the tree and report clean forever. So: every `--example`
must be flagged as a live line by the rule, and every `--pattern` must match at least one example;
a `--pattern` with leading/trailing whitespace is refused; and a declared file that is PRESENT
must hold at least one match (a declared file that is absent is fine -- the repo may not carry
the source). Each is exit 2. There is no opt-out: a declared file that holds no match is a wrong
path or a pattern that cannot fire, and deleting the declaration costs nothing.

THE ARGS FILE. A regex cannot travel in the action's word-split `args`, so the policy lives in a
file: `ci-declared-literal @ci/literals.args`, one argument per line, read as UTF-8 (a BOM is
fine; UTF-16 or a NUL byte is exit 2 naming the encoding). Blank lines and lines starting `#`
are notes, so a VALUE starting with `#` must use the `--opt=value` form. `--pattern=<regex>` on
one line keeps spaces and backslashes whole.

EXEMPT LINES. A line whose first non-blank characters open a comment may carry the value for
history. The prefixes depend on the file kind (`COMMENT_FAMILIES`: `#` for shell/YAML/Python/
config, `//` and a `*` continuation for C-family, `--` for SQL/Lua, `;` for ini, `<!--` for
markup; a suffix the table does not know gets `#` only); `--comment-prefix KEY=PREFIX` adds one
for a suffix (`.j2`), a basename (`Dockerfile`), or `*` (every file). Each physical line is
also split at every Unicode line break (`str.splitlines`: CR, form feed, NEL, U+2028, ...), so a
comment opener cannot shelter code after a break that is not LF. In PowerShell
(`POWERSHELL_SUFFIXES`) comments are STRIPPED and what is left is scanned: `#` line comments,
`<# ... #>` blocks across lines, with quoted strings (backtick escapes) and `@"..."@` /
`@'...'@` here-strings read as text, never as comment openers.

INPUT: `git ls-files` (never a filesystem walk), EVERY tracked path (`__pycache__` included).
A file is decoded by its BOM (UTF-8/16/32), else as UTF-8 with replacement, so a value inside a
binary is still seen; a file under a TEXT suffix (`hidden_unicode.TEXT_SUFFIXES`) with no BOM
whose bytes are a quarter or more NUL (UTF-16 without a BOM) is exit 2 -- it cannot be read as
text. A stray NUL in UTF-8 text (a separator in a string) is read like any other character. Symlinks and gitlinks are skipped.

Exit 0 clean, 1 findings, 2 could not look: no rule, any policy fault above, `--selftest` mixed
with a policy, git failing, no file read under a live rule, a tracked path missing on disk, an
unreadable file. The last line says files read, exempt, declared-only, skipped and rules loaded.

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

from ci_kit import discover, site_lint
from ci_kit.hidden_unicode import TEXT_SUFFIXES

GATE = "declared-literal"
IMPLICIT = "literal"
POWERSHELL_SUFFIXES = frozenset({".ps1", ".psm1", ".psd1"})
DEFAULT_MESSAGE = "Read the value from the declared source instead; a comment line may keep it for history."

_HASH, _C, _DASH, _SEMI, _XML = ("#",), ("//", "*"), ("--",), (";",), ("<!--",)
#: Comment openers by file kind. Every entry is a SUBSET of the six openers the predecessor
#: honoured everywhere (`#`, `//`, `*`, `--`, `;`, `<!--`), so no kind is looser than before.
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
    ConfigError naming the encoding when the file is UTF-16/32 or holds a NUL byte."""
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
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    return [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]


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
            except re.error as exc:
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
    return (key.lower() if key.startswith(".") else key), prefix.strip()


def comment_table(extra: Sequence[tuple[str, str]]) -> dict[str, tuple[str, ...]]:
    table = dict(COMMENT_FAMILIES)
    for key, prefix in extra:
        table[key] = table.get(key, FALLBACK_COMMENTS if key != "*" else ()) + (prefix,)
    return table


def prefixes_for(rel: str, table: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
    p = pathlib.PurePosixPath(rel)
    base = table.get(p.name, table.get(p.suffix.lower(), FALLBACK_COMMENTS))
    return base + table.get("*", ())


# ---------------------------------------------------------------- the predicate
def strip_ps(line: str, state: str) -> tuple[str, str]:
    """(the PowerShell code on `line` with comments removed, the state after it).

    state: "" code, "<#" inside a block comment, '@"' / "@'" inside a here-string. A here-string
    is TEXT (kept and scanned), a `<#` or `#` inside a string or here-string is not a comment,
    and a `#` line comment ends the line -- it can open no block."""
    out: list[str] = []
    i, n, quote = 0, len(line), ""
    if state in ('@"', "@'"):
        if line.lstrip().startswith(state[1] + "@"):
            j = line.index(state[1] + "@") + 2
            out.append(line[:j])
            i, state = j, ""
        else:
            return line, state
    while i < n:
        if state == "<#":
            close = line.find("#>", i)
            if close < 0:
                return "".join(out), state
            i, state = close + 2, ""
            continue
        ch = line[i]
        if quote:
            out.append(ch)
            if quote == '"' and ch == "`" and i + 1 < n:
                out.append(line[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if line.startswith(("@\"", "@'"), i) and not line[i + 2:].strip():
            out.append(line[i:])
            return "".join(out), line[i:i + 2]
        if ch in "'\"":
            quote = ch
        elif line.startswith("<#", i):
            state, i = "<#", i + 2
            continue
        elif ch == "#":
            return "".join(out), state
        out.append(ch)
        i += 1
    return "".join(out), state


def findings_in(text: str, rel: str, rules: Sequence[Rule],
                comments: Mapping[str, tuple[str, ...]] | None = None) -> list[tuple[int, str, str]]:
    """[(line, rule label, the line)] for each live line a rule matches outside its declared files.

    Lines are numbered by LF; each is split again at every other line break, and the comment test
    is applied per piece, so `# note<CR>GW=<value>` is a finding, as it was to the predecessor."""
    live = [r for r in rules if rel not in r.declared]
    if not live:
        return []
    table = COMMENT_FAMILIES if comments is None else comments
    openers = prefixes_for(rel, table)
    ps = pathlib.PurePosixPath(rel).suffix.lower() in POWERSHELL_SUFFIXES
    state = ""
    out: list[tuple[int, str, str]] = []
    for n, raw in enumerate(text.split("\n"), 1):
        pieces = raw.splitlines() or [""]
        labels: list[str] = []
        for piece in pieces:
            code = piece
            if ps:
                code, state = strip_ps(piece, state)
            elif openers and code.lstrip().startswith(openers):
                continue
            labels.extend(r.label for r in live if r.label not in labels and r.hits(code))
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
        print(f"{GATE}: could not run git ls-files: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if r.returncode != 0:
        print(f"{GATE}: git ls-files failed in {root}: {r.stderr.decode(errors='replace').strip()}",
              file=sys.stderr)
        raise SystemExit(2)
    return sorted(p for p in r.stdout.decode("utf-8", errors="replace").split("\0") if p)


def decode(data: bytes, rel: str) -> str | None:
    """The text by BOM, else UTF-8 with replacement; None for a text-suffix file it cannot read."""
    for bom, enc in _BOMS:
        if data.startswith(bom):
            return data[len(bom):].decode(enc, errors="replace")
    # UTF-16 without a BOM: ASCII text is every other byte NUL (and still valid UTF-8, so a decode
    # cannot tell). A NUL or two is a separator in UTF-8 text (a JS template key) and is read.
    if data.count(b"\0") * 4 >= len(data) > 0 and pathlib.PurePosixPath(rel).suffix.lower() in TEXT_SUFFIXES:
        return None
    return data.decode("utf-8", errors="replace")


def _declared_fault(rule: Rule, rel: str, text: str) -> str | None:
    if rel in rule.declared and not any(rx.search(text) for rx in rule.compiled):
        return (f"rule {rule.label!r}: declared file {rel} is present but holds no match -- a wrong "
                f"path, or a pattern that cannot fire on the value it guards")
    return None


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
        is_declared = any(rel in r.declared for r in rules)
        if pathlib.PurePosixPath(rel).suffix.lower() in exempt_suffixes and not is_declared:
            out.exempt += 1
            continue
        text = decode(path.read_bytes(), rel)
        if text is None:
            blind.append(f"{rel} is mostly NUL bytes under a text suffix with no BOM (UTF-16?) -- "
                         f"re-save it as UTF-8")
            continue
        blind.extend(f for r in rules if (f := _declared_fault(r, rel, text)))
        if all(rel in r.declared for r in rules):
            out.declared_only += 1
            continue
        out.read += 1
        out.findings.extend((rel, ln, label, line) for ln, label, line in findings_in(text, rel, rules, comments))
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
          exempt_suffixes: frozenset[str], comments: Mapping[str, tuple[str, ...]]) -> int:
    seen = Scan()
    base: list[pathlib.Path] = []

    def run_scan() -> list[tuple[str, int, str, str]]:
        base.append(root or discover.repo_root())
        got = scan(base[0], rules, exempt_suffixes, comments)
        seen.read, seen.exempt, seen.declared_only, seen.skipped = (got.read, got.exempt,
                                                                    got.declared_only, got.skipped)
        return got.findings

    return site_lint.check(list(argv), gate=GATE, selftest=selftest, scanned=lambda: seen.read,
                           scan_dirs=(".",), scan=run_scan,
                           report=lambda found: _report(found, seen, rules, base[0]))


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
    # a `#` line comment opens no block; a here-string's `<#` is text
    "h.ps1": f"# see <# in docs\n$d = '{NET}1'\n$s = @\"\nuse <# here\n\"@\n$e = '{NET}1'\n",
    "f.sh": f"N=1 <#\nGW='{NET}1'\n",   # `<#` opens nothing outside PowerShell
    # a comment opener before a NON-LF line break shelters nothing (CR, form feed, U+2028)
    "k.sh": f"# note\rGW={NET}1\r\n# note\fGW={NET}1\n",
    "k.js": f"// note const h = '{NET}1'\n",
    # `--` / `*` / `#` are comments only where the file kind says so
    "m.sh": f"docker run \\\n  --add-host=gw:{NET}1 \\\n  img\n",
    "m.c": f"#define HOST \"{NET}1\"\n",
}
_SELF_WANT = {"a.sh:2", "b.yml:1", "d.ps1:6", "f.sh:2", "g.ps1:1", "g.ps1:3", "g.ps1:5", "c.md:1",
              "h.ps1:2", "h.ps1:6", "k.sh:1", "k.sh:2", "k.js:1", "m.sh:2", "m.c:1"}


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
        ("clean tree", _SELF_RULES, 0), ("no rule", [], 2),
        ("bad regex", ["--pattern", "(unclosed", "--example", "x"], 2),
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
    empty = _repo(tmp, "empty", {})
    rc, _ = _run_quiet(empty, _SELF_RULES)
    if rc != 2:
        fails.append(f"zero tracked files: exit {rc} (want 2 -- it did not look)")
    return fails


def selftest() -> list[str]:
    """Both directions on throwaway git repos: every planted shape fires, every exemption is quiet,
    and every way of not looking or of a policy that cannot fire is 2. Mutations that kill it:
    PowerShell stripping on for every file (f.sh:2), the string-quote check (g.ps1:5), the
    here-string state (h.ps1:6), the `#` line comment in PowerShell (h.ps1:2), the per-piece
    split (k.sh/k.js), the per-kind comment table (m.sh:2, m.c:1), each policy proof."""
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
    p = argparse.ArgumentParser(prog="ci-declared-literal", allow_abbrev=False,
                                description=__doc__.split("\n", 1)[0])
    p.add_argument("--root", type=pathlib.Path, default=None)
    p.add_argument("--rule", action=_OpenRule, metavar="LABEL", help="open a rule; the flags after it belong to it")
    p.add_argument("--pattern", dest="patterns", action=_ToRule, metavar="REGEX", help="repeatable, OR'd")
    p.add_argument("--example", dest="examples", action=_ToRule, metavar="TEXT",
                   help="a line the rule MUST flag (required, repeatable): the rule's own must-fire proof")
    p.add_argument("--declared", dest="declared", action=_ToRule, metavar="PATH",
                   help="repo-relative file allowed to hold the literal (repeatable)")
    p.add_argument("--message", dest="message", action=_ToRule, metavar="TEXT", help="the remediation printed on a finding")
    p.add_argument("--exempt-suffix", type=_suffix, action="append", default=[], metavar=".EXT")
    p.add_argument("--comment-prefix", type=_comment_prefix, action="append", default=[], metavar="KEY=PREFIX",
                   help="one more comment opener for a suffix (.j2), a basename, or * (repeatable)")
    p.add_argument("--selftest", action="store_true", help="prove every rule shape fires and stays quiet")
    p.set_defaults(rules=None)
    return p


def main(argv: Sequence[str] | None = None) -> int:
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
            return _gate(["--selftest"], None, [], frozenset(), {})
        comments = comment_table(args.comment_prefix)
        rules = compile_rules(args.rules or [], comments)
    except ConfigError as exc:
        print(f"{GATE}: CANNOT CHECK (exit 2) -- {exc}", file=sys.stderr)
        return 2
    env_root = os.environ.get("CI_KIT_ROOT")  # same order as the kit's other gates
    root = args.root or (pathlib.Path(env_root) if env_root else None)
    return _gate([], root.resolve() if root else None, rules, frozenset(args.exempt_suffix), comments)


if __name__ == "__main__":
    sys.exit(main())
