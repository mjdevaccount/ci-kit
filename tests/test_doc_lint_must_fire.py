"""Prove ci-doc-lint can actually FAIL, by driving each guard against the thing it catches.

A guard nobody has watched fail is indistinguishable from one that cannot fail, and this gate has
four refusals that a `--selftest` on the live repo can never exercise: the live repo is not
shallow, its baseline is already correct, and its documents are already either clean or marked. So
each case below builds a throwaway git repo where the bad condition is REAL.

The shallow-clone case is the one that matters most. It nearly shipped broken: actions/checkout
defaults to fetch-depth 1, `git log HEAD` on a depth-1 checkout sees one commit, so no reference
can ever be shown to have existed, every finding disappears, and the gate reports a confident PASS
while completely blind. That is a guard armed where it cannot fire — the signature
defect — and it is caught here rather than by a green CI run that proved nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import NL, git, run_tool, write

_git = git


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """From INSIDE the gated repo, as a caller runs it: no copy of the tool lives there now."""
    return run_tool("doc_lint", repo, *args)


@pytest.fixture
def repo(new_repo) -> Path:
    """KNOWN-BAD: `sources/doomed.py` existed, was referenced by a doc, and was deleted."""
    r = new_repo()
    (r / "docs").mkdir()
    (r / "sources").mkdir()
    (r / "sources" / "doomed.py").write_text("x = 1" + NL, encoding="utf-8", newline=NL)
    (r / "docs" / "R.md").write_text("# R" + NL * 2 + "See `sources/doomed.py`." + NL,
                                     encoding="utf-8", newline=NL)
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "one")
    _git(r, "rm", "-q", "sources/doomed.py")
    _git(r, "commit", "-qm", "two")
    return r


def test_fires_on_a_real_deletion_transition(repo: Path) -> None:
    """The whole point. Mutate `if tok not in ever: continue` to `pass` and this still passes --
    which is why the never-tracked case below is the other half of the proof."""
    r = _run(repo)
    assert r.returncode == 1, r.stdout
    assert "doomed.py" in r.stdout


def test_a_path_that_never_existed_is_silent(repo: Path) -> None:
    """The precision mechanism, and the reason there is no allowlist.

    A route and a flag list are not paths, were never tracked, and must not be reported. Delete
    the `tok not in ever` line and this test goes red -- it is what separates 63 findings from
    971.
    """
    doc = repo / "docs" / "R.md"
    doc.write_text(doc.read_text(encoding="utf-8")
                   + "Call `/admin/run/x` with `--scan/--write-baseline` on `tier1/tier2`." + NL,
                   encoding="utf-8", newline=NL)
    r = _run(repo)
    assert "/admin/run/x" not in r.stdout
    assert "tier1/tier2" not in r.stdout


def test_a_doc_pointer_inside_source_code_is_caught(repo: Path) -> None:
    """THE POINTER TRAIL RUNS BOTH WAYS, and only one direction was watched.

    A comment saying "why: docs/X.md" is the single thread from a migration back to the reasoning
    that produced it. When the doc moves there is no import to fail and no test to go red, so it
    is cut silently. Measured 2026-08-29: one commit moved a batch of design docs into
    docs/planning/ and updated NO code comment, leaving 17 dead pointers across .py/.sql/.yaml
    that survived two months of green CI because the gate read only markdown.
    """
    _git(repo, "mv", "docs/R.md", "docs/moved.md")
    (repo / "service").mkdir(exist_ok=True)
    (repo / "service" / "thing.py").write_text(
        '"""Why this exists: docs/R.md section 2."""' + NL, encoding="utf-8", newline=NL)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "move the doc, leave the pointer")

    r = _run(repo)
    assert r.returncode == 1, r.stdout
    assert "service/thing.py" in r.stdout, "a doc pointer inside source code went unseen"
    assert "docs/moved.md" in r.stdout, "the gate found it but did not say where the doc went"


def test_a_dead_rule_trigger_is_caught(repo: Path) -> None:
    """A TRIGGER LIST IS THE ONE POINTER THAT ARMS SOMETHING.

    `.claude/rules/*.md` carry a `paths:` block deciding when the rule auto-loads. A dead entry is
    not cosmetic — it is a guard that cannot fire. Measured 2026-08-29: a rule listed
    `service/adapters/local_box.py`, renamed to `localbox.py`, so editing the REAL file
    silently stopped loading the rule while the rule sat there looking armed.

    These live in YAML frontmatter as bare quoted strings, so the code-span parser cannot see
    them. Delete `scan_rule_triggers` and this test goes red; nothing else does.
    """
    rules = repo / ".claude" / "rules"
    rules.mkdir(parents=True)
    (rules / "thing.md").write_text(
        "---" + NL
        + "description: a rule" + NL
        + "paths:" + NL
        + '  - "sources/doomed.py"' + NL
        + '  - "docs/**"' + NL
        + "---" + NL * 2
        + "# Thing" + NL,
        encoding="utf-8", newline=NL)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "a rule whose trigger names a deleted file")

    r = _run(repo)
    assert r.returncode == 1, r.stdout
    assert "cannot fire" in r.stdout, "a dead auto-load trigger went unreported"
    assert "sources/doomed.py" in r.stdout
    assert "docs/**" not in r.stdout, "a live glob trigger must not be reported"


def test_historical_marker_suppresses(repo: Path) -> None:
    """A teardown record naming the file it tore down is the document being CORRECT."""
    doc = repo / "docs" / "R.md"
    doc.write_text("<!-- doc-rot: historical -->" + NL + doc.read_text(encoding="utf-8"),
                   encoding="utf-8", newline=NL)
    r = _run(repo)
    assert r.returncode == 0, r.stdout
    assert _run(repo, "--audit").stdout.count("docs/R.md") == 1


def test_baseline_refuses_to_grow(repo: Path) -> None:
    """A ratchet whose only defence is a note in the file is not a ratchet.

    Without this refusal, the next agent facing a red gate runs --baseline and launders the debt
    into "accepted" with nothing in the diff to notice.
    """
    assert _run(repo, "--baseline").returncode == 0
    (repo / "sources").mkdir(exist_ok=True)
    (repo / "sources" / "second.py").write_text("y = 2" + NL, encoding="utf-8", newline=NL)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "three")
    _git(repo, "rm", "-q", "sources/second.py")
    _git(repo, "commit", "-qm", "four")
    doc = repo / "docs" / "R.md"
    doc.write_text(doc.read_text(encoding="utf-8") + "Also `sources/second.py`." + NL,
                   encoding="utf-8", newline=NL)

    r = _run(repo, "--baseline")
    assert r.returncode == 1, r.stdout
    assert "REFUSING to grow" in r.stderr
    kept = json.loads((repo / "docs" / "doc-refs-baseline.json").read_text(encoding="utf-8"))
    assert len(kept["accepted"]) == 1, "the baseline was rewritten despite the refusal"


def test_shallow_clone_exits_2_and_never_a_clean_0(repo: Path, tmp_path: Path) -> None:
    """BLIND MUST NOT LOOK CLEAN.

    On a depth-1 checkout `git log HEAD` sees one commit, so nothing ever existed and every
    finding silently disappears. Exit 2 means "did not finish looking" and is distinct from 1
    ("looked, found things") precisely because a crashed detector returning 0 reads as healthy to
    every caller.
    """
    shallow = tmp_path / "s"
    subprocess.run(["git", "clone", "-q", "--depth", "1", "file://" + repo.as_posix(),
                    str(shallow)], capture_output=True, check=False)
    if not (shallow / ".git" / "shallow").exists():
        pytest.skip("git did not produce a shallow clone here; the guard would go UNPROVEN")

    r = _run(shallow)
    assert r.returncode == 2, f"shallow clone must exit 2, got {r.returncode}: {r.stdout}"
    assert "shallow" in r.stderr


def test_known_good_exits_0(new_repo) -> None:
    """KNOWN-GOOD: every pointer resolves. Without this, a gate that always exits 1 passes above."""
    r = new_repo()
    write(r / "sources" / "kept.py", "x = 1" + NL)
    write(r / "docs" / "R.md", "# R" + NL * 2 + "See `sources/kept.py`." + NL)
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "one")
    res = _run(r)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "0 new" in res.stdout


def test_a_path_only_an_unmerged_branch_holds_is_silent(new_repo) -> None:
    """A SIBLING BRANCH IS NOT HISTORY. CI fetches every open PR's branch (fetch-depth 0).

    A plan doc on main's plan doc names it as a FUTURE file and
    open PR #60's branch adds it, so `git log --all` saw it "exist" there and "vanish" here. That
    path never fell from a positive count on the line being gated. Revert `HEAD` to `--all` in
    tracked_ever() and this goes red; the deletion test above proves HEAD still sees real falls.
    """
    r = new_repo()
    write(r / "docs" / "PLAN.md", "# P" + NL * 2 + "M1 adds `host/future.sh`." + NL)
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "plan")
    _git(r, "checkout", "-qb", "side")
    write(r / "host" / "future.sh", "true" + NL)
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "the open PR")
    _git(r, "checkout", "-q", "-")
    res = _run(r)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "future.sh" not in res.stdout


def test_blind_outside_any_repo_exits_2(tmp_path: Path) -> None:
    """BLIND: no git repo at all. It must say "did not finish looking", never a clean 0."""
    plain = tmp_path / "plain"
    write(plain / "docs" / "R.md", "See `sources/doomed.py`." + NL)
    res = _run(plain)
    assert res.returncode == 2, res.stdout + res.stderr


def test_root_flag_gates_another_tree(repo: Path, tmp_path: Path) -> None:
    """The canonical copy runs AGAINST a checkout; `--root` must reach it from anywhere."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    res = run_tool("doc_lint", elsewhere, "--root", str(repo))
    assert res.returncode == 1, res.stdout + res.stderr
    assert "doomed.py" in res.stdout


def test_shallow_worktree_is_still_refused(repo: Path, tmp_path: Path) -> None:
    """A shallow repo's WORKTREE has a `.git` FILE, so the old `.git/shallow` probe missed it."""
    shallow = tmp_path / "s"
    subprocess.run(["git", "clone", "-q", "--depth", "1", "file://" + repo.as_posix(),
                    str(shallow)], capture_output=True, check=False)
    wt = tmp_path / "wt"
    _git(shallow, "worktree", "add", "-q", str(wt))
    if not (wt / ".git").is_file():
        pytest.skip("git did not produce a worktree here; the guard would go UNPROVEN")
    res = _run(wt)
    assert res.returncode == 2, f"shallow worktree must exit 2, got {res.returncode}: {res.stdout}"


def test_selftest_passes() -> None:
    res = run_tool("doc_lint", Path.cwd(), "--selftest")
    assert res.returncode == 0, res.stdout


def test_exempt_prefix_is_off_by_default_and_mutes_only_its_namespace(new_repo) -> None:
    """NO ESTATE DEFAULT. A span in some other namespace (`u/...`, a job scheduler's path) that
    happens to equal a path the repo once tracked IS reported unless the caller exempts it."""
    r = new_repo()
    write(r / "u" / "admin" / "flow.py", "x = 1" + NL)
    write(r / "docs" / "R.md", "# R" + NL * 2 + "Run `u/admin/flow.py` nightly." + NL)
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "one")
    _git(r, "rm", "-q", "u/admin/flow.py")
    _git(r, "commit", "-qm", "two")
    assert _run(r).returncode == 1
    assert _run(r, "--exempt-prefix", "u/").returncode == 0
    assert _run(r, "--exempt-prefix", "f/").returncode == 1, "an exemption must mute only its own prefix"


def test_code_doc_dir_adds_a_second_doc_tree_only_when_named(new_repo) -> None:
    r = new_repo()
    write(r / "runbooks" / "ops.md", "# ops" + NL)
    write(r / "svc" / "thing.py", '"""why: runbooks/ops.md"""' + NL)
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "one")
    _git(r, "mv", "runbooks/ops.md", "runbooks/moved.md")
    _git(r, "commit", "-qm", "two")
    assert _run(r).returncode == 0, "runbooks/ is not read unless named"
    res = _run(r, "--code-doc-dir", "docs", "--code-doc-dir", "runbooks")
    assert res.returncode == 1 and "svc/thing.py" in res.stdout, res.stdout


def test_ci_kit_root_env_selects_the_tree(repo: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    res = subprocess.run([sys.executable, "-m", "ci_kit.doc_lint"], cwd=elsewhere, capture_output=True,
                         text=True, check=False, env={**os.environ, "CI_KIT_ROOT": str(repo)})
    assert res.returncode == 1 and "doomed.py" in res.stdout, res.stdout + res.stderr
