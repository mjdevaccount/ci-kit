#!/usr/bin/env python3
"""ci-doc-lint -- the doc gate that can read a WHOLE docs tree without drowning in noise.

WHY THIS EXISTS. A runbook named three deleted source files and a dead route for over two months
and nothing noticed, because nothing was looking. The obvious widening -- resolve every backticked
path-ish token against `git ls-files` -- was measured over a live corpus first (write the rule,
RUN it, count what it flags, then decide) and it reported 971 broken pointers across 140 of 189
files at roughly 90% false positives:

    /admin/run/snapshot             an HTTP route
    --scan/--write-baseline         a list of flags that happens to contain slashes
    tier1/tier2/tier3               prose
    usecases/auto_paper             a real module named by its short form

A gate that is nine-tenths noise gets muted in a week, and a muted gate is worse than no gate
because its PASS still reads as coverage.

THE RULE THAT WORKS, AND WHY IT IS NOT MINE. Surveying what mature projects actually run turned up
a consistent answer: nobody guesses whether a code span is a path. lychee ships
`include_verbatim = false` and refuses to look inside code spans at all; Vale documents the same
policy. The one peer-reviewed method that DOES parse backticks (Tan, Wagner & Treude, "Detecting
outdated code element references in software repository documentation", EMSE 2024) survives on a
different question. It does not ask "does this resolve today". It asks:

    DID THIS PATH'S COUNT IN THE SOURCE TREE GO FROM A POSITIVE INTEGER TO ZERO?

That reframing is the whole trick. A route, a flag list and a job-scheduler path were never files,
so they never fell from positive to zero, and they go quiet WITHOUT A SINGLE ALLOWLIST ENTRY --
971 findings at ~90% noise became 63 findings that were almost all real.

THE BLIND SPOT, STATED PLAINLY BECAUSE IT IS PERMANENT. A path that was NEVER real -- a typo, an
invented filename, a file that only ever lived in someone's plan -- has no positive-to-zero
transition and is invisible to this rule forever. That is the price of the precision. The other
half of the answer is not a cleverer regex: it is to write load-bearing pointers as real markdown
links, `[x](../x)`, which a link checker can verify by construction. This gate covers the decay of
things that were once true; links cover things that were never true.

THE ONE EXEMPTION, AND WHY IT IS IN THE FILE AND NOT IN A LIST. A document that RECORDS A REMOVAL
is being correct when it names a deleted file: a teardown record, a dated session changelog.
Rather than keep a central allowlist of such files -- which rots on its own, and which nobody
reading the document would ever see -- a document declares itself:

    <!-- doc-rot: historical -- records a past state; deleted paths are the POINT -->

The marker lives in the document it exempts, so anyone reading the doc sees the claim, and
`--audit` prints every marker so the exemption set can never grow silently.

THE CANONICAL COPY. This gate runs AGAINST a caller's checkout: from the reusable workflow
`doc-refs.yml`, the pre-commit hook `doc-refs`, or the `ci-doc-lint` command. So the tree it gates
is not where the script sits: it is `--root`, else `CI_KIT_ROOT`, else the git toplevel holding
the working directory.

NO BUILT-IN EXEMPTIONS. A repo whose docs name paths in some other namespace that happens to look
like a file path (a job scheduler's `u/user/flow`, say) passes `--exempt-prefix u/`; nothing is
exempt by default. Source files are read for pointers at `docs/**.md`; add a second doc tree with
`--code-doc-dir runbooks`.

Usage:
    ci-doc-lint                        check every tracked *.md (exit 1 on a finding)
    ci-doc-lint --root DIR             gate DIR instead of the repo holding the working directory
    ci-doc-lint --exempt-prefix P      never treat a span starting with P as a path (repeatable)
    ci-doc-lint --code-doc-dir D       a doc tree source files may point into (repeatable; default docs)
    ci-doc-lint --audit                list the historical-marker exemptions and exit 0
    ci-doc-lint --baseline             write the shrink-only ratchet file
    ci-doc-lint --selftest             prove each rule against text that should trip it

Exit codes:  0 clean · 1 findings · 2 DID NOT FINISH LOOKING (git unavailable, corpus unreadable).
2 is distinct on purpose: a crashed detector that returns 0 reads as healthy to every caller.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _default_root() -> Path:
    """The tree being gated: CI_KIT_ROOT, else the git toplevel holding the working directory.

    NOT `Path(__file__)`: this copy is installed or downloaded apart from the repo it checks, so
    its own location names the kit (or a site-packages dir), never the caller. A working directory
    outside any repo falls back to itself, where `git ls-files` then fails and the run exits 2.
    """
    env = os.environ.get("CI_KIT_ROOT")
    if env:
        return Path(env).resolve()
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                       check=False)
    top = r.stdout.strip() if r.returncode == 0 else ""
    return Path(top).resolve() if top else Path.cwd().resolve()


ROOT = _default_root()
#: Span prefixes that are never path claims in THIS repo (`--exempt-prefix`). Empty by default.
EXEMPT_PREFIXES: tuple[str, ...] = ()
#: Doc trees a SOURCE file may point into (`--code-doc-dir`). `docs` is the one convention assumed.
CODE_DOC_DIRS: tuple[str, ...] = ("docs",)


def _baseline() -> Path:
    """Read ROOT at call time: `--root` and the selftest both repoint it after import."""
    return ROOT / "docs" / "doc-refs-baseline.json"

# A code span is a run of N backticks closed by a run of N. Assuming N=1 is what left five of six
# gates checking a minority of their pointers while reporting clean.
_SPAN = re.compile(r"(`+)([^`].*?)\1", re.S)
_HISTORICAL = re.compile(r"<!--\s*doc-rot:\s*historical\b", re.I)

# Shape prefilter ONLY. This is not where the precision comes from -- the transition rule is --
# so it stays deliberately crude: anything that cannot be a path at all.
_META = set("<>{}*$|()[]?!,\"'" + chr(92))
_EXT = re.compile(r"\.[A-Za-z0-9]{1,6}$")


def _git(*args: str) -> str:
    """Run git in the repo, or raise. A git failure must reach exit 2, never a clean 0."""
    r = subprocess.run(["git", "-C", str(ROOT), *args],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed rc={r.returncode}: {r.stderr.strip()}")
    return r.stdout


def is_candidate(tok: str) -> bool:
    """Could this span be a repo path at all? Crude on purpose; the transition rule decides."""
    if not tok or len(tok) > 120:
        return False
    if any(c.isspace() for c in tok):
        return False              # a command line or a sentence, not a pointer
    if _META & set(tok):
        return False              # a template, a glob, a query string
    if "://" in tok:
        return False              # a URL
    # NOT LOAD-BEARING, AND SAID SO. The transition rule already mutes every shape below -- a
    # route and a flag list were never tracked files, so they have no positive-to-zero fall.
    # Measured: removing these lines left the live finding count unchanged. They are kept because
    # they cost nothing, they cut work before the set lookups, and they close the one coincidence
    # the transition rule cannot -- a route whose text happens to equal a path some repo really
    # did delete. The selftest asserts them so they cannot rot into a lie.
    if tok.startswith(("/", "-", "~")):
        return False              # an HTTP route, a flag, a home-relative path
    if EXEMPT_PREFIXES and tok.startswith(EXEMPT_PREFIXES):
        return False              # the caller's declared non-path namespace
    return "/" in tok or bool(_EXT.search(tok))


def normalise(tok: str) -> str:
    """Strip the decorations docs put on pointers: `path:line`, `path#anchor`, trailing slash."""
    return tok.strip().split(":")[0].split("#")[0].strip().rstrip("/")


def tracked_now() -> set[str]:
    return {x for x in _git("ls-files").split() if x}


def tracked_ever() -> set[str]:
    """Every path git has ever known in HEAD's ancestry -- the 'positive integer' side of the rule.

    HEAD, NOT `--all`. CI checks out with fetch-depth 0, which fetches every OPEN PR's branch too,
    and `--all` read them: a plan doc on main naming a file that an unmerged PR adds made the path
    "exist" there and "vanish" here, failing every OTHER PR in the repo. A path only an unmerged
    branch ever held never fell from a positive count on THIS line, so it is the never-tracked
    case and stays silent.

    A SHALLOW CLONE MAKES THIS GATE BLIND, AND BLIND WOULD OTHERWISE LOOK CLEAN. `git log HEAD`
    on a depth-1 checkout sees one commit, so nothing ever "existed", so every finding vanishes
    and the gate reports PASS. actions/checkout defaults to fetch-depth: 1 -- this gate would
    have shipped armed in a place it could not fire. So: refuse to run.
    """
    # `.git/shallow` alone misses a shallow WORKTREE, whose `.git` is a file: ask git as well.
    shallow = _git("rev-parse", "--is-shallow-repository").strip() == "true"
    if shallow or (ROOT / ".git" / "shallow").exists():
        raise RuntimeError(
            "shallow clone: `git log HEAD` cannot see history, so no reference can ever be "
            "shown to have existed and this gate would report a false PASS. "
            "Set `fetch-depth: 0` on actions/checkout.")
    out = _git("log", "HEAD", "--name-only", "--pretty=format:")
    return {x for x in out.split() if x}


def deleted_when(path: str) -> str:
    """Provenance for a finding. A bare 'this is gone' invites 'says who?'."""
    try:
        out = _git("log", "HEAD", "--diff-filter=D", "-1", "--date=short",
                   "--format=%h %ad", "--", path).strip()
    except RuntimeError:
        return "unknown"
    return out.splitlines()[0] if out else "unknown"


def moved_to(ref: str, now: set[str]) -> list[str]:
    """Where a deleted path's basename lives today, if anywhere.

    Most real rot is not deletion but RELOCATION: one commit moves a batch of design docs under
    docs/planning/ and no pointer follows. Saying "gone" for those is true and useless; saying
    where it went makes the finding a one-line fix.
    """
    base = ref.rsplit("/", 1)[-1]
    return sorted(f for f in now if f.rsplit("/", 1)[-1] == base)[:3]


# THE POINTER TRAIL RUNS BOTH WAYS, and only one direction was being watched. A code comment
# naming the design doc it came from is the single thread from a migration back to the
# reasoning that produced it, and when the doc moves, that thread is cut silently -- there is no
# import to fail, no test to go red. Measured: one commit moved a batch of design docs and
# updated NO code comment, leaving 17 dead pointers across .py, .sql and .yaml that survived two
# months of green CI, because the gate read only markdown.
_CODE_EXT = (".py", ".sql", ".yaml", ".yml", ".sh", ".ts", ".tsx", ".mjs", ".ps1")


def doc_ref_re(dirs: tuple[str, ...] = ()) -> re.Pattern[str]:
    """`<dir>/...md` for every doc tree in `dirs` (default: CODE_DOC_DIRS)."""
    alts = "|".join(re.escape(d) for d in (dirs or CODE_DOC_DIRS))
    return re.compile(r"\b((?:" + alts + r")/[\w./-]+\.md)\b")


def scan_code(now: set[str], ever: set[str]) -> list[dict]:
    """Doc pointers embedded in SOURCE files, graded by the same deletion-transition rule."""
    out: list[dict] = []
    rx = doc_ref_re()
    for rel in sorted(f for f in now if f.endswith(_CODE_EXT)):
        p = ROOT / rel
        if not p.exists():
            continue
        seen: set[str] = set()
        for ref in rx.findall(p.read_text(encoding="utf-8", errors="replace")):
            if ref in seen or ref in now or (ROOT / ref).exists():
                continue
            if ref not in ever:
                continue
            seen.add(ref)
            out.append({"doc": rel, "ref": ref, "in_code": True})
    return out


# A RULE'S TRIGGER LIST IS THE ONE POINTER THAT ARMS SOMETHING. `.claude/rules/*.md` carry a
# `paths:` block that decides when the rule auto-loads. A dead entry there is not cosmetic: it is a
# guard that cannot fire. Measured: a rule listed `service/adapters/local_box.py`, renamed to
# `localbox.py`, so editing the REAL file stopped loading the rule entirely, silently, with the
# rule still present and looking armed.
#
# These live in YAML frontmatter as bare quoted strings, not markdown code spans, so the span
# parser above cannot see them. That is exactly why this needs its own reader: the recurring
# defect is a mechanism that is not armed where it must fire, and a checker that only looks at
# prose will report the document clean while the trigger is dead.
_TRIGGER_BLOCK = re.compile(r"^paths:\s*$(.*?)^(?:---|\w)", re.M | re.S)
_TRIGGER_ITEM = re.compile(r"^\s*-\s*[\"']?([^\"'\s]+)[\"']?\s*$", re.M)


def scan_rule_triggers(now: set[str]) -> list[dict]:
    """Auto-load triggers in .claude/rules/*.md that point at nothing.

    Graded against HEAD, not by the deletion-transition rule. A trigger is a live claim about the
    tree RIGHT NOW -- unlike a doc pointer, it has no historical reading in which naming a
    departed file is correct. A glob entry is checked down to its last literal directory.
    """
    out: list[dict] = []
    for rel in sorted(f for f in now if f.startswith(".claude/") and f.endswith(".md")):
        p = ROOT / rel
        if not p.exists():
            continue
        block = _TRIGGER_BLOCK.search(p.read_text(encoding="utf-8", errors="replace"))
        if not block:
            continue
        for item in _TRIGGER_ITEM.findall(block.group(1)):
            if any(ch in item for ch in "*?["):
                stem = item.split("*")[0].rstrip("/")
                if stem and not (ROOT / stem).exists():
                    out.append({"doc": rel, "ref": item, "trigger": True})
                continue
            if item not in now and not (ROOT / item).exists():
                out.append({"doc": rel, "ref": item, "trigger": True})
    return out


def pointer_candidates(raw: str) -> list[str]:
    """The path claims inside one code span.

    A PATH WRITTEN WITH ITS ARGUMENTS IS STILL A PATH CLAIM. `is_candidate` vetoes any token
    containing whitespace -- correct for `docker compose up -d`, wrong for
    `scripts/migrate.py --dry-run`, which names a file and asserts it exists. That is the most
    natural way an operator writes a tool reference: as the command you would run.

    WIDEN BY THE FIRST WORD, NOT THE WHOLE SPAN. The rest is flags, arguments and shell
    punctuation; feeding those to the resolver is how a gate starts reporting `-d` and `--rc`.

    WHAT THIS STILL DOES NOT COVER. When an INTERPRETER leads the command -- `uv run python
    scripts/x.py` -- the path is word two or later and is still unchecked. This is a widening,
    not a closed class.

    NO FILE-EXTENSION REQUIREMENT HERE, DELIBERATELY. This gate asks whether a path's count fell
    from a positive integer to ZERO, and prose was never a tracked file, so `dI/dt` and `bid/ask`
    have no transition and go quiet BY CONSTRUCTION. Requiring an extension would buy nothing and
    would lose every extensionless deleted path written in command position.
    """
    tok = raw.strip()
    if not any(c.isspace() for c in tok):
        return [tok]
    return [tok.split()[0]]


def scan(now: set[str], ever: set[str]) -> tuple[list[dict], list[str]]:
    """Return (findings, exempt_files). One pass over every tracked markdown file."""
    findings: list[dict] = []
    exempt: list[str] = []
    for rel in sorted(f for f in now if f.endswith(".md")):
        p = ROOT / rel
        if not p.exists():
            continue              # tracked but absent in the worktree; not this gate's business
        text = p.read_text(encoding="utf-8", errors="replace")
        if _HISTORICAL.search(text):
            exempt.append(rel)
            continue
        seen: set[str] = set()
        for _, raw in _SPAN.findall(text):
            for cand in pointer_candidates(raw):
                tok = normalise(cand)
                if tok in seen or not is_candidate(tok):
                    continue
                if tok in now or (ROOT / tok).exists():
                    continue
                if tok not in ever:
                    continue      # never tracked -> never a path claim -> silent, no allowlist
                seen.add(tok)
                findings.append({"doc": rel, "ref": tok})
    return findings, exempt


def load_baseline() -> set[str]:
    if not _baseline().exists():
        return set()
    data = json.loads(_baseline().read_text(encoding="utf-8"))
    return {f"{r['doc']}::{r['ref']}" for r in data.get("accepted", [])}


def _selftest_scan_widening() -> list[str]:
    """The widening driven through the REAL scan(), over a real file on disk.

    Every other case exercises pointer_candidates in ISOLATION, so reverting scan()'s loop to the
    old single-token form left them all green -- a unit proven while its only caller ignored it.
    """
    fails: list[str] = []
    _saved_root = globals()["ROOT"]
    _tmp = Path(tempfile.mkdtemp(prefix="ci-doc-lint-selftest-"))
    try:
        (_tmp / "d.md").write_text(
            "run `scripts/gone.py --dry-run` nightly" + chr(10)
            + "growth is `dI/dt = c*S*I` and the spread is `bid/ask spread`" + chr(10)
            + "still shipping `scripts/kept.py --once` daily" + chr(10),
            encoding="utf-8")
        globals()["ROOT"] = _tmp
        found, _exempt = scan({"d.md", "scripts/kept.py"},
                              {"d.md", "scripts/kept.py", "scripts/gone.py"})
        refs = [x["ref"] for x in found]
        if "scripts/gone.py" not in refs:
            fails.append("selftest: scan() did not apply the command-span widening -- the "
                         "helper is proven and its only caller ignores it")
        # THE TRANSITION RULE, PINNED THROUGH scan() RATHER THAN IN THE ABSTRACT. Deleting scan()'s
        # `tok not in ever` once left the suite green; if it stops firing, `dI/dt` is reported.
        for prose in ("dI/dt", "bid/ask"):
            if prose in refs:
                fails.append(f"selftest: scan() reported prose as a broken pointer ({prose}) -- "
                             f"the transition rule is no longer muting")
        if "scripts/kept.py" in refs:
            fails.append("selftest: scan() reported a path that still exists")
    except Exception as exc:
        fails.append(f"selftest: the scan() widening case could not run: {exc}")
    finally:
        globals()["ROOT"] = _saved_root
        shutil.rmtree(_tmp, ignore_errors=True)
    if globals()["ROOT"] != _saved_root:
        fails.append("selftest: ROOT was left patched at a deleted temp directory -- the"
                     " next in-process caller would index an empty tree")
    return fails


def _selftest_parameters() -> list[str]:
    """The two caller-set knobs, both directions: an exemption mutes ONLY its prefix, and a
    second doc tree is read ONLY when named."""
    fails: list[str] = []
    saved = globals()["EXEMPT_PREFIXES"]
    try:
        globals()["EXEMPT_PREFIXES"] = ()
        if not is_candidate("u/admin/flow"):
            fails.append("selftest: with no exemption configured, `u/admin/flow` must be a candidate")
        globals()["EXEMPT_PREFIXES"] = ("u/", "f/")
        for tok in ("u/admin/flow", "f/x/y"):
            if is_candidate(tok):
                fails.append(f"selftest: --exempt-prefix did not mute {tok}")
        if not is_candidate("service/x.py"):
            fails.append("selftest: an exemption muted a path outside its prefix")
    finally:
        globals()["EXEMPT_PREFIXES"] = saved
    text = "see docs/a.md and runbooks/b.md"
    if doc_ref_re(("docs",)).findall(text) != ["docs/a.md"]:
        fails.append("selftest: the default doc tree must be `docs` alone")
    if sorted(doc_ref_re(("docs", "runbooks")).findall(text)) != ["docs/a.md", "runbooks/b.md"]:
        fails.append("selftest: --code-doc-dir did not add the second doc tree")
    return fails


def selftest() -> list[str]:
    """Every rule is asserted against text that SHOULD trip it, and text that should not.

    A test that has never been run against the broken code is a hypothesis, so each case below
    names the exact mutation it dies to.
    """
    fails: list[str] = []

    # is_candidate -- mutation: drop any single disqualifier and one of these starts flagging.
    for bad, why in [
        ("/admin/run/snapshot", "HTTP route"),
        ("--scan/--write-baseline", "flag list"),
        ("migrations/NNN_<slug>.sql", "template"),
        ("https://example.com/x.py", "URL"),
        ("ticker, sentiment, strike", "prose with spaces"),
        ("scripts/a.py --flag", "a command line reaching is_candidate directly"),
    ]:
        if is_candidate(bad):
            fails.append(f"selftest: is_candidate accepted {why}: {bad}")
    for good in ("service/usecases/x.py", "scripts/migrate.py", "docs/A.md"):
        if not is_candidate(good):
            fails.append(f"selftest: is_candidate rejected a real path shape: {good}")

    # THE TRANSITION RULE ITSELF -- the one that carries the precision. Mutation: change
    # `tok not in ever: continue` to `pass` and the never-tracked case below starts failing.
    now, ever = {"a.md", "kept.py"}, {"a.md", "kept.py", "gone.py"}
    for tok, want in [("gone.py", True), ("tier1/tier2/tier3", False), ("kept.py", False)]:
        got = tok not in now and tok in ever
        if got is not want:
            fails.append(f"selftest: transition rule wrong for {tok}: got {got}, want {want}")

    # The historical marker. Mutation: delete the _HISTORICAL check in scan() and this fails.
    if not _HISTORICAL.search("<!-- doc-rot: historical -- records a removal -->"):
        fails.append("selftest: historical marker not recognised")
    if _HISTORICAL.search("this doc is historical"):
        fails.append("selftest: historical marker matched bare prose")

    # normalise -- mutation: drop the split(':') and `path.py:42` stops resolving.
    for raw, want in [("a/b.py:42", "a/b.py"), ("a/b.md#sec", "a/b.md"), ("a/b/", "a/b")]:
        if normalise(raw) != want:
            fails.append(f"selftest: normalise({raw!r}) = {normalise(raw)!r}, want {want!r}")

    # A span parser that assumes N=1 backtick. Mutation: change _SPAN to a single-backtick
    # pattern and the second pointer below goes unseen -- the 93%-blind bug, pinned.
    spans = [m[1] for m in _SPAN.findall("``a`b`` and `real/path.py` here")]
    if "real/path.py" not in spans:
        fails.append("selftest: span parser lost a pointer after a double-backtick span")

    # A PATH WRITTEN WITH ITS ARGUMENTS. Mutation: return `[tok]` unconditionally from
    # pointer_candidates and the first case stops being caught.
    if pointer_candidates("scripts/migrate.py --dry-run") != ["scripts/migrate.py"]:
        fails.append("selftest: a path written with its arguments was not extracted")
    # ...and only the FIRST word. Mutation: split and return every word, and `-d` becomes a
    # candidate; return the last word and a flag list does.
    if pointer_candidates("scripts/migrate.py service/gone.py") != ["scripts/migrate.py"]:
        fails.append("selftest: a later word in a command span was extracted -- widen by the "
                     "FIRST word only, or the flags become findings")
    if pointer_candidates("docs/A.md") != ["docs/A.md"]:
        fails.append("selftest: an ordinary pointer changed shape under the widening")
    # END TO END, both directions, through the same transition rule the rest of the gate uses.
    now2 = {"docs/A.md", "scripts/kept.py"}
    ever2 = {"docs/A.md", "scripts/kept.py", "scripts/gone.py", "service/legacy"}
    for span, want, why in [
        ("scripts/gone.py --flag", True, "a DELETED path written as a command"),
        ("service/legacy --flag", True, "a deleted EXTENSIONLESS path in command position"),
        ("scripts/kept.py --flag", False, "a path that still exists"),
        ("docker compose up -d", False, "a shell command"),
        ("dI/dt = c*S*I", False, "prose the transition rule mutes without an allowlist"),
        ("bid/ask spread", False, "A/B shorthand, likewise"),
    ]:
        tok2 = normalise(pointer_candidates(span)[0])
        got = bool(is_candidate(tok2) and tok2 not in now2 and tok2 in ever2)
        if got is not want:
            fails.append(f"selftest: command-span widening wrong for {why}: {span!r} -> "
                         f"{got}, want {want}")

    fails += _selftest_scan_widening()
    fails += _selftest_parameters()
    return fails


def _check(args: argparse.Namespace) -> int:
    try:
        now, ever = tracked_now(), tracked_ever()
        findings, exempt = scan(now, ever)
        findings += scan_code(now, ever)
        findings += scan_rule_triggers(now)
    except (RuntimeError, OSError) as exc:
        # EXIT 2 = DID NOT FINISH LOOKING. Distinct from 1 = looked and found things.
        print(f"lint-doc-refs: could not complete the scan: {exc}", file=sys.stderr)
        return 2

    if args.audit:
        print(f"historical-marker exemptions: {len(exempt)}")
        for rel in exempt:
            print(f"  {rel}")
        return 0

    if args.baseline:
        rows = [dict(r, deleted=deleted_when(r["ref"])) for r in findings]
        # SHRINK-ONLY, ENFORCED, NOT REQUESTED. A ratchet whose only defence is a note in the
        # file is not a ratchet -- the next agent that hits a red gate runs --baseline and the
        # debt is laundered into "accepted". Growing it requires deleting the file on purpose,
        # which is a reviewable act in a diff rather than an invisible one.
        prior = len(load_baseline())
        if prior and len(rows) > prior:
            print(f"lint-doc-refs: REFUSING to grow the baseline {prior} -> {len(rows)}. "
                  f"Fix the reference, repoint it, or retire the document. To reset "
                  f"deliberately, delete {_baseline().name} first.", file=sys.stderr)
            return 1
        _baseline().write_text(
            json.dumps({"accepted": rows, "note": "shrink-only: --baseline may never grow this "
                                                  "file; fix or retire the doc instead"},
                       indent=2, sort_keys=True) + chr(10), encoding="utf-8", newline=chr(10))
        print(f"baseline written: {len(rows)} accepted finding(s) -> {_baseline().name}")
        return 0

    accepted = load_baseline()
    fresh = [r for r in findings if f"{r['doc']}::{r['ref']}" not in accepted]
    for r in fresh:
        where = moved_to(r["ref"], now)
        hint = f" -> now at {', '.join(where)}" if where else ""
        if r.get("trigger"):
            # Said differently on purpose: this is not a stale mention, it is a rule that cannot
            # load for the file it names, so the reader never learns the rule exists.
            print(f"{r['doc']}: auto-load trigger `{r['ref']}` matches NOTHING "
                  f"— the rule cannot fire for it{hint}")
            continue
        print(f"{r['doc']}: `{r['ref']}` existed and is gone "
              f"({deleted_when(r['ref'])}){hint}")
    carried = len(findings) - len(fresh)
    print(f"lint-doc-refs: {len(fresh)} new, {carried} baselined, "
          f"{len(exempt)} historical · {sum(1 for f in now if f.endswith('.md'))} docs scanned")
    return 1 if fresh else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ci-doc-lint", description=__doc__.split(chr(10))[0])
    ap.add_argument("--audit", action="store_true", help="list historical-marker exemptions")
    ap.add_argument("--baseline", action="store_true", help="write the shrink-only ratchet")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--root", help="the repo to gate (default: CI_KIT_ROOT, else the git "
                                   "toplevel holding the working directory)")
    ap.add_argument("--exempt-prefix", action="append", default=[], metavar="PREFIX",
                    help="a span starting with PREFIX is never a path claim (repeatable; none by default)")
    ap.add_argument("--code-doc-dir", action="append", default=[], metavar="DIR",
                    help="a doc tree source files may point into, as DIR/**.md (repeatable; default: docs)")
    args = ap.parse_args(argv)
    if args.root:
        globals()["ROOT"] = Path(args.root).resolve()
    globals()["EXEMPT_PREFIXES"] = tuple(args.exempt_prefix)
    if args.code_doc_dir:
        globals()["CODE_DOC_DIRS"] = tuple(args.code_doc_dir)

    if args.selftest:
        fails = selftest()
        for f in fails:
            print(f)
        print(f"selftest: {'FAIL' if fails else 'ok'}")
        return 1 if fails else 0
    return _check(args)


if __name__ == "__main__":
    sys.exit(main())
