"""Is this diff a dependency version bump and nothing else? Pure; the review tier's second skip.

WHY (2026-11-10). A Renovate PR is `pyproject.toml` +1/-1 plus `uv.lock`, or a workflow's
`uses: owner/action@v1.22.2` -> `@v1.22.3`. The model review reads a version string: there is no
structure to judge, and it cannot see the new release's code either. On 11-09 trade-data opened
~12 of them; each held the ONE sealed review runner for ~10-13 min and queued real PRs behind it.
In trade-data `pyproject.toml` is a guard path, so docs_only's lockfile skip never reached them.

THE RULE, rejecting the bad shape rather than defining the good one: every changed line of every
non-lockfile file pairs with exactly one line on the other side, the pair is IDENTICAL once each
version token is masked, and every version that changed moved strictly UP in its numeric core with its suffix unchanged
(`2.0.0` -> `2.0.0rc1`, `1.0.0+local`, `-slim` -> `-alpine` are not bumps) and never sat after
`!=`, `<` or `<=` (re-admitting an excluded version or lifting a cap is a policy change). A token
counts only where a dependency spec puts one:
  * in a package spec (SPEC_NAMES, requirements/constraints) right after a version operator
    (`==`, `>=`, `<=`, `~=`, `!=`, `^`, `~`, `<`, `>`), or a git source's `tag = "v1.2.3"` /
    `rev = "<sha>"` on a line that names `git = "..."` -- never after a bare `=` or `: `, so
    `fail_under = 85.0` -> `10.0` stays reviewed; never `@` (PEP 508 puts a URL there);
  * on a REF line only, after `@` or an image-tag colon glued to a name (`python:3.12.7`). A ref
    line is the WHOLE line `uses|image|container: <one value> [# comment]` (lower-case keys) or,
    in a Dockerfile, `FROM [--platform=x] <one value> [AS|as name]` (upper-case FROM): a `run:` line
    that merely mentions `docker://`, an `IMAGE:` env key, a Python `from` import never is
    (reviewer 11-11 #1). Elsewhere `u@10.0.0.5` is an address and `if: n >=1.5` a threshold. A
    token followed by `/` or `:` is a host, never a ref;
  * a full-length commit SHA or image digest, masked APART from versions: SHA -> tag is not a bump;
  * after `#` that follows whitespace, a trailing comment (`# v4.2.0`): inert;
  * package.json's exact semver pin, a whole `"name": "1.2.3"` line.
Only dependency MANIFESTS qualify (MANIFEST_NAMES / workflow files; go.mod and Cargo.toml plain
`name v1.2.3` / `x = "1.2.3"` lines never mask, so they are reviewed). Lockfile content is trusted
exactly as docs_only trusts it. The diff is split on LF ONLY, as git writes it: pip and YAML also
break on CR/FF, so `ruff==0.8.0<CR>evil==1` is two requirements to pip -- kept as ONE line here,
its injected text breaks the mask and it is refused (tests pin it). A new, deleted, binary or mode-changed file, an
unpaired line, an unknown hunk line, an empty diff, or a file the diff does not show is NOT a bump.
The caller still forces a review with its label.

THIS IS THE SECOND LOCK, NOT THE FIRST. A diff alone cannot prove a new commit SHA or digest is the
release it claims, nor that a lockfile riding along holds only that bump (reviewer 11-11 #2, #5).
ci_kit.review.tier calls this only for a same-repo PR OPENED BY Renovate whose every non-merge
commit Renovate authored and every merge is a clean, unedited one (tier.bot_authored).
"""
from __future__ import annotations

import re

LOCKFILES = frozenset({"uv.lock", "poetry.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
                       "Cargo.lock"})
SPEC_NAMES = frozenset({"pyproject.toml", "package.json", "Cargo.toml", "go.mod"})
MANIFEST_NAMES = SPEC_NAMES | {"action.yml", "action.yaml"}
_DOCKERFILE = re.compile(r"^Dockerfile([.-][A-Za-z0-9-]+)?$|^[\w-]+\.Dockerfile$")
_MANIFEST_RE = re.compile(r"^(requirements|constraints)[\w.-]*\.(txt|in)$|^(docker-)?compose[\w.-]*\.ya?ml$")
# atomic: with backtracking `10.0.0.5/` would match `10.0.0` and leave `.5/` to compare equal
_VERSION = r"(?>v?[0-9]+(?:\.[0-9]+)+[-+.0-9A-Za-z]*)(?![/:])"
_SHA = r"|(?P<sp>sha256:|@|(?<![\w-])rev\s*=\s*\")(?P<sha>[0-9a-f]{40}|[0-9a-f]{64})\b(?![/:])"
_COMMENT = r"(?<=\s)#\s*"
_SPEC_TOKEN = re.compile(rf"(?P<vp>==|>=|<=|~=|!=|\^|~|<|>|{_COMMENT})(?P<v>{_VERSION})" + _SHA)
_GIT_TOKEN = re.compile(rf"(?P<vp>(?<![\w-])(?:tag|rev)\s*=\s*\"|{_COMMENT})(?P<v>{_VERSION})" + _SHA)
_REF_TOKEN = re.compile(rf"(?P<vp>@|(?<=[\w./-]):|{_COMMENT})(?P<v>{_VERSION})" + _SHA)
_COMMENT_TOKEN = re.compile(rf"(?P<vp>{_COMMENT})(?P<v>{_VERSION})")
_REF_LINE = re.compile(r"^\s*(-\s*)?(uses|image|container):\s*([\"']?)[^\s\"'#]+\3(\s+#.*)?\s*$")
_FROM_LINE = re.compile(r"^FROM\s+(--platform=\S+\s+)?[^\s#]+(\s+(AS|as)\s+[\w.-]+)?\s*$")
_GIT_LINE = re.compile(r"(?<![\w-])git\s*=\s*\"")
_JSON_PIN = re.compile(r'^(\s*"[@\w./-]+":\s*")([0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.]+)?)("\s*,?\s*)$')
_CAPS = ("!=", "<", "<=")   # a version after these is an exclusion or a ceiling: moving it is policy
_BAD_HEADER = ("new file mode", "deleted file mode", "old mode", "new mode", "Binary files",
               "GIT binary patch", "similarity index", "rename from", "copy from")


def is_manifest(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    if name in MANIFEST_NAMES or _MANIFEST_RE.match(name) or _DOCKERFILE.match(name):
        return True
    return path.startswith(".github/workflows/") and name.endswith((".yml", ".yaml"))


def _pattern(line: str, path: str) -> re.Pattern[str]:
    name = path.rsplit("/", 1)[-1]
    if name in SPEC_NAMES or name.startswith(("requirements", "constraints")):
        return _GIT_TOKEN if _GIT_LINE.search(line) else _SPEC_TOKEN
    ref = _FROM_LINE if _DOCKERFILE.match(name) else _REF_LINE
    return _REF_TOKEN if ref.match(line) else _COMMENT_TOKEN


def tokens(line: str, path: str) -> tuple[str, list[tuple[str, str]]]:
    """(the line with every dependency-position token masked, (operator, version) per masked
    version, in order). A SHA masks to `<SHA>`, a version to `<V>`: SHA -> tag changes the mask."""
    if path.rsplit("/", 1)[-1] == "package.json":
        m = _JSON_PIN.match(line)
        if m:
            return f"{m.group(1)}<V>{m.group(3)}", [("", m.group(2))]
    found: list[tuple[str, str]] = []

    def sub(m: re.Match[str]) -> str:
        if m.groupdict().get("sha"):   # _COMMENT_TOKEN has no sha group (reviewer 11-11 #4)
            return f"{m.group('sp')}<SHA>"
        found.append((m.group("vp"), m.group("v")))
        return f"{m.group('vp')}<V>"
    return _pattern(line, path).sub(sub, line), found


def _split(version: str) -> tuple[tuple[int, ...], str]:
    """(numeric release core, the rest): `v1.22.3-slim` -> ((1, 22, 3), "-slim")."""
    core = re.match(r"v?([0-9]+(?:\.[0-9]+)*)", version)
    if not core:
        return (), version
    return tuple(int(x) for x in core.group(1).split(".")), version[core.end():]


def _moved_up(old: list[tuple[str, str]], new: list[tuple[str, str]]) -> bool:
    """Every changed version rose strictly in its core, kept its suffix, and was no exclusion/cap."""
    for (op, x), (_, y) in zip(old, new, strict=True):
        if x == y:
            continue
        (kx, sx), (ky, sy) = _split(x), _split(y)
        if op in _CAPS or sx != sy or not ky > kx:
            return False
    return True


def _files(diff: str) -> dict[str, list[list[str]]] | None:
    """`git diff -U0` text -> {path: [hunk lines]}; None on anything that is never a bump."""
    files: dict[str, list[list[str]]] = {}
    path, in_header = "", False
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            if len(parts) != 2:
                return None
            path, in_header = parts[1], True
            files[path] = []
        elif not path:
            if line:
                return None
        elif in_header:
            if line.startswith(_BAD_HEADER):
                return None
            if line.startswith("@@"):
                in_header = False
                files[path].append([])
        elif line.startswith("@@"):
            files[path].append([])
        elif line[:1] in ("+", "-"):
            files[path][-1].append(line)
        elif not line.startswith("\\") and line:
            return None
    return files


def bump_only(diff: str, changed: list[str]) -> list[str]:
    """The bumps as `path: old -> new` lines when the diff is version bumps only, else []."""
    files = _files(diff)
    if not files or set(files) != set(changed):
        return []
    bumps: list[str] = []
    for path, hunks in files.items():
        if path.rsplit("/", 1)[-1] in LOCKFILES:
            continue
        if not is_manifest(path) or not hunks:
            return []
        for hunk in hunks:
            old = [h[1:] for h in hunk if h.startswith("-")]
            new = [h[1:] for h in hunk if h.startswith("+")]
            if not old or len(old) != len(new):
                return []
            for a, b in zip(old, new, strict=True):
                (ma, va), (mb, vb) = tokens(a, path), tokens(b, path)
                if a == b or ma != mb or not _moved_up(va, vb):
                    return []
                bumps.append(f"{path}: {a.strip()[:80]} -> {b.strip()[:80]}")
    return bumps
