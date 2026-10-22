#!/usr/bin/env python3
"""secret_argv -- a secret must expand where it is used, never in a host-side argv.

2026-10-20: a host's timer scripts read APP_API_KEY out of the active app
container with `docker exec <c> printenv`, then passed it back in as
`docker exec <c> curl -H "X-API-Key: $K"`. The HOST shell expanded `$K`, so the key value sat
in plain text in every `docker events` exec_create record, in `docker inspect` of the exec, and in
host `ps` -- and one of those event streams was printed into an agent's tool output. Ten
scripts carried the shape. The fix is to let the variable expand INSIDE the container
(`docker exec <c> sh -c 'curl -H "X-API-Key: $APP_API_KEY" ...'`), where it already is.

Three rules, read off logical shell lines (backslash continuations joined) of every tracked
.sh / .bash / .yml / .yaml / Makefile / shebang-sh file; markdown and comment lines are exempt:

  R1 exec-argv  a `docker exec` / `docker run` (and their compose forms) whose argv carries a
                HOST-expanded secret: an auth header (-H "X-API-Key: $..", Authorization, Cookie,
                X-*-Token), --password/--token "$..", -u "user:$..", -e NAME_KEY="$..", or any
                $VAR whose name is secret-shaped (*API_KEY, *TOKEN, *SECRET, *PASSWORD).
                Single-quoted spans are masked: they reach the container unexpanded, which is
                the fix. For -e, pass the NAME and let docker read the value from its own env:
                `PGPASSWORD="$p" docker run -e PGPASSWORD ...`.
  R2 hoist      `$(docker exec ... printenv <SECRET_NAME>)`, or a `$(docker exec ...)` whose
                container command echoes/printfs a secret-named var -- reading a secret onto the
                host at all. Whatever the host then does with it, the value now lives in a host
                variable. Piped straight into a hash (`... | sha256sum`) it is a fingerprint,
                not a hoist, and is quiet, as is a pipe into the curl/wget that sends it; any
                other pipe (`| tr`, `| head`) still lands the
                value. Read inside $( ), `< <( )` and backticks, through sudo / timeout N.
  R3 host-argv  a host-side curl / wget / ssh whose argv carries the same secret shapes as R1.
                Visible in `ps` to every process on the box. Pass it on stdin or a process
                substitution instead: `curl -H @<(printf 'X-API-Key: %s\\n' "$K")`.

Lines are LOGICAL: backslash continuations are joined, and so is a line that ends inside an open
quote (a multi-line `sh -c '...'`), up to 20 physical lines; past that cap the physical lines are
read one by one. Unquoted trailing `# ...` comments are dropped. A process substitution `<(...)`
is its own HOST command wherever it sits: blanked out of the enclosing argv (curl sees a
/dev/fd path), but a curl/wget/ssh or docker exec inside it is scanned in its own right.

KNOWN BLIND (stated, not hidden): Python subprocess argv lists, PowerShell, a docker exec nested
inside an outer host-side single-quoted `sh -c '...'` (R2 still catches the hoist that such a
line needs), credentials inside a connection URL (`psql "postgresql://u:$PW@h/db"`), a docker
compose invocation carrying global flags other than -f/-p/--profile/--env-file, a printenv that
names no variable (`printenv | grep API_KEY`), a process substitution whose `<(` and `)` sit on
different lines, and a heredoc body: it is read as ordinary lines, so an apostrophe there can
open a quote for up to 20 lines and HIDE a real finding inside it, not just join it. A secret
held in an innocuously named variable reaches R1/R3 only through a header/flag shape --
`docker exec -e WMK="$v"` is unseen, because `-e NAME="$v"` for non-secrets (SVC_ACTIVE,
VITE_BUILD_SHA) is common and correct.

Exit 1 = findings, 2 = blind (git ls-files failed or empty), 0 = clean.
`ci-secret-argv-lint [--root DIR]`; --selftest proves each rule fires on a planted file and is
quiet on the container-side fix.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import tempfile


def _default_root() -> pathlib.Path:
    """The tree being checked: CI_KIT_ROOT, else the git toplevel holding the working directory."""
    env = os.environ.get("CI_KIT_ROOT")
    if env:
        return pathlib.Path(env).resolve()
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                       check=False)
    top = r.stdout.strip() if r.returncode == 0 else ""
    return pathlib.Path(top).resolve() if top else pathlib.Path.cwd().resolve()


ROOT = _default_root()
SHELL_SUFFIX = {".sh", ".bash", ".yml", ".yaml"}
SHELL_NAMES = {"Makefile"}

SECRET_NAME = r"[A-Za-z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|PASSWD)"
# `docker run` is the same class: its argv is in host `ps`, and `-e NAME="$v"` puts the value
# there where `-e NAME` (inherit from the client's environment) would not.
COMPOSE_FLAGS = r"(?:\s+(?:-f|--file|-p|--project-name|--profile|--env-file)[=\s]+\S+)*"
EXEC = re.compile(r"\bdocker(?:\s+container|(?:\s+|-)compose" + COMPOSE_FLAGS
                  + r")?\s+(?:exec|run)\b")
HOST_CMD = re.compile(r"(?<![\w-])(?:curl|wget|ssh)\b")
# `$(docker exec ...)` or `read K < <(docker exec ...)` lands the output in a host variable;
# `--env-file <(docker exec ...)` hands it to the docker client on an fd, which is the fix.
HOIST_START = re.compile(r"(?:\$\(|<\s*<\(|`)\s*(?:(?:sudo|timeout\s+\S+)\s+)*(?=docker\b)")
# The pipes that keep the value off the host: straight into a hash (a fingerprint), or into the
# curl / wget that sends it (`printf 'K: %s\n' "$K" | curl -H @-`, whose output is the response).
# Any other pipe (`| tr -d x`, `| head -1`) still hands the value itself to the host.
SINK = re.compile(r"""["'\\\s]*\|\s*(?:sha\d+sum|md5sum|b2sum|cksum|shasum|openssl\s+dgst|curl|wget)\b""")
PRINTENV = re.compile(r"\bprintenv\s+" + SECRET_NAME + r"\b")
ECHOES = re.compile(r"\b(?:echo|printf)\b[^|;&]*\$\{?" + SECRET_NAME + r"\b")
# A `$` the HOST shell will expand. `\$` inside a double-quoted `sh -c "..."` reaches the container
# as a literal `$` and expands there -- that is the fix (a host-update script), not a finding.
# `$$` is compose's escape for a literal `$` (it expands in the container), and bash's PID.
HOST_DOLLAR = r"(?<![\\$])\$(?!\$)"
JOIN_CAP = 20
SECRET_SHAPES = [
    ("auth header",
     re.compile(r"(?:-H|--header)[=\s]*[\"']?\s*(?:X-API-Key|Authorization|Proxy-Authorization|"
                r"Cookie|X-[A-Za-z0-9-]*(?:Token|Key|Secret))\s*:[^\"\n]*?" + HOST_DOLLAR, re.I)),
    ("password/token flag",
     re.compile(r"(?:--password|--token|--api-key|--secret)[=\s]+[\"']?" + HOST_DOLLAR)),
    ("user:password", re.compile(r"(?:\s-u|--user)[=\s]+[\"']?[^\s\"']*:" + HOST_DOLLAR)),
    ("secret env",
     re.compile(r"(?:\s-e\s+|\s--env[=\s]+)[\"']?" + SECRET_NAME + r"[A-Za-z0-9_]*=[\"']?" + HOST_DOLLAR)),
    ("secret-named var", re.compile(HOST_DOLLAR + r"\{?" + SECRET_NAME + r"\b")),
]


def tracked(root: pathlib.Path) -> list[pathlib.Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True,
                         check=True).stdout
    return [root / p for p in out.split("\0") if p]


def is_shell(path: pathlib.Path, head: str) -> bool:
    if path.suffix in SHELL_SUFFIX or path.name in SHELL_NAMES:
        return True
    return path.suffix == "" and head.startswith("#!") and "sh" in head.split("\n", 1)[0]


def quotes(line: str, stack: tuple[str, ...] = ()) -> tuple[list[str], tuple[str, ...]]:
    """Per-character shell quote state, entering with `stack` open, and the stack left open.

    The stack holds ' " $' (ANSI-C) and ( / $( contexts: `"$(a "b")"` is a fresh unquoted context
    inside the double quotes, as bash reads it. Per character: '' = unquoted (top level or inside
    a $( )), else the innermost quote. A backslash escape outside single quotes marks both
    characters '\\', so an escaped quote or paren is never read as syntax.
    """
    st = list(stack)
    qs: list[str] = []
    i, n = 0, len(line)
    while i < n:
        ch, top = line[i], (st[-1] if st else "")
        step, mark = 1, top if top in ("'", '"', "$'") else ""
        if top == "'":
            if ch == "'":
                st.pop()
        elif ch == "\\" and i + 1 < n:
            step, mark = 2, "\\"
        elif top in ('"', "$'"):
            if ch == top[-1]:
                st.pop()
            elif top == '"' and line.startswith("$(", i):
                st.append("$(")
                step, mark = 2, ""
        elif ch == "#" and (i == 0 or line[i - 1] in " \t;&|("):
            qs += ["#"] * (n - i)  # an unquoted comment: its apostrophes open nothing
            break
        elif line.startswith(("$(", "$'"), i):
            st.append(line[i:i + 2])
            step, mark = 2, ("$'" if ch == "$" and line[i + 1] == "'" else "")
        elif ch in "'\"":
            st.append(ch)
            mark = ch
        elif ch == "(":
            st.append("(")
        elif ch == ")" and top in ("(", "$("):
            st.pop()
        qs += [mark] * step
        i += step
    return qs[:n], tuple(st)


def open_quote(stack: tuple[str, ...]) -> bool:
    """A quote or a $( ) still open: the command goes on past this physical line."""
    return any(s != "(" for s in stack)


def comment_at(line: str, qs: list[str]) -> int:
    """Index of an unquoted `# comment` (quotes() marks it), else len(line)."""
    return qs.index("#") if "#" in qs else len(line)


def close_paren(line: str, j: int) -> int:
    """Index of the `)` matching the `(` at line[j] (quote-aware), else len(line)."""
    qs, _ = quotes(line[j:])
    depth = 0
    for k, q in enumerate(qs):
        ch = line[j + k]
        if q == "" and ch == "(":
            depth += 1
        elif q == "" and ch == ")":
            depth -= 1
            if depth == 0:
                return j + k
    return len(line)


def logical_lines(text: str) -> list[tuple[int, str]]:
    """(first physical line number, joined text), comments dropped.

    Joins backslash continuations, and a line left inside an open quote, up to JOIN_CAP physical
    lines; past the cap an unclosed quote is presumed an apostrophe in prose and the physical
    lines are read one by one.
    """
    phys = text.splitlines()
    out: list[tuple[int, str]] = []
    k = 0
    while k < len(phys):
        buf: list[str] = []
        stack: tuple[str, ...] = ()
        j = k
        while j < len(phys):
            line = phys[j]
            cont = line.endswith("\\")
            body = line[:-1] if cont else line
            qs, stack = quotes(body, stack)
            buf.append(body[:comment_at(body, qs)])
            j += 1
            if not (cont or open_quote(stack)):
                break
            if j - k >= JOIN_CAP and open_quote(stack):
                buf = []
                for m in range(k, j):
                    qs, _ = quotes(phys[m])
                    out.append((m + 1, phys[m][:comment_at(phys[m], qs)]))
                break
        if buf:
            out.append((k + 1, " ".join(buf)))
        k = j
    return out


def command_span(line: str, i: int) -> tuple[int, str]:
    """From index i, the rest of THIS command with single-quoted spans blanked.

    Stops at an unquoted ; & | newline or a `)` that closes an enclosing $( ). Returns
    (end index, masked text). Blanking (not deleting) keeps offsets aligned.
    """
    out: list[str] = []
    quote, depth, n = "", 0, len(line)
    while i < n:
        ch = line[i]
        if quote in ("'", "$'"):
            # literal to the shell that reads it: nothing here is expanded on the host
            if quote == "$'" and ch == "\\" and i + 1 < n:
                out.append("  ")
                i += 2
                continue
            out.append("'" if ch == "'" else " ")
            if ch == "'":
                quote = ""
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(line[i:i + 2])
            i += 2
            continue
        if quote == '"':
            if ch == '"':
                quote = ""
            out.append(ch)
            i += 1
            continue
        if ch in "'\"" or line.startswith("$'", i):
            quote = "$'" if ch == "$" else ch
            out.append(quote)
            i += len(quote)
            continue
        if line.startswith(("<(", ">("), i):
            # A PROCESS SUBSTITUTION is its own command: `curl -H @<(printf 'K: %s' "$K")`
            # puts a /dev/fd path in curl's argv and runs printf -- a builtin, so never exec'd
            # and never in `ps`. Blanked out of THIS argv; findings_in scans its body as a host
            # command of its own (a curl in there is in `ps`).
            end = close_paren(line, i + 1)
            out.append(line[i:i + 2] + " " * max(0, end - i - 2) + (")" if end < n else ""))
            i = end + 1
            continue
        if line.startswith("$(", i):
            depth += 1
            out.append("$(")
            i += 2
            continue
        if ch == ")":
            if depth == 0:
                break
            depth -= 1
        elif ch in ";&|`" and depth == 0:
            break
        out.append(ch)
        i += 1
    return i, "".join(out)


def shape_in(masked: str) -> str:
    for name, rx in SECRET_SHAPES:
        if rx.search(masked):
            return name
    return ""


def hoist_in(line: str) -> str:
    """'printenv' / 'echo' when a `$(docker exec ...)` or `< <(docker exec ...)` brings a secret's
    value onto the host; '' when it does not, or when it is piped straight on (a fingerprint)."""
    for m in HOIST_START.finditer(line):
        start = m.end()
        x = EXEC.match(line, start)
        if not x or not x.group(0).endswith("exec"):
            continue
        end, _ = command_span(line, start)
        if SINK.match(line, end):
            continue  # `$(docker exec c printenv K | sha256sum)`: the value never lands in a var
        raw = line[start:end]
        for p in reversed([q.start() for q in re.finditer(r"[<>]\(", raw)]):
            stop = close_paren(raw, p + 1)  # a psub's output goes to its reader, not this var
            raw = raw[:p] + " " * (min(stop + 1, len(raw)) - p) + raw[stop + 1:]
        for name, rx in (("printenv", PRINTENV), ("echo/printf", ECHOES)):
            e = rx.search(raw)
            if e and not SINK.match(raw[e.end():]):
                return name
    return ""


def findings_in(path: pathlib.Path, rel: str) -> list[tuple[str, int, str, str]]:
    if path.suffix == ".md":
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if not is_shell(path, text[:200]):
        return []
    hits: list[tuple[str, int, str, str]] = []
    for n, line in logical_lines(text):
        if not line.strip():
            continue
        hoist = hoist_in(line)
        if hoist:
            hits.append((rel, n, f"R2 hoist ({hoist} of a secret onto the host)", line.strip()))
            continue
        qs, _ = quotes(line)
        psubs = [(i, close_paren(line, i + 1)) for i in range(len(line) - 1)
                 if line[i] in "<>" and line[i + 1] == "(" and qs[i] == qs[i + 1] == ""]
        spans: list[tuple[int, int]] = []
        found = ""
        for m in EXEC.finditer(line):
            end, masked = command_span(line, m.start())
            spans.append((m.start(), end))
            shape = shape_in(masked)
            if shape and not found:
                found = f"R1 exec-argv ({shape} expanded on the host)"
        if not found:
            for m in HOST_CMD.finditer(line):
                if (any(a <= m.start() < b for a, b in spans)
                        and not any(a < m.start() < b for a, b in psubs)):
                    continue
                _, masked = command_span(line, m.start())
                shape = shape_in(masked)
                if shape:
                    found = f"R3 host-argv ({shape} in a {m.group(0)} argv)"
                    break
        if found:
            hits.append((rel, n, found, line.strip()))
    return hits


def scan(root: pathlib.Path) -> tuple[list[tuple[str, int, str, str]], int]:
    files = tracked(root)
    hits: list[tuple[str, int, str, str]] = []
    for p in files:
        if p.is_file():
            hits.extend(findings_in(p, p.relative_to(root).as_posix()))
    return hits, len(files)


def selftest() -> int:
    fails = 0
    hdr = "X-API-" + "Key"  # assembled so this file is not itself a finding
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        (root / "bad.sh").write_text(
            "#!/usr/bin/env bash\n"
            "K=$(docker exec \"$cn\" printenv APP_API_KEY)\n"               # 2 R2
            f"docker exec \"$cn\" curl -sf -X POST -H \"{hdr}: $K\" \\\n"          # 3 R1
            "  \"http://localhost:8080/x\"\n"
            "docker exec svc-server curl -s -H \"Authorization: Bearer ${SVC_TOKEN}\" u\n"  # 5
            f"curl -s -H \"{hdr}: $_key\" http://127.0.0.1:8088/x\n"                # 6 R3
            "docker exec -e PGPASSWORD=$PGPW db psql\n"                            # 7 R1
            "docker run --rm -e PGPASSWORD=\"$3\" img psql\n"                       # 8 R1
            "K=$(docker exec \"$(active.sh)\" printenv APP_API_KEY)\n"   # 9 R2
            "K=$(docker exec c sh -c 'echo \"$APP_API_KEY\"')\n"            # 10 R2
            "docker-compose exec svc curl -H \"Authorization: Bearer $T\" u\n"     # 11 R1
            "docker run --env APP_API_KEY=\"$k\" img\n"                     # 12 R1
            "docker run --env-file <(curl -s -H \"Authorization: Bearer $T\" u) i\n"  # 13 R3
            f"curl -H @<(printf '{hdr}: %s' \"$K\" | cat) -H \"Cookie: s=$C\" u\n"  # 14 R3
            "echo don\\'t\n"                                                       # 15
            f"docker exec c curl -H \"{hdr}: $K\" u\n"                             # 16 R1
            "docker compose -f x.yml exec svc curl -H \"Authorization: Bearer $T\" u\n"  # 17 R1
            "read -r K < <(docker exec c printenv APP_API_KEY)\n"            # 18 R2
            "K=$(docker exec c printenv APP_API_KEY | tr -d x)\n"           # 19 R2
            "K=$(docker exec c sh -c 'printf %s \"$APP_API_KEY\" | head -1')\n"  # 20 R2
            "K=`docker exec c printenv SVC_TOKEN`\n"                         # 21 R2
            "K=$(sudo docker exec c printenv SVC_TOKEN)\n"                   # 22 R2
            "K=$(timeout 5 docker exec c printenv SVC_TOKEN)\n"              # 23 R2
        )
        (root / "good.sh").write_text(
            "#!/usr/bin/env bash\n"
            "# history: K=$(docker exec c printenv APP_API_KEY) was the bug\n"
            f"docker exec \"$cn\" sh -c 'curl -sf -H \"{hdr}: $APP_API_KEY\" \"$1\"' _ \"$u\"\n"
            f"curl -s -H @<(printf '{hdr}: %s\\n' \"$K\") http://127.0.0.1:8088/x\n"
            "b=\"$(curl -s -H @<(printf 'Authorization: Bearer %s\\n' \"$SVC_TOKEN\") \"$SVC/a\")\"\n"
            "docker run --env-file <(docker exec c sh -c 'printf \"T=%s\" \"$SVC_TOKEN\"') i\n"
            "docker exec \"$c\" printenv PORTAL_API_KEY >/dev/null 2>&1 || true\n"
            "f=\"$(docker exec \"$c\" sh -c 'printf \"%s\" \"$APP_API_KEY\" | sha256sum')\"\n"
            "docker exec \"$cn\" curl -s \"http://localhost:8080/healthz?sym=$s\"\n"
            f"docker exec \"$svc\" sh -c \"curl -s -H '{hdr}: \\$APP_API_KEY' u\"\n"
            "PGPASSWORD=\"$3\" docker run --rm -e PGPASSWORD -e \"SVC=$t\" img psql\n"
            "docker exec \"$cn\" sh -c '\n"
            f"  curl -sf -H \"{hdr}: $APP_API_KEY\" \"$1\"\n"
            "' _ \"$u\"\n"
            f"docker exec c curl -s u  # was -H \"{hdr}: $K\"\n"
            f"docker exec c sh -c $'curl -H \"{hdr}: $APP_API_KEY\" u'\n"
            "f=$(docker exec c printenv APP_API_KEY | sha256sum)\n"
        )
        (root / "compose.yml").write_text(
            "    healthcheck:\n"
            "      test: [\"CMD-SHELL\", \"curl -sf -H 'Authorization: Bearer $$TOKEN' u\"]\n"
        )
        (root / "probe.yaml").write_text(
            "probe: >-\n"
            "  sh -c 'P=$(docker exec db printenv DB_PASSWORD) || exit 2;\n"   # 2 R2
            "  docker exec db db-client --password \"$P\"'\n"
        )
        (root / "doc.md").write_text(f"docker exec c curl -H \"{hdr}: $K\"\n")
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        hits, n = scan(root)
        got = {(h[0], h[1], h[2][:2]) for h in hits}
        want = {("bad.sh", 2, "R2"), ("bad.sh", 3, "R1"), ("bad.sh", 5, "R1"),
                ("bad.sh", 6, "R3"), ("bad.sh", 7, "R1"), ("bad.sh", 8, "R1"),
                ("bad.sh", 9, "R2"), ("bad.sh", 10, "R2"), ("bad.sh", 11, "R1"),
                ("bad.sh", 12, "R1"), ("bad.sh", 13, "R3"), ("bad.sh", 14, "R3"),
                ("bad.sh", 16, "R1"), ("bad.sh", 17, "R1"), ("bad.sh", 18, "R2"),
                ("bad.sh", 19, "R2"), ("bad.sh", 20, "R2"), ("bad.sh", 21, "R2"),
                ("bad.sh", 22, "R2"), ("bad.sh", 23, "R2"),
                # lines 2-3 are ONE logical line (the sh -c quote spans them): the hoist is it
                ("probe.yaml", 2, "R2")}
        if got != want:
            print(f"  SELFTEST FAIL: wanted {sorted(want)}, got {sorted(got)}")
            fails += 1
        if n != 5:
            print(f"  SELFTEST FAIL: scanned {n} files, wanted 5")
            fails += 1
    print(f"secret_argv: selftest {'FAILED' if fails else 'OK'}")
    return 1 if fails else 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    if "--selftest" in argv:
        return selftest()
    root = ROOT
    if "--root" in argv:
        root = pathlib.Path(argv[argv.index("--root") + 1]).resolve()
    try:
        hits, n = scan(root)
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"secret_argv: BLIND -- could not list tracked files: {exc}")
        return 2
    if n == 0:
        print("secret_argv: BLIND -- git ls-files returned nothing; it cannot have looked")
        return 2
    if hits:
        print(f"secret_argv: {len(hits)} secret(s) expanded into a host-side argv:")
        for rel, ln, why, txt in hits:
            print(f"  {rel}:{ln}: {why}: {txt[:140]}")
        print("Let it expand where it lives: docker exec <c> sh -c '... \"$VAR\" ...'; "
              "for a host curl pass the header on a process substitution (-H @<(printf ...)).")
        return 1
    print(f"secret_argv: OK (0 host-side secret argv; {n} files scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
