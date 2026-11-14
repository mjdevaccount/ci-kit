"""Tier 2's built-in release-binary pin rehash (ci_kit/pin_rehash.py) -- both directions.

MUST FIRE (ci-kit#79/#80, 2026-11-14): Renovate moved jscpd's / oasdiff's version line and not the
six hashes, so every such bump arrived red. A red PR whose diff touches a Pin file regenerates the
sha256 table from the assets of the version now named, and the pusher accepts the file.
MUST NOT FIRE: a hash already right moves nothing; the pusher refuses a "rehash" that changed
anything but sha256 literals, and refuses a sha256-only change to a file the PR does not touch.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from ci_kit import api_diff, dup_code, gitleaks, pin_rehash, pr_apply, pr_regen

SRC = '''from ci_kit import release_bin

# renovate: datasource=github-releases depName=o/tool
TOOL_VERSION = "{v}"
TOOL = release_bin.Pin(
    name="tool", version=TOOL_VERSION, env="CI_KIT_TOOL",
    url="https://github.com/o/tool/releases/download/v{{v}}/tool-{{asset}}.tar.gz",
    assets={{"linux-x64": "linux", "darwin-x64": "mac", "darwin-arm64": "mac"}},
    sha256={{
        "linux-x64": "{a}",
        "darwin-x64": "{b}",
        "darwin-arm64": "{b}",
    }},
)
'''


def h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def fake_fetch(calls: list[str]):
    def fetch(url: str, *, gate: str) -> bytes:
        calls.append(url)
        return url.encode()     # the "asset" is its own URL: a distinct, known digest per version
    return fetch


def url(v: str, asset: str) -> str:
    return f"https://github.com/o/tool/releases/download/v{v}/tool-{asset}.tar.gz"


def test_the_estates_real_pins_are_all_readable() -> None:
    for mod, pin in ((dup_code, dup_code.JSCPD), (api_diff, api_diff.OASDIFF), (gitleaks, gitleaks.GITLEAKS)):
        found = pin_rehash.pins(Path(mod.__file__).read_text(encoding="utf-8"))
        assert [f.name for f in found] == [pin.name]
        assert found[0].version == pin.version and {k: v[0] for k, v in found[0].hashes.items()} == dict(pin.sha256)


def test_a_bumped_version_rehashes_every_platform_one_fetch_per_asset_must_fire(tmp_path) -> None:
    old = SRC.format(v="1.0.0", a=h("x"), b=h("y"))
    f = tmp_path / "tool.py"
    f.write_text(old.replace('"1.0.0"', '"1.1.0"'), encoding="utf-8")
    calls: list[str] = []
    moved = pin_rehash.rehash(f, fake_fetch(calls))
    assert moved and "3 hash(es) moved" in moved[0]
    assert sorted(calls) == sorted([url("1.1.0", "linux"), url("1.1.0", "mac")])   # darwin_all fetched once
    found = pin_rehash.pins(f.read_text(encoding="utf-8"))[0]
    assert found.hashes["linux-x64"][0] == h(url("1.1.0", "linux"))
    assert found.hashes["darwin-arm64"][0] == found.hashes["darwin-x64"][0] == h(url("1.1.0", "mac"))
    assert pin_rehash.only_hashes(old.replace('"1.0.0"', '"1.1.0"'), f.read_text(encoding="utf-8"))


def test_a_current_pin_moves_nothing_must_not_fire(tmp_path) -> None:
    f = tmp_path / "tool.py"
    text = SRC.format(v="2.0.0", a=h(url("2.0.0", "linux")), b=h(url("2.0.0", "mac")))
    f.write_text(text, encoding="utf-8")
    assert pin_rehash.rehash(f, fake_fetch([])) == [] and f.read_text(encoding="utf-8") == text


def test_only_hashes_is_the_bound() -> None:
    a = 'x = "' + h("1") + '"\ny = 1\n'
    assert pin_rehash.only_hashes(a, a.replace(h("1"), h("2")))
    assert not pin_rehash.only_hashes(a, a)                                   # no change is not a rehash
    assert not pin_rehash.only_hashes(a, a.replace(h("1"), h("2")).replace("y = 1", "y = 2"))
    assert not pin_rehash.only_hashes(a, a + "z = 3\n")


def test_an_unreadable_pin_is_an_error_never_current(tmp_path) -> None:
    with pytest.raises(ValueError):
        pin_rehash.pins('Pin(name="t", version=V(), url="u", assets={}, sha256={})')
    with pytest.raises(ValueError):
        pin_rehash.pins('Pin(name="t", version="1", url="u", assets={"a": "a"}, sha256={"a": "nothex"})')
    bad, none = tmp_path / "bad.py", tmp_path / "none.py"
    bad.write_text('Pin(name="t", version=V(), url="u", assets={}, sha256={})\n', encoding="utf-8")
    none.write_text("x = 1\n", encoding="utf-8")
    assert pin_rehash.main([str(bad)]) == 1 and pin_rehash.main([str(none)]) == 0


def test_the_pusher_takes_a_sha256_only_pin_change_and_nothing_wider(tmp_path) -> None:
    d = tmp_path / "r"
    d.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *a],  # noqa: E731
                                  check=True, capture_output=True, text=True).stdout.strip()
    g("init", "-q", "-b", "main")
    (d / "tool.py").write_text(SRC.format(v="1.0.0", a=h("x"), b=h("y")), encoding="utf-8", newline="\n")
    (d / "other.py").write_text(SRC.format(v="1.0.0", a=h("x"), b=h("y")), encoding="utf-8", newline="\n")
    g("add", "-A")
    g("commit", "-qm", "init")
    base = g("rev-parse", "HEAD")
    bumped = (d / "tool.py").read_text(encoding="utf-8").replace('"1.0.0"', '"1.1.0"')
    (d / "tool.py").write_text(bumped, encoding="utf-8", newline="\n")
    g("commit", "-qam", "renovate bump")
    head = g("rev-parse", "HEAD")

    def art(name: str, files: dict[str, str]) -> Path:
        a = tmp_path / name
        (a / "files").mkdir(parents=True)
        for k, v in files.items():
            (a / "files" / k).write_text(v, encoding="utf-8", newline="\n")
        (a / "manifest.json").write_text(json.dumps({"tier": "regen", "base": base, "head": head, "merged": False,
                                                     "conflicted": [], "files": {k: "write" for k in files}}))
        return a

    rehashed = bumped.replace(h("x"), h("x2"))
    kw = {"head": head, "base": base, "ref": "main", "regen_globs": [], "max_lines": 300}
    assert pr_apply.apply(d, art("ok", {"tool.py": rehashed}), **kw)[0] == "ready"
    g("reset", "-q", "--hard", head)
    wider = rehashed.replace('env="CI_KIT_TOOL"', 'env="CI_KIT_OTHER"')
    result, why = pr_apply.apply(d, art("wide", {"tool.py": wider}), **kw)
    assert result == "refused" and "tool.py" in why
    other = (d / "other.py").read_text(encoding="utf-8").replace(h("x"), h("evil"))
    result, why = pr_apply.apply(d, art("other", {"other.py": other}), **kw)
    assert result == "refused" and "other.py" in why                         # not a file the PR changes


def test_a_red_pr_bumping_a_pin_version_gets_the_builtin_generator_must_fire(tmp_path) -> None:
    d = tmp_path / "r"
    (d / "ci_kit").mkdir(parents=True)
    g = lambda *a: subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *a],  # noqa: E731
                                  check=True, capture_output=True, text=True).stdout.strip()
    g("init", "-q", "-b", "main")
    src = Path(dup_code.__file__).read_text(encoding="utf-8")
    (d / "ci_kit" / "dup_code.py").write_text(src, encoding="utf-8")
    (d / "README.md").write_text("Pin( in prose is not python\n", encoding="utf-8")
    g("add", "-A")
    g("commit", "-qm", "init")
    base = g("rev-parse", "HEAD")
    files = ["ci_kit/dup_code.py", "README.md"]
    (d / "ci_kit" / "dup_code.py").write_text(src.replace("MIN_TOKENS, MIN_LINES = 70", "MIN_TOKENS, MIN_LINES = 71"),
                                              encoding="utf-8")
    assert pr_regen.pinned(d, files, base) == []        # touched, version unchanged: the hashes stay frozen
    bumped = src.replace(f'JSCPD_VERSION = "{dup_code.JSCPD_VERSION}"', 'JSCPD_VERSION = "9.9.9"')
    (d / "ci_kit" / "dup_code.py").write_text(bumped, encoding="utf-8")
    assert pr_regen.pinned(d, files, base) == ["ci_kit/dup_code.py"]
    e = pr_regen.pin_entry("ci_kit/dup_code.py", base)
    assert e.on == frozenset({pr_regen.RED}) and e.run.startswith(f"PYTHONSAFEPATH=1 python3 -m ci_kit.pin_rehash --since {base} ")
    assert pr_regen.CONFLICT not in e.on         # a conflicted pin file is a judgment, not a rehash


def test_only_a_bumped_pin_is_rehashed_a_same_version_pin_stays_frozen_must_not_fire(tmp_path) -> None:
    old = SRC.format(v="1.0.0", a=h("x"), b=h("y"))
    assert pin_rehash.bumped(old, old) == set() and pin_rehash.bumped(old, None) == {"tool"}
    assert pin_rehash.bumped(old.replace('"1.0.0"', '"1.1.0"'), old) == {"tool"} and pin_rehash.bumped(old, "") == {"tool"}
    f = tmp_path / "tool.py"
    f.write_text(old, encoding="utf-8")
    calls: list[str] = []
    assert pin_rehash.rehash(f, fake_fetch(calls), before=old) == [] and calls == []


def test_heal_commits_are_the_author_renovate_ignores() -> None:
    preset = (Path(__file__).resolve().parents[1] / "renovate" / "default.json5").read_text(encoding="utf-8")
    assert pr_regen.BOT[1] in preset and "gitIgnoredAuthors" in preset
