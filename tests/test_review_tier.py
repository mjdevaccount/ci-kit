"""The review gate's first step (ci_kit.review.tier): who is reviewed, why, on which credential.

Each rule in BOTH directions: the docs-only skip must refuse code as surely as it skips prose, a
missing credential must be red (UNARMED) as surely as a present one arms the gate, and the CLI
must read the PR's OWN diff with renames off.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ci_kit.review import tier

ARGS = {"guard": False, "guard_files": "", "commits": 1, "changed": ["app/x.py"], "labeled": False,
        "label": "review:required", "head_repo": "o/r", "repo": "o/r", "auth_mode": "",
        "has_oauth": True, "has_key": False, "model": "small", "guard_model": "big"}


def _decide(**kw: object) -> tier.Tier:
    return tier.decide(**{**ARGS, **kw})


@pytest.mark.parametrize("changed", [["README.md"], ["docs/a.md", "uv.lock"], ["web/package-lock.json"]])
def test_prose_and_lockfiles_are_docs_only(changed: list[str]) -> None:
    assert tier.docs_only(changed)


@pytest.mark.parametrize("changed", [
    [],                                   # nothing read is not clean
    ["README.md", "app/x.py"],            # one code file is enough
    [".claude/agents/reviewer.md"],       # an agent charter is behaviour, not prose
    ["prompts/system.md"],                # a runtime LLM prompt is behaviour, not prose
    ["uv.lock.py"],
])
def test_anything_else_is_not_docs_only(changed: list[str]) -> None:
    assert not tier.docs_only(changed)


def test_not_prose_prefixes_are_the_callers() -> None:
    assert tier.docs_only(["prompts/a.md"], not_prose=("agents/",))
    assert not tier.docs_only(["agents/a.md"], not_prose=("agents/",))


def test_every_code_pr_is_reviewed_on_the_default_model() -> None:
    t = _decide()
    assert t.required and t.reasons == "every code PR: structure review" and t.model == "small"


def test_a_docs_only_diff_skips_the_model_call() -> None:
    t = _decide(changed=["README.md"], commits=5)
    assert not t.required
    assert any("docs/lockfile-only" in n for n in t.notes)


@pytest.mark.parametrize(("kw", "reason"), [
    ({"guard": True, "guard_files": "scripts/lint_x.py"}, "guard path (scripts/lint_x.py)"),
    ({"labeled": True}, "label review:required"),
])
def test_a_guard_path_or_the_label_still_reviews_a_docs_only_diff(kw: dict, reason: str) -> None:
    t = _decide(changed=["README.md"], **kw)
    assert t.required and reason in t.reasons


def test_a_guard_path_picks_the_guard_model() -> None:
    assert _decide(guard=True, guard_files="a").model == "big"


def test_three_commits_is_a_reason_only_for_code() -> None:
    assert "3 commits" in _decide(commits=3).reasons
    assert "3 commits" not in _decide(commits=2).reasons


def test_guard_names_cannot_forge_an_output_line() -> None:
    newline = chr(10)
    assert newline not in tier.guard_names(f"'a{newline}required=false' b")
    assert tier.guard_names(" ".join(f"f{i}" for i in range(9))) == "f0,f1,f2,f3,f4"


@pytest.mark.parametrize(("mode", "oauth", "key", "cred", "unarmed"), [
    ("subscription", True, True, "oauth", False),
    ("subscription", False, True, "", True),        # the mode's own secret, never a fallback
    ("api", True, True, "api_key", False),
    ("api", True, False, "", True),
    ("", True, True, "oauth", False),               # unset = auto, subscription first
    ("auto", False, True, "api_key", False),
    ("auto", False, False, "", True),
    ("bogus", True, True, "", True),                # not a mode: UNARMED, never a silent default
])
def test_the_credential_toggle(mode: str, oauth: bool, key: bool, cred: str, unarmed: bool) -> None:
    t = _decide(auth_mode=mode, has_oauth=oauth, has_key=key)
    assert t.credential == cred and t.unarmed is unarmed
    assert bool(t.blind) is unarmed


def test_the_mode_cannot_forge_an_output_line() -> None:
    assert tier.mode_of("API" + chr(10) + "unarmed=false") == "apiunarmedfalse"
    assert tier.mode_of("") == "auto"


def test_a_fork_head_is_blind_but_not_unarmed() -> None:
    t = _decide(head_repo="someone/fork")
    assert "fork" in t.blind and t.unarmed is False


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _outputs(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


@pytest.fixture
def pr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "x.py").write_text("x = 1" + chr(10))
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    monkeypatch.setenv("BASE_SHA", _git(tmp_path, "rev-parse", "HEAD"))
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("HEAD_REPO", "o/r")
    monkeypatch.setenv("HAS_OAUTH", "true")
    monkeypatch.setattr(tier, "verified", lambda repo, sha: False)   # no API here: unsigned unless a test says
    _git(tmp_path, "checkout", "-qb", "pr")
    return tmp_path


def test_a_code_file_moved_under_docs_is_not_docs_only(pr: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # with rename detection `--name-only` prints only the NEW name: docs/x.md would read as prose
    (pr / "docs").mkdir()
    _git(pr, "mv", "app/x.py", "docs/x.md")
    _git(pr, "commit", "-qm", "move")
    monkeypatch.setenv("HEAD_SHA", _git(pr, "rev-parse", "HEAD"))
    assert tier.main(["--model", "m", "--guard-model", "g"]) == 0
    out = _outputs(pr / "out")
    assert out["required"] == "true" and out["credential"] == "oauth"


def test_a_prose_only_pr_writes_required_false(pr: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (pr / "README.md").write_text("hi" + chr(10))
    _git(pr, "add", ".")
    _git(pr, "commit", "-qm", "docs")
    monkeypatch.setenv("HEAD_SHA", _git(pr, "rev-parse", "HEAD"))
    assert tier.main(["--model", "m", "--guard-model", "g"]) == 0
    assert _outputs(pr / "out") == {"required": "false"}


def test_the_reviewer_line_names_the_credential_never_its_value(pr: Path, monkeypatch: pytest.MonkeyPatch,
                                                                 capsys: pytest.CaptureFixture[str]) -> None:
    (pr / "app" / "x.py").write_text("x = 2" + chr(10))
    _git(pr, "commit", "-qam", "code")
    monkeypatch.setenv("HEAD_SHA", _git(pr, "rev-parse", "HEAD"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "s3cret-value")
    assert tier.main(["--model", "m", "--guard-model", "g"]) == 0
    printed = capsys.readouterr().out
    assert "Reviewer: mode=auto credential=CLAUDE_CODE_OAUTH_TOKEN model=m" in printed
    assert "s3cret-value" not in printed + (pr / "out").read_text() + (pr / "summary").read_text()


def test_an_unreadable_diff_is_exit_2(pr: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEAD_SHA", "0" * 40)
    assert tier.main(["--model", "m", "--guard-model", "g"]) == 2


RENOVATE = tier.BUMP_BOTS["renovate[bot]"]


def _bump_pr(pr: Path, extra: dict[str, bytes], email: str = RENOVATE) -> str:
    (pr / "requirements.txt").write_bytes(b"ruff==0.7.1" + bytes([10]))
    _git(pr, "add", ".")
    _git(pr, "commit", "-qm", "req")
    base = _git(pr, "rev-parse", "HEAD")
    (pr / "requirements.txt").write_bytes(b"ruff==0.8.0" + bytes([10]))
    for name, data in extra.items():
        (pr / name).write_bytes(data)
    _git(pr, "add", ".")
    # as Renovate's API commit lands: the bot authors, GitHub (web-flow) commits
    _git(pr, "-c", f"author.email={email}", "-c", f"committer.email={tier.WEB_FLOW}", "commit", "-qm", "bump")
    return base


def test_a_version_bump_pr_skips_the_model_and_says_so(pr: Path, monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("BASE_SHA", _bump_pr(pr, {}))
    monkeypatch.setenv("HEAD_SHA", _git(pr, "rev-parse", "HEAD"))
    monkeypatch.setenv("GUARD", "true")   # requirements.txt is a guard path in the caller
    monkeypatch.setenv("PR_AUTHOR", "renovate[bot]")
    monkeypatch.setattr(tier, "verified", lambda repo, sha: repo == "o/r")
    assert tier.main(["--model", "m", "--guard-model", "g"]) == 0
    assert _outputs(pr / "out") == {"required": "false"}
    assert "Review tier: skipped (version bump only, 1 lines)" in capsys.readouterr().out
    assert "ruff==0.8.0" in (pr / "summary").read_text()


def test_an_amended_bot_commit_is_reviewed(pr: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # reviewer 11-11 round 4: `commit --amend` (and rebase, cherry-pick) keep the bot as AUTHOR;
    # the committer is the person who edited it
    monkeypatch.setenv("BASE_SHA", _bump_pr(pr, {}))
    (pr / "requirements.txt").write_bytes(b"ruff==0.8.1" + bytes([10]))
    _git(pr, "commit", "-qa", "--amend", "--no-edit")
    assert _git(pr, "log", "-1", "--format=%ae %ce") == f"{RENOVATE} t@t"
    monkeypatch.setenv("HEAD_SHA", _git(pr, "rev-parse", "HEAD"))
    monkeypatch.setenv("PR_AUTHOR", "renovate[bot]")
    assert tier.main(["--model", "m", "--guard-model", "g"]) == 0
    assert _outputs(pr / "out")["required"] == "true"


@pytest.mark.parametrize(("author", "email", "head_repo", "hand_commit"), [
    ("mattj", RENOVATE, "o/r", False),                 # a person opened it: same diff, reviewed
    ("renovate[bot]", "t@t", "o/r", False),            # the bot's PR, a commit it did not author
    ("renovate[bot]", RENOVATE, "o/r", True),          # a hand commit pushed onto the bot's branch
    ("renovate[bot]", RENOVATE, "evil/r", False),      # a fork head is never the bot's own branch
    ("", RENOVATE, "o/r", False),                      # no author in the event: reviewed
])
def test_only_the_bump_bots_own_pr_skips(pr: Path, monkeypatch: pytest.MonkeyPatch, author: str, email: str,
                                         head_repo: str, hand_commit: bool) -> None:
    # reviewer 11-11 #2: a diff cannot prove a SHA or a lockfile; the bot's identity is the first lock
    monkeypatch.setenv("BASE_SHA", _bump_pr(pr, {}, email))
    if hand_commit:
        (pr / "requirements.txt").write_bytes(b"ruff==0.8.1" + bytes([10]))
        _git(pr, "commit", "-qam", "hand")
    monkeypatch.setenv("HEAD_SHA", _git(pr, "rev-parse", "HEAD"))
    monkeypatch.setenv("PR_AUTHOR", author)
    monkeypatch.setenv("HEAD_REPO", head_repo)
    assert tier.main(["--model", "m", "--guard-model", "g"]) == 0
    assert _outputs(pr / "out")["required"] == "true"


@pytest.mark.parametrize(("edit", "required"), [
    (None, "false"),                 # update-branch: main merged in clean, untouched -- still skips
    (b"ruff==0.8.1", "true"),        # the same merge carrying an edit (reviewer 11-11 round 3 #1)
])
def test_a_merge_on_the_bots_branch_must_be_an_honest_one(pr: Path, monkeypatch: pytest.MonkeyPatch,
                                                         edit: bytes | None, required: str) -> None:
    base = _bump_pr(pr, {})
    bot_head = _git(pr, "rev-parse", "HEAD")
    _git(pr, "checkout", "-q", "-B", "main", base)          # main moves on with an unrelated commit
    (pr / "notes.md").write_text("n" + chr(10))
    _git(pr, "add", "notes.md")
    _git(pr, "commit", "-qm", "main moves")
    main_tip = _git(pr, "rev-parse", "HEAD")
    _git(pr, "checkout", "-q", "-B", "pr", bot_head)
    _git(pr, "merge", "-q", "--no-ff", "--no-commit", "main")
    if edit:
        (pr / "requirements.txt").write_bytes(edit + bytes([10]))
        _git(pr, "add", "requirements.txt")
    _git(pr, "-c", "user.email=someone@x", "commit", "-qm", "merge main")
    monkeypatch.setenv("BASE_SHA", main_tip)
    monkeypatch.setenv("HEAD_SHA", _git(pr, "rev-parse", "HEAD"))
    monkeypatch.setenv("GUARD", "true")
    monkeypatch.setenv("PR_AUTHOR", "renovate[bot]")
    monkeypatch.setattr(tier, "verified", lambda repo, sha: True)
    assert tier.main(["--model", "m", "--guard-model", "g"]) == 0
    assert _outputs(pr / "out")["required"] == required


@pytest.mark.parametrize(("signed", "author_date"), [
    (False, None),                      # `git filter-branch` keeps both identities and dates, drops the signature
    (True, "2026-01-01T00:00:00Z"),     # GitHub's rebase update-branch re-commits an amend: dates part
])
def test_a_rewritten_bot_commit_is_reviewed(pr: Path, monkeypatch: pytest.MonkeyPatch, signed: bool,
                                            author_date: str | None, capsys: pytest.CaptureFixture[str]) -> None:
    # reviewer 11-11 round 5: identities alone can be carried through a rewrite
    if author_date:
        monkeypatch.setenv("GIT_AUTHOR_DATE", author_date)
    monkeypatch.setenv("BASE_SHA", _bump_pr(pr, {}))
    monkeypatch.setenv("HEAD_SHA", _git(pr, "rev-parse", "HEAD"))
    monkeypatch.setenv("PR_AUTHOR", "renovate[bot]")
    monkeypatch.setattr(tier, "verified", lambda repo, sha: signed)
    assert tier.main(["--model", "m", "--guard-model", "g"]) == 0
    assert _outputs(pr / "out")["required"] == "true"
    assert "bump skip NOT taken for renovate[bot]" in capsys.readouterr().out


def test_a_non_utf8_file_is_reviewed_never_red(pr: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # reviewer 11-10 #2: the -U0 read must not turn a Latin-1 file into exit 2 the label cannot fix
    monkeypatch.setenv("BASE_SHA", _bump_pr(pr, {"x.csv": b"caf" + bytes([0xE9]) + bytes([10])}))
    monkeypatch.setenv("HEAD_SHA", _git(pr, "rev-parse", "HEAD"))
    monkeypatch.setenv("PR_AUTHOR", "renovate[bot]")
    assert tier.main(["--model", "m", "--guard-model", "g"]) == 0
    assert _outputs(pr / "out")["required"] == "true"


LOCK = {"uv.lock": b"ci-kit 1.27.0" + bytes([10])}
ACTIONS = "41898282+github-actions[bot]@users.noreply.github.com"


def _relock(pr: Path, files: dict[str, bytes | None], email: str = ACTIONS, allow_empty: bool = False) -> None:
    """A second commit on the bot's branch, as trade-data's relock.yml makes one: another author."""
    for name, data in files.items():
        if data is None:
            _git(pr, "rm", "-q", name)
        else:
            (pr / name).parent.mkdir(parents=True, exist_ok=True)
            (pr / name).write_bytes(data)
            _git(pr, "add", name)
    extra = ["--allow-empty"] if allow_empty else []
    _git(pr, "-c", f"user.email={email}", "commit", "-qm", "chore(lock): relock uv.lock", *extra)


def _run_relock(pr: Path, monkeypatch: pytest.MonkeyPatch, before: dict[str, bytes],
                relock: dict[str, bytes | None], allow_empty: bool = False,
                guard_files: str = "requirements.txt") -> str:
    for name, data in before.items():   # already on main, below the PR's base
        (pr / name).parent.mkdir(parents=True, exist_ok=True)
        (pr / name).write_bytes(data)
        _git(pr, "add", name)
        _git(pr, "commit", "-qm", "lock on main")
    monkeypatch.setenv("BASE_SHA", _bump_pr(pr, {}))
    _relock(pr, relock, allow_empty=allow_empty)
    monkeypatch.setenv("HEAD_SHA", _git(pr, "rev-parse", "HEAD"))
    monkeypatch.setenv("GUARD", "true")
    monkeypatch.setenv("GUARD_FILES", guard_files)
    monkeypatch.setenv("PR_AUTHOR", "renovate[bot]")
    monkeypatch.setattr(tier, "verified", lambda repo, sha: _git(pr, "log", "-1", "--format=%ae", sha) == RENOVATE)
    assert tier.main(["--model", "m", "--guard-model", "g"]) == 0
    return _outputs(pr / "out")["required"]


def test_a_foreign_lockfile_only_commit_on_a_bot_bump_skips(pr: Path, monkeypatch: pytest.MonkeyPatch,
                                                           capsys: pytest.CaptureFixture[str]) -> None:
    # trade-data #3040/#3012: renovate[bot] bumps pyproject, github-actions[bot] relocks uv.lock
    assert _run_relock(pr, monkeypatch, LOCK, {"uv.lock": b"ci-kit 1.27.1" + bytes([10])}) == "false"
    out = capsys.readouterr().out
    assert "Review tier: skipped (version bump only, 1 lines)" in out
    # the accepted commit is named by its SHA: an author string is the committer's to choose
    assert f"lockfile-only commits by another author accepted: {_git(pr, 'rev-parse', 'HEAD')[:8]}" in out
    assert "github-actions" not in out and "NOT taken" not in out


@pytest.mark.parametrize(("before", "relock", "allow_empty"), [
    (LOCK, {"app/x.py": b"x = 2" + bytes([10])}, False),                                  # a code file
    (LOCK, {"uv.lock": b"l2" + bytes([10]), "app/x.py": b"x = 2" + bytes([10])}, False),  # a lockfile AND code
    (LOCK, {"x.py": b"x" + bytes([10]), "uv.lock": b"l2" + bytes([10])}, False),          # a new code file beside it
    (LOCK, {"x-uv.lock": b"l2" + bytes([10])}, False),                                    # a lockfile SUFFIX is not a name
    (LOCK, {".claude/uv.lock": b"l2" + bytes([10])}, False),                              # under a not-prose prefix
    (LOCK, {"app/x.py": None, "poetry.lock": b"x = 1" + bytes([10])}, False),             # code RENAMED to a lockfile
    (LOCK, {}, True),                                                                     # an empty commit: nothing read
])
def test_a_foreign_commit_touching_anything_but_lockfiles_is_reviewed(
        pr: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        before: dict[str, bytes], relock: dict[str, bytes | None], allow_empty: bool) -> None:
    assert _run_relock(pr, monkeypatch, before, relock, allow_empty) == "true"
    out = capsys.readouterr().out
    assert "bump skip NOT taken for renovate[bot]" in out
    assert "failed lock: non-lockfile foreign commit" in out


def test_a_lockfile_renamed_to_code_is_reviewed(pr: Path, monkeypatch: pytest.MonkeyPatch,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    # `--no-renames`: the rename is a delete (uv.lock) plus an add (app/lock.py); the add is code
    lock = b"l" + bytes([10])
    assert _run_relock(pr, monkeypatch, {"uv.lock": lock}, {"uv.lock": None, "app/lock.py": lock}) == "true"
    assert "failed lock: non-lockfile foreign commit" in capsys.readouterr().out


def test_code_renamed_to_a_lockfile_name_is_not_lockfile_only(pr: Path) -> None:
    # with rename detection `--name-only` prints the NEW name alone: `--no-renames` is the rule
    _git(pr, "mv", "app/x.py", "uv.lock")
    _git(pr, "commit", "-qm", "mv")
    assert _git(pr, "diff", "--name-only", "-M", "HEAD~1", "HEAD") == "uv.lock"
    assert tier.lockfile_paths("HEAD~1", "HEAD") == []


def test_a_foreign_commit_on_a_guarded_lockfile_is_reviewed(pr: Path, monkeypatch: pytest.MonkeyPatch,
                                                           capsys: pytest.CaptureFixture[str]) -> None:
    # the skip drops the guard reason for the bot's version tokens, never for another author's lockfile
    relock = {"uv.lock": b"ci-kit 1.27.1" + bytes([10])}
    assert _run_relock(pr, monkeypatch, LOCK, relock, guard_files="requirements.txt uv.lock") == "true"
    out = capsys.readouterr().out
    assert f"failed lock: foreign commit {_git(pr, 'rev-parse', 'HEAD')[:8]} touches a guarded lockfile" in out
    assert tier.guarded_foreign({"aaaaaaaa": ["web/uv.lock"]}, "'web/uv.lock' x.py") == "aaaaaaaa"
    assert tier.guarded_foreign({"aaaaaaaa": ["web/uv.lock"]}, "uv.lock x.py") == ""
    assert tier.guarded_foreign({"aaaaaaaa": ["uv.lock"]}, "'unbalanced uv.lock") == "aaaaaaaa"


def _forge(pr: Path, author_email: str) -> None:
    """Rewrite HEAD with `author_email` exactly as given: `git commit` refuses a control byte in
    an identity, `git hash-object` does not, and neither does a push."""
    raw = subprocess.run(["git", "-C", str(pr), "cat-file", "commit", "HEAD"], check=True,
                         capture_output=True).stdout
    head, _, body = raw.partition(bytes([10, 10]))
    lines = [b"author t <" + author_email.encode() + b"> 5 +0000" if ln.startswith(b"author ") else
             b"committer t <t@t> 5 +0000" if ln.startswith(b"committer ") else ln
             for ln in head.split(bytes([10]))]
    obj = bytes([10]).join(lines) + bytes([10, 10]) + body
    sha = subprocess.run(["git", "-C", str(pr), "hash-object", "-t", "commit", "-w", "--stdin"],
                         input=obj, check=True, capture_output=True).stdout.decode().strip()
    _git(pr, "reset", "-q", "--hard", sha)


def test_a_commit_whose_author_field_forges_a_bot_row_is_reviewed(pr: Path) -> None:
    # one HAND commit of code, no bot commit at all. Its author email carries the field and line
    # separators, so a `splitlines()` parse read it as a bot row plus an honest merge of the base
    base = _git(pr, "rev-parse", "HEAD")
    (pr / "app" / "x.py").write_text("x = 2" + chr(10))
    _git(pr, "commit", "-qam", "code")
    us, rs = chr(0x1f), chr(0x1e)
    _forge(pr, f"{RENOVATE}{us}{tier.WEB_FLOW}{us}5{us}5{rs}{base}{us}{base} {base}{us}z")
    raw = _git(pr, "log", "--format=%H%x1f%P%x1f%ae%x1f%ce%x1f%at%x1f%ct", f"{base}..HEAD")
    assert [len(r.split(us)) for r in raw.splitlines()] == [6, 6]   # the forgery, as the old parse saw it
    failed, foreign = tier.bump_identity("renovate[bot]", "o/r", "o/r", base, "HEAD", signed=lambda r, s: True)
    assert failed == "unreadable commits (ValueError)" and foreign == {}


def test_a_lockfile_moved_between_lockfile_names_is_lockfile_only(pr: Path) -> None:
    lock = b"l" + bytes([10])
    (pr / "uv.lock").write_bytes(lock)
    _git(pr, "add", ".")
    _git(pr, "commit", "-qm", "lock")
    (pr / "web").mkdir()
    _git(pr, "mv", "uv.lock", "web/uv.lock")
    _git(pr, "commit", "-qm", "move")
    assert tier.lockfile_paths("HEAD~1", "HEAD") == ["uv.lock", "web/uv.lock"]
    _git(pr, "rm", "-q", "web/uv.lock")   # a deleted lockfile is still a lockfile-only commit; the
    _git(pr, "commit", "-qm", "rm")       # whole-PR bump_only refuses a deleted file on its own
    assert tier.lockfile_paths("HEAD~1", "HEAD") == ["web/uv.lock"]


def test_an_unreadable_commit_diff_is_reviewed_never_red(pr: Path, monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    real = tier._git

    def broken(*args: str) -> str:
        if args[:2] == ("diff", "--no-renames") and "-z" in args:
            raise subprocess.CalledProcessError(128, "git")
        return real(*args)
    monkeypatch.setattr(tier, "_git", broken)
    assert _run_relock(pr, monkeypatch, LOCK, {"uv.lock": b"l" + bytes([10])}) == "true"
    assert "failed lock: unreadable commits (CalledProcessError)" in capsys.readouterr().out


def test_a_root_commit_in_the_range_is_reviewed(pr: Path) -> None:
    # a shallow clone grafts its oldest commit parentless: the walk must not read it as a bump
    base = _bump_pr(pr, {})
    _git(pr, "checkout", "-q", "--orphan", "lonely")
    _git(pr, "-c", f"author.email={RENOVATE}", "-c", f"committer.email={tier.WEB_FLOW}", "commit", "-qm", "root")
    failed, foreign = tier.bump_identity("renovate[bot]", "o/r", "o/r", base, "HEAD", signed=lambda r, s: True)
    assert failed.startswith("root or shallow commit") and foreign == {}


def test_lockfile_only_commits_without_any_bot_commit_are_not_a_bot_bump(pr: Path) -> None:
    base = _git(pr, "rev-parse", "HEAD")
    _relock(pr, {"uv.lock": b"l" + bytes([10])})
    failed, _ = tier.bump_identity("renovate[bot]", "o/r", "o/r", base, "HEAD", signed=lambda r, s: True)
    assert failed == "identity (no commit by the bump bot)"


def test_the_refusal_names_a_non_bump_diff(pr: Path, monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("BASE_SHA", _bump_pr(pr, {"x.csv": b"a" + bytes([10])}))
    monkeypatch.setenv("HEAD_SHA", _git(pr, "rev-parse", "HEAD"))
    monkeypatch.setenv("PR_AUTHOR", "renovate[bot]")
    monkeypatch.setattr(tier, "verified", lambda repo, sha: True)
    assert tier.main(["--model", "m", "--guard-model", "g"]) == 0
    assert "failed lock: non-bump diff" in capsys.readouterr().out
