"""ci-quarantine: every `@pytest.mark.quarantine` carries a reason, an issue, and an unexpired until.

MOVED FROM trade-data (v1.49.0, `scripts/lint_quarantine.py`; W3-5). Its nightly integration suite
was red on 14 of 22 runs, one streak 29 nights long, and a suite that is always red carries no
signal. Quarantine lets a known-bad test leave the GATING run while a non-gating job keeps running
and reporting it; this gate keeps quarantine from being a quiet delete. A mark needs:

    reason="..."          what is wrong, in words (non-empty)
    issue="td#123"        where the fix is tracked: `#N`, `prefix#N`, `owner/repo#N` or an issues URL
    until="YYYY-MM-DD"    past it the mark FAILS; at most `--max-days` (60) ahead of today

An expired mark fails the PR that happens to run after it, not the one that added it: the fix is
to extend it or fix the test, on purpose, with the issue in hand.

WHAT IT CANNOT SEE. It reads the AST of tracked `.py` under `--dir`. A mark built at runtime
(`pytest.mark.quarantine(**kw)`, a hook) has no literal to check, so `**kwargs`, a non-literal value
or a positional argument is a FINDING: an unreadable mark is not a valid one. The mark count is
printed on every run, so an empty quarantine is stated, never implied.

  ci-quarantine [--dir tests] [--mark quarantine] [--max-days 60] [--tz UTC] [--today YYYY-MM-DD]
                [--summary PATH] [--root DIR] [--selftest]
  --summary appends a markdown table of every mark to PATH (a nightly's $GITHUB_STEP_SUMMARY).

Exit 0 clean, 1 findings, 2 could not look (no tracked test file, one that will not parse).
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import pathlib
import re
import sys
import zoneinfo
from collections.abc import Sequence

from ci_kit import astread, discover, site_lint

GATE = "quarantine"
ISSUE_RE = re.compile(r"^(?:[A-Za-z][\w.-]*#\d+|#\d+|[\w.-]+/[\w.-]+#\d+|https://github\.com/[\w.-]+/[\w.-]+/issues/\d+)$")
REQUIRED = ("reason", "issue", "until")


@dataclasses.dataclass(frozen=True)
class Mark:
    origin: str
    line: int
    target: str
    kw: dict[str, str]


def _is_mark(func: ast.expr, name: str) -> bool:
    """`pytest.mark.<name>` or `mark.<name>` (`from pytest import mark`)."""
    if not (isinstance(func, ast.Attribute) and func.attr == name):
        return False
    v = func.value
    return (isinstance(v, ast.Attribute) and v.attr == "mark") or (isinstance(v, ast.Name) and v.id == "mark")


def _target(tree: ast.Module, call: ast.Call) -> str:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and any(
                call in ast.walk(d) for d in node.decorator_list):
            return node.name
    return "<module>"


def marks_in(tree: ast.Module, origin: str, name: str = "quarantine") -> tuple[list[Mark], list[str]]:
    """(marks, findings) for one parsed file. A bare `@pytest.mark.<name>` (no call) is a finding."""
    marks: list[Mark] = []
    findings: list[str] = []
    calls = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _is_mark(node, name) and id(node) not in calls:
            findings.append(f"{origin}:{node.lineno}: bare {name} mark - needs reason=, issue= and until=")
        if not (isinstance(node, ast.Call) and _is_mark(node.func, name)):
            continue
        where = f"{origin}:{node.lineno}"
        if node.args or any(k.arg is None for k in node.keywords):
            findings.append(f"{where}: positional or **kwargs {name} args - use literal reason=, issue=, "
                            f"until= so they can be checked")
        kw: dict[str, str] = {}
        for k in node.keywords:
            if k.arg is None:
                continue
            if isinstance(k.value, ast.Constant) and isinstance(k.value.value, str):
                kw[k.arg] = k.value.value
            else:
                findings.append(f"{where}: {k.arg}= is not a string literal - cannot be checked")
        marks.append(Mark(origin, node.lineno, _target(tree, node), kw))
    return marks, findings


def judge(m: Mark, today: dt.date, max_days: int = 60) -> list[str]:
    where = f"{m.origin}:{m.line} ({m.target})"
    out = [f"{where}: missing {k}=" for k in REQUIRED if not m.kw.get(k, "").strip()]
    issue = m.kw.get("issue", "").strip()
    if issue and not ISSUE_RE.match(issue):
        out.append(f"{where}: issue={issue!r} is not #N, prefix#N, owner/repo#N or an issues URL")
    until = m.kw.get("until", "").strip()
    if until:
        try:
            end = dt.date.fromisoformat(until)
        except ValueError:
            return [*out, f"{where}: until={until!r} is not YYYY-MM-DD"]
        if end < today:
            out.append(f"{where}: EXPIRED - until={until} is before {today}; fix the test, or extend the "
                       f"date on purpose with {issue or 'its issue'} in hand")
        elif (end - today).days > max_days:
            out.append(f"{where}: until={until} is more than {max_days} days out - that is a delete, "
                       f"not a quarantine")
    return out


def summary_md(marks: Sequence[Mark], today: dt.date, name: str = "quarantine") -> str:
    lines = [f"### Quarantine: {len(marks)} mark(s) (as of {today})", ""]
    if not marks:
        return "\n".join([*lines, f"Nothing carries @pytest.mark.{name}: the gating run runs every test.", ""])
    lines += ["| test | issue | until | reason |", "|---|---|---|---|"]
    for m in marks:
        reason = m.kw.get("reason", "").replace("|", "/")
        lines.append(f"| `{m.origin}::{m.target}` | {m.kw.get('issue', '?')} | {m.kw.get('until', '?')} | {reason} |")
    return "\n".join([*lines, ""])


def _verdict(src: str, today: dt.date) -> list[str]:
    m, f = marks_in(ast.parse(src), "t.py")
    return f + [x for mk in m for x in judge(mk, today)]


def selftest() -> list[str]:
    today = dt.date(2026, 9, 28)
    dec = "import pytest\n@pytest.mark.quarantine({})\ndef test_a(): pass\n"
    good = dec.format('reason="flaky port", issue="td#1", until="2026-10-10"')
    bad = {
        "expired": dec.format('reason="r", issue="#2", until="2026-09-27"'),
        "no reason": dec.format('issue="#2", until="2026-10-01"'),
        "no issue": dec.format('reason="r", until="2026-10-01"'),
        "bad issue": dec.format('reason="r", issue="soon", until="2026-10-01"'),
        "bad date": dec.format('reason="r", issue="#2", until="next week"'),
        "too far": dec.format('reason="r", issue="#2", until="2027-09-28"'),
        "kwargs": "import pytest\nKW = {}\n@pytest.mark.quarantine(**KW)\ndef test_g(): pass\n",
        "bare": "import pytest\n@pytest.mark.quarantine\ndef test_f(): pass\n",
        "module": 'import pytest\npytestmark = pytest.mark.quarantine(reason="r", issue="#2")\n',
        "from-import": 'from pytest import mark\n@mark.quarantine(reason="r")\ndef test_i(): pass\n',
    }
    fails = [f"the good mark was refused: {got}"] if (got := _verdict(good, today)) else []
    fails += [f"MISSED the {name!r} shape" for name, src in bad.items() if not _verdict(src, today)]
    if "EXPIRED" not in " ".join(_verdict(good, dt.date(2026, 10, 11))):
        fails.append("a mark the day after its until was not EXPIRED")
    if "0 mark(s)" not in summary_md([], today):
        fails.append("an empty quarantine was not stated in the summary")
    return fails


def _zone(name: str) -> dt.tzinfo:
    """The zone, or exit 2: with no tz database (Windows without `tzdata`) 'today' is unknown,
    and a guessed date is how an expired mark passes."""
    if name.upper() == "UTC":
        return dt.UTC
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
        print(f"{GATE}: cannot resolve --tz {name!r} ({exc}); install tzdata or pass --today", file=sys.stderr)
        raise SystemExit(2) from exc


def main(argv: Sequence[str] | None = None) -> int:
    ap = site_lint.gate_parser("ci-quarantine", __doc__)
    ap.add_argument("--mark", default="quarantine")
    ap.add_argument("--max-days", type=int, default=60)
    ap.add_argument("--tz", default="UTC", help="the zone whose calendar date is 'today'")
    ap.add_argument("--today", type=dt.date.fromisoformat, default=None)
    ap.add_argument("--summary", type=pathlib.Path, default=None)
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if a.selftest:
        return site_lint.selftest_exit(GATE, selftest)
    root = site_lint.gate_root(a.root)
    floors = site_lint.dir_floors(a.dir or ["tests"], gate=GATE)
    today = a.today or dt.datetime.now(_zone(a.tz)).date()
    seen = {"files": 0, "marks": 0}

    def scan() -> list[str]:
        files = discover.tracked(floors, root=root)
        discover.require_scanned_each(discover.count_by_dir(files, floors), floors, gate=GATE)
        marks: list[Mark] = []
        findings: list[str] = []
        for rel in files:
            src = astread.read_text(root / rel, gate=GATE, origin=rel)
            if a.mark in src:
                m, f = marks_in(astread.parse(src, rel, gate=GATE), rel, a.mark)
                marks += m
                findings += f
        findings += [x for m in marks for x in judge(m, today, a.max_days)]
        if a.summary:
            with a.summary.open("a", encoding="utf-8") as fh:
                fh.write(summary_md(marks, today, a.mark))
        seen.update(files=len(files), marks=len(marks))
        return findings

    return site_lint.check(
        [], gate=GATE, scan=scan, selftest=selftest,
        header=lambda n: f"{GATE}: {n} finding(s) in {seen['marks']} {a.mark} mark(s):",
        ok=lambda: (f"{GATE}: OK - {seen['files']} test file(s) scanned, {seen['marks']} {a.mark} mark(s), "
                    f"all with reason, issue and an unexpired until (as of {today})"))


if __name__ == "__main__":
    sys.exit(main())
