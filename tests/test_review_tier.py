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
    _git(pr, "-c", f"user.email={email}", "commit", "-qm", "bump")
    return base


def test_a_version_bump_pr_skips_the_model_and_says_so(pr: Path, monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("BASE_SHA", _bump_pr(pr, {}))
    monkeypatch.setenv("HEAD_SHA", _git(pr, "rev-parse", "HEAD"))
    monkeypatch.setenv("GUARD", "true")   # requirements.txt is a guard path in the caller
    monkeypatch.setenv("PR_AUTHOR", "renovate[bot]")
    assert tier.main(["--model", "m", "--guard-model", "g"]) == 0
    assert _outputs(pr / "out") == {"required": "false"}
    assert "Review tier: skipped (version bump only, 1 lines)" in capsys.readouterr().out
    assert "ruff==0.8.0" in (pr / "summary").read_text()


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
    assert tier.main(["--model", "m", "--guard-model", "g"]) == 0
    assert _outputs(pr / "out")["required"] == required


def test_a_non_utf8_file_is_reviewed_never_red(pr: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # reviewer 11-10 #2: the -U0 read must not turn a Latin-1 file into exit 2 the label cannot fix
    monkeypatch.setenv("BASE_SHA", _bump_pr(pr, {"x.csv": b"caf" + bytes([0xE9]) + bytes([10])}))
    monkeypatch.setenv("HEAD_SHA", _git(pr, "rev-parse", "HEAD"))
    monkeypatch.setenv("PR_AUTHOR", "renovate[bot]")
    assert tier.main(["--model", "m", "--guard-model", "g"]) == 0
    assert _outputs(pr / "out")["required"] == "true"
