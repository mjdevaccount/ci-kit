"""The pr-token record READ BACK (ci_kit/pr_token.py parse/classify, v1.53.0): one module owns both
directions of the grammar, so this suite holds them to each other.

  * ROUND TRIP: every record the composite's two steps can print -- driven through the real
    `decide` / `report` entry points over the whole input product -- parses, and parses back to
    exactly what was written.
  * REFUSED, NEVER SKIPPED: every malformed shape raises RecordError, and `check` counts it.
  * CLASSIFIED: every reason has a class, and each failing shape is a finding a naive
    "source is pat or app = fine" reader would miss.
"""
from __future__ import annotations

import io
import itertools
import json
import pathlib

import pytest

from ci_kit import pr_token as pt

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/pr-token/action.yml"
REPO = "o/r"


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, **env: str) -> pathlib.Path:
    out = tmp_path / "out"
    out.write_text("", encoding="utf-8")
    for k in ("CI_KIT_TOKEN_MODE", "CI_KIT_TOKEN_SCOPE", "HAS_APP_ID", "HAS_KEY", "HAS_PAT", "MINT", "SOURCE",
              "REASON", "MINT_OUTCOME", "APP_SLUG", "GITHUB_STEP_SUMMARY", *pt.DEPRECATED):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("GITHUB_JOB", "update")
    monkeypatch.setenv("GITHUB_WORKFLOW_REF", f"{REPO}/.github/workflows/pr-autoupdate.yml@refs/heads/main")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return out


def _printed_records(text: str) -> list[str]:
    return [ln.split("::", 2)[2] for ln in text.splitlines() if ln.startswith("::") and "::pr-token: " in ln]


def _every_emitted_record(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
                          capsys: pytest.CaptureFixture[str]) -> list[str]:
    """Run decide, then (when it passed) report, for every combination of inputs; collect every
    record line either step printed. Nothing is hand-built: these are the composite's own lines."""
    got: list[str] = []
    product = itertools.product(("pat", "app", "app-required", "Bogus Mode", ""), (True, False), (True, False),
                                (True, False), ("update", "push", "nope"), ("success", "failure", "skipped"),
                                ("example-bot", ""), ("octocat", ""))
    for mode, app_id, key, pat, scope, outcome, slug, pat_login in product:
        flags = {"HAS_APP_ID": str(app_id).lower(), "HAS_KEY": str(key).lower(), "HAS_PAT": str(pat).lower()}
        out = _env(monkeypatch, tmp_path, CI_KIT_TOKEN_MODE=mode, CI_KIT_TOKEN_SCOPE=scope, **flags)
        rc = pt.main(["decide"])
        said = capsys.readouterr().out
        got += _printed_records(said)
        if rc:
            continue
        outs = dict(x.split("=", 1) for x in out.read_text(encoding="utf-8").splitlines())
        _env(monkeypatch, tmp_path, CI_KIT_TOKEN_MODE=mode, CI_KIT_TOKEN_SCOPE=scope, MINT=outs["mint"],
             SOURCE=outs["source"], REASON=outs["reason"], MINT_OUTCOME=outcome if outs["mint"] == "true" else "skipped",
             APP_SLUG=slug)
        monkeypatch.setattr(pt, "_pat_login", lambda login=pat_login: login)
        pt.main(["report"])
        got += _printed_records(capsys.readouterr().out)
    return got


def test_every_record_the_steps_print_parses_back_to_what_was_written_must_fire(
        monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    records = _every_emitted_record(monkeypatch, tmp_path, capsys)
    seen = {pt.parse(r).reason for r in records}
    # the product reaches every reason but decide-crashed (the shell writes that one: next test)
    assert seen == set(pt.REASONS) - {"decide-crashed"}, seen
    for r in records:
        rec = pt.parse(r)
        assert list(rec.asdict()) == list(pt.KEYS)
        assert r == " ".join([pt.PREFIX, *(f"{k}={v}" for k, v in rec.asdict().items())])  # nothing lost or reordered


def test_the_shells_own_crash_record_parses() -> None:
    text = ACTION.read_text(encoding="utf-8")
    template = next(ln for ln in text.splitlines() if "reason=decide-crashed" in ln)
    assert "${" in template                         # the template itself is refused ...
    with pytest.raises(pt.RecordError, match="template"):
        pt.parse(template.split('"')[1])
    rec = pt.parse("pr-token: source=none mode=app reason=decide-crashed app=absent repo=o/r "
                   "workflow=pr-autoupdate.yml job=update login=unknown")  # ... its rendering parses
    assert rec.reason == "decide-crashed" and pt.classify(rec, REPO)


@pytest.mark.parametrize("field", ["mode", "repo", "workflow", "job", "login"])
@pytest.mark.parametrize("hostile", ["a b", "a=b", "x\ny", "\t", "", "  ", "${x}", "ünï"])
def test_hostile_free_fields_still_round_trip_or_are_refused_never_misread(field: str, hostile: str) -> None:
    kw = {"mode": "pat", "app": "absent", "repo": REPO, "workflow": "w.yml", "job": "j", "login": "me"}
    kw[field] = hostile
    ln = pt.line(pt.Decision(False, "pat", "mode-pat"), **kw)
    if "${" in hostile:
        with pytest.raises(pt.RecordError):
            pt.parse(ln)
        return
    rec = pt.parse(ln)
    assert getattr(rec, field) == pt._field(hostile.lower() if field == "mode" else hostile,
                                            {"mode": "pat", "login": "unknown"}.get(field, "?"))


def test_an_empty_reason_renders_as_a_refused_record_never_a_silent_one() -> None:
    ln = pt.line(pt.Decision(False, "pat", ""), "pat", "absent", REPO, "w.yml", "j", "me")
    assert "reason=?" in ln
    with pytest.raises(pt.RecordError, match="reason="):
        pt.parse(ln)


GOOD = "pr-token: source=pat mode=pat reason=mode-pat app=absent repo=o/r workflow=w.yml job=update login=me"


@pytest.mark.parametrize("bad,why", [
    (GOOD + " extra=1", "unknown key"),
    (GOOD.replace(" login=me", ""), "missing"),
    (GOOD + " source=pat", "duplicated"),
    (GOOD.replace("app=absent", "app="), "bad token"),
    (GOOD.replace("app=absent", "app=a=b"), "bad token"),
    (GOOD.replace(" mode=pat", "  mode=pat"), "bad token"),
    (GOOD.replace("source=pat", "source=github"), "source="),
    (GOOD.replace("app=absent", "app=maybe"), "app="),
    (GOOD.replace("reason=mode-pat", "reason=brand-new"), "reason="),
    (GOOD.replace("mode=pat", "mode=${mode:-pat}"), "template"),
    ("pr-token:source=pat", "does not start"),
    ("pr-tokens: " + GOOD[10:], "does not start"),
    (GOOD + "\nsecond line", "more than one line"),
    (GOOD.replace("source=pat", "SOURCE=pat"), "bad token"),
])
def test_every_malformed_shape_is_refused_must_fire(bad: str, why: str) -> None:
    with pytest.raises(pt.RecordError, match=why):
        pt.parse(bad)
    _, findings = pt.check(bad, "lines", REPO)
    assert findings and findings[0].startswith("unparseable record")


def test_every_reason_is_classified_and_nothing_else() -> None:
    assert set(pt.REASON_CLASS) == set(pt.REASONS)
    assert set(pt.REASON_CLASS.values()) == {"ok", "fallback", "failure"}


def _rec(**kw: str) -> pt.Record:
    base = {"source": "pat", "mode": "pat", "reason": "mode-pat", "app": "absent", "repo": REPO,
            "workflow": "w.yml", "job": "update", "login": "me"}
    return pt.Record(**{**base, **kw})


@pytest.mark.parametrize("kw,want", [
    ({"app": "failed"}, "app=failed"),
    ({"source": "none", "reason": "identity-unreadable", "login": "unknown"}, "reason=identity-unreadable"),
    ({"source": "none", "reason": "decide-crashed"}, "reason=decide-crashed"),
    ({"source": "none", "mode": "app", "reason": "mint-failed", "app": "failed"}, "reason=mint-failed"),
    ({"source": "none", "mode": "bogus", "reason": "mode-invalid"}, "reason=mode-invalid"),
    ({"source": "none", "reason": "scope-invalid"}, "reason=scope-invalid"),
    ({"mode": "app", "reason": "key-absent"}, "asked for the App"),
    ({"mode": "app", "reason": "var-absent"}, "asked for the App"),
    ({"source": "none", "mode": "app-required", "reason": "key-absent"}, "asked for the App"),
    ({"source": "app", "reason": "configured", "app": "minted"}, "contract violation"),
    ({"repo": "o/other"}, "record names repo"),
    ({"mode": "weird"}, "mode=weird is not one of"),
])
def test_each_failing_record_is_a_finding_must_fire(kw: dict, want: str) -> None:
    got = pt.classify(_rec(**kw), REPO)
    assert any(want in x for x in got), got


@pytest.mark.parametrize("kw", [
    {}, {"app": "minted"}, {"login": "unknown"},
    {"source": "app", "mode": "app", "reason": "configured", "app": "minted", "login": "example-bot[bot]"},
    {"source": "app", "mode": "app-required", "reason": "configured", "app": "minted"},
])
def test_healthy_records_are_quiet(kw: dict) -> None:
    assert pt.classify(_rec(**kw), REPO) == []


ECHO = ('j\tUNKNOWN STEP\t2026-11-15T18:59:55.6Z \x1b[36;1mrec="pr-token: source=none mode=${mode:-pat} '
        'reason=decide-crashed app=absent"\x1b[0m')


def test_the_log_reader_takes_only_anchored_lines_must_fire() -> None:
    log = "\n".join([f"j\tdecide\t2026-11-15T18:59:55.6Z ##[notice]{GOOD}\r", ECHO,
                     f"j\tdecide\t2026-11-15T18:59:55.6Z echo '{GOOD}'", f"j\tdecide\t2026-11-15T18:59:55.6Z {GOOD}",
                     "##[notice]pr-token: source=none mode=${mode:-pat} reason=decide-crashed", f"##[error]{GOOD}"])
    assert pt.records_in_log(log) == [GOOD, GOOD]


def test_the_annotation_reader_takes_only_record_messages() -> None:
    anns = [{"title": "pr-token", "message": GOOD}, {"title": "pr-token", "message": "the App canary mint FAILED"},
            {"title": "", "message": GOOD}, {"title": "pr-token", "message": "pr-token: x=${y}"}, "junk"]
    assert pt.records_in_annotations(anns) == [GOOD]


def _cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], text: str, *args: str) -> tuple[int, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    rc = pt.check_main(list(args))
    cap = capsys.readouterr()
    return rc, cap.out + cap.err


def test_cli_clean_is_0(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = _cli(monkeypatch, capsys, f"j\tdecide\t2026-11-15T18:59:55.6Z ##[notice]{GOOD}\n", "--repo", REPO)
    assert rc == 0 and json.loads(out.splitlines()[0])["source"] == "pat"


@pytest.mark.parametrize("text,fmt", [
    (f"##[warning]{GOOD.replace('app=absent', 'app=failed')}", "log"),     # a finding
    (GOOD.replace("app=absent", "app=nope"), "lines"),                       # unparseable
    ("nothing here\n", "log"),                                               # no record at all
    (json.dumps([{"title": "pr-token", "message": "prose only"}]), "annotations"),
])
def test_cli_findings_are_1_must_fire(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
                                      text: str, fmt: str) -> None:
    rc, out = _cli(monkeypatch, capsys, text, "--format", fmt)
    assert rc == 1 and "FINDING:" in out


def test_cli_unreadable_is_2(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
                             tmp_path: pathlib.Path) -> None:
    assert _cli(monkeypatch, capsys, "", str(tmp_path / "missing.log"))[0] == 2
    rc, out = _cli(monkeypatch, capsys, "{not json", "--format", "annotations")
    assert rc == 2 and "COULD NOT LOOK" in out
    bad = tmp_path / "bad.log"
    bad.write_bytes(b"\xff\xfe\x00junk")
    assert _cli(monkeypatch, capsys, "", str(bad))[0] == 2


def test_the_module_entry_routes_check(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(GOOD + "\n"))
    assert pt.main(["check", "--format", "lines"]) == 0
    with pytest.raises(SystemExit):
        pt.main(["decide", "--format", "lines"])
