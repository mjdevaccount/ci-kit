"""The push token: GitHub App or PAT, staged, and WHO it is (ci_kit/pr_token.py, .github/actions/pr-token).

v1.37.0. MUST FIRE: app mode with the App configured -> the App acts; app-required with it absent ->
red; app mode with a FAILED mint -> red, never a silent PAT; an unreadable acting identity -> red;
base-retry under an App token reads no GET /user and honours the PAT's old markers (and the reverse
on rollback). MUST NOT FIRE: the default (no mode) stays the PAT; a forged marker by any other
login -- another Bot included -- grants nothing; the no-author case never admits the edge-bot.
The composite and the five reusables are checked as YAML: what cannot be run here is named in
the PR (the mint itself, the post-step revoke, `vars` resolution in a called workflow).
"""
from __future__ import annotations

import json
import pathlib

import pytest
import yaml

from ci_kit import pr_baseretry as br
from ci_kit import pr_token as pt

ROOT = pathlib.Path(__file__).resolve().parent.parent
ACTION = ROOT / ".github" / "actions" / "pr-token" / "action.yml"
WF = ROOT / ".github" / "workflows"
PAT_USER, BOT = "mjdevaccount", "mjdev-edge-bot[bot]"


# ── the policy ───────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("mode,app_id,key,pat,want", [
    # the DEFAULT is the PAT whatever is configured: v1 moving cuts nobody over
    ("", True, True, True, ("pat", "mode=pat", True)),           # key passed: a canary mint, unused
    ("pat", True, False, True, ("pat", "mode=pat", False)),
    ("PAT", False, False, True, ("pat", "mode=pat", False)),
    # app: the App when configured, else the PAT with the reason
    ("app", True, True, True, ("app", "configured", True)),
    ("app", True, True, False, ("app", "configured", True)),     # no PAT needed
    ("app", False, True, True, ("pat", "var-absent", False)),
    ("app", True, False, True, ("pat", "key-absent", False)),
    # app-required: the App or red
    ("app-required", True, True, False, ("app", "configured", True)),
])
def test_decide_the_table(mode: str, app_id: bool, key: bool, pat: bool, want: tuple) -> None:
    d = pt.decide(mode, app_id, key, pat)
    assert (d.source, d.reason, d.mint) == want and d.error == ""


@pytest.mark.parametrize("mode,app_id,key,pat,reason", [
    ("app-required", False, True, True, "var-absent"),   # never the PAT, even when it is there
    ("app-required", True, False, True, "key-absent"),
    ("pat", True, True, False, "mode=pat"),               # nothing to act with: red, as before
    ("app", True, False, False, "key-absent"),
    ("apps", True, True, True, "mode-invalid"),           # a typo is red, never a silent PAT
])
def test_decide_refuses_must_fire(mode: str, app_id: bool, key: bool, pat: bool, reason: str) -> None:
    d = pt.decide(mode, app_id, key, pat)
    assert d.source == "none" and d.reason == reason and d.error


def test_a_failed_mint_in_app_mode_is_red_never_the_pat_must_fire() -> None:
    d = pt.decide("app", True, True, True)
    acting, trusted, app, err = pt.resolve(d, "failure", "", PAT_USER)
    assert err and acting == "" and trusted == [] and app == "failed"


def test_a_failed_canary_in_pat_mode_leaves_the_pat_acting() -> None:
    d = pt.decide("pat", True, True, True)
    assert pt.resolve(d, "failure", "", PAT_USER) == (PAT_USER, [PAT_USER], "failed", "")


def test_resolve_names_both_identities_from_config() -> None:
    app = pt.decide("app", True, True, True)
    assert pt.resolve(app, "success", "mjdev-edge-bot", PAT_USER) == (BOT, [BOT, PAT_USER], "minted", "")
    canary = pt.decide("pat", True, True, True)            # rollback: the bot's markers still count
    assert pt.resolve(canary, "success", "mjdev-edge-bot", PAT_USER) == (PAT_USER, [PAT_USER, BOT], "minted", "")
    plain = pt.decide("pat", False, False, True)
    assert pt.resolve(plain, "skipped", "", PAT_USER) == (PAT_USER, [PAT_USER], "absent", "")


def test_an_unreadable_acting_identity_is_red_must_fire() -> None:
    assert pt.resolve(pt.decide("pat", False, False, True), "skipped", "", "")[3]           # GET /user failed
    assert pt.resolve(pt.decide("app", True, True, True), "success", "", PAT_USER)[3]       # no app-slug


def test_one_spelling_and_only_a_bot_gets_the_suffix() -> None:
    assert pt.rest_login("mjdev-edge-bot", "Bot") == BOT                # GraphQL's spelling of the App
    assert pt.rest_login(BOT, "Bot") == BOT
    assert pt.rest_login("mjdev-edge-bot", "User") == "mjdev-edge-bot"  # a person who took the slug
    assert pt.bot_login("mjdev-edge-bot") == BOT and pt.bot_login("") == ""


def test_trusted_logins_never_widen_an_unknown_identity() -> None:
    env = {pt.TRUSTED_ENV: f"{BOT}, {PAT_USER}"}
    assert pt.trusted_logins(BOT, env) == {BOT, PAT_USER}
    assert pt.trusted_logins("", env) == frozenset()                     # unknown = nobody, never "anybody"
    assert pt.trusted_logins(PAT_USER, {}) == {PAT_USER}


def test_the_record_is_one_fixed_greppable_line() -> None:
    d = pt.decide("app", False, True, True)
    got = pt.line(d, "app", "absent", "o/r", "pr-autoupdate.yml", "update", PAT_USER)
    assert got == ("pr-token: source=pat mode=app reason=var-absent app=absent repo=o/r "
                   "workflow=pr-autoupdate.yml job=update login=mjdevaccount")


def _cli(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, step: str, pat_login: str = PAT_USER,
         **env: str) -> tuple[int, dict[str, str], str]:
    out, summ = tmp_path / "out", tmp_path / "summary"
    for k in ("CI_KIT_TOKEN_MODE", "CI_KIT_TOKEN_SCOPE", "HAS_APP_ID", "HAS_KEY", "HAS_PAT", "MINT", "SOURCE", "REASON",
              "MINT_OUTCOME", "APP_SLUG"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summ))
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_JOB", "update")
    monkeypatch.setenv("GITHUB_WORKFLOW_REF", "o/r/.github/workflows/pr-autoupdate.yml@refs/heads/main")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(pt, "_pat_login", lambda: pat_login)
    rc = pt.main([step])
    outs = dict(x.split("=", 1) for x in out.read_text(encoding="utf-8").splitlines()) if out.exists() else {}
    return rc, outs, summ.read_text(encoding="utf-8") if summ.exists() else ""


def test_the_steps_write_outputs_and_the_record(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    rc, outs, _ = _cli(monkeypatch, tmp_path, "decide", CI_KIT_TOKEN_MODE="app", CI_KIT_TOKEN_SCOPE="update",
                       HAS_APP_ID="true", HAS_KEY="true", HAS_PAT="true")
    assert rc == 0 and outs == {"mint": "true", "source": "app", "reason": "configured", "mode": "app"}
    (tmp_path / "out").unlink()
    rc, outs, summary = _cli(monkeypatch, tmp_path, "report", CI_KIT_TOKEN_MODE="app", MINT="true", SOURCE="app",
                             REASON="configured", MINT_OUTCOME="success", APP_SLUG="mjdev-edge-bot")
    assert rc == 0 and outs == {"login": BOT, "trusted": f"{BOT},{PAT_USER}"}
    line = ("pr-token: source=app mode=app reason=configured app=minted repo=o/r workflow=pr-autoupdate.yml "
            f"job=update login={BOT}")
    assert f"::notice title=pr-token::{line}" in capsys.readouterr().out and line in summary


def test_a_failed_mint_still_writes_its_record_and_is_red(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    rc, outs, summary = _cli(monkeypatch, tmp_path, "report", CI_KIT_TOKEN_MODE="app", MINT="true", SOURCE="app",
                             REASON="configured", MINT_OUTCOME="failure")
    said = capsys.readouterr().out
    assert rc == 1 and outs == {} and "source=none mode=app reason=mint-failed app=failed" in summary
    assert "::error title=pr-token::" in said


def test_an_invalid_mode_is_red_with_its_record(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    rc, outs, summary = _cli(monkeypatch, tmp_path, "decide", CI_KIT_TOKEN_MODE="appp", HAS_PAT="true")
    assert rc == 1 and outs["source"] == "none" and "reason=mode-invalid" in summary


# ── base-retry under each identity (pr_baseretry) ────────────────────────────────────────────────
H = "a" * 40


def _note(head: str, who: str) -> tuple[str, str]:
    return who, br.MARK.format(head=head, base="1" * 40)


class _Done:
    def __init__(self, rc: int = 0, out: str = "") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, ""


def test_whoami_under_an_app_token_never_calls_user_must_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    # OLD CODE: GET /user -> 403 for an installation token -> '' -> the feature refused every pass
    calls: list[tuple] = []
    monkeypatch.setenv(pt.LOGIN_ENV, BOT)
    assert br.whoami(lambda *a, **k: calls.append(a) or _Done(1)) == BOT and calls == []
    monkeypatch.delenv(pt.LOGIN_ENV)
    assert br.whoami(lambda *a, **k: _Done(0, PAT_USER + chr(10))) == PAT_USER   # a direct PAT caller: as before


def test_markers_from_both_configured_identities_count_across_a_flip_must_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    # OLD CODE: only `me` counted, so a flip reset the cap and re-fired a spent head
    notes = [_note(H, PAT_USER), _note("b" * 40, BOT), _note("c" * 40, "renovate[bot]"), _note("d" * 40, "mallory")]
    monkeypatch.setenv(pt.TRUSTED_ENV, f"{BOT},{PAT_USER}")
    assert br.trusted(notes, pt.trusted_logins(BOT), br._MARK) == {H, "b" * 40}          # after the flip
    assert br.trusted(notes, pt.trusted_logins(PAT_USER), br._MARK) == {H, "b" * 40}     # after a rollback
    monkeypatch.delenv(pt.TRUSTED_ENV)
    assert br.trusted(notes, pt.trusted_logins(BOT), br._MARK) == {"b" * 40}             # nothing configured: own only
    assert br.trusted(notes, BOT, br._MARK) == {"b" * 40} and br.trusted(notes, "", br._MARK) == set()


def test_the_edge_bot_never_qualifies_for_the_no_author_case() -> None:
    # it is a Bot too, and it pushes onto PRs -- but the case keys on the PR's AUTHOR, exactly listed
    p = {"author": {"__typename": "Bot", "login": "mjdev-edge-bot"}}
    assert "is not a configured no-author bot" in br.bot_reason(p, ["test"], br.Evidence([], [], []),
                                                                frozenset({"renovate"}))


# ── the composite and the reusables, as YAML ──────────────────────────────────────────────────────
def _yaml(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_the_composite_pins_the_mint_by_sha_and_never_shells_the_token() -> None:
    text = ACTION.read_text(encoding="utf-8")
    steps = {s["id"]: s for s in _yaml(ACTION)["runs"]["steps"]}
    mint = steps["mint"]
    assert mint["uses"].split("@")[0] == "actions/create-github-app-token"
    sha = mint["uses"].split("@")[1]
    assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)
    assert f"{mint['uses']} # v3.3.0" in text
    assert mint["with"]["owner"] == "${{ github.repository_owner }}"
    assert mint["with"]["repositories"] == "${{ github.repository }}"                  # this repo only
    assert mint["continue-on-error"] == "${{ steps.decide.outputs.source == 'pat' }}"  # app modes: a failed mint is red
    assert steps["report"]["if"].startswith("always()")                                # the record survives a failed mint
    out = _yaml(ACTION)["outputs"]["token"]["value"]
    assert out == "${{ steps.decide.outputs.source == 'app' && steps.mint.outputs.token || inputs.pat }}"
    for s in steps.values():  # no shell ever holds the key or the App token (the PAT only for GET /user)
        env = list((s.get("env") or {}).values())
        assert "${{ inputs.private-key }}" not in env and not any("mint.outputs.token" in v for v in env)


def granted(action: dict) -> dict[str, dict[str, str]]:
    """The App permissions the composite's mint step asks for, per scope, read off its expressions."""
    mint = next(s for s in action["runs"]["steps"] if s.get("id") == "mint")
    out: dict[str, dict[str, str]] = {s: {} for s in pt.PERMISSIONS}
    for k, v in mint["with"].items():
        if not k.startswith("permission-"):
            continue
        scopes = json.loads(v.split("fromJSON('")[1].split("')")[0])
        level = v.split("&& '")[1].split("'")[0]
        for s in scopes:
            out.setdefault(s, {})[k[len("permission-"):]] = level
    return out


def test_the_composite_asks_for_exactly_the_permission_table() -> None:
    assert granted(_yaml(ACTION)) == pt.PERMISSIONS


def test_a_widened_grant_is_caught_must_fire() -> None:
    action = _yaml(ACTION)
    mint = next(s for s in action["runs"]["steps"] if s.get("id") == "mint")
    mint["with"]["permission-administration"] = "${{ contains(fromJSON('[\"update\",\"comment\"]'), inputs.scope) && 'write' || '' }}"
    assert granted(action) != pt.PERMISSIONS


def test_an_unknown_scope_is_red(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    rc, outs, summary = _cli(monkeypatch, tmp_path, "decide", CI_KIT_TOKEN_MODE="app", CI_KIT_TOKEN_SCOPE="everything",
                             HAS_APP_ID="true", HAS_KEY="true", HAS_PAT="true")
    assert rc == 1 and outs["source"] == "none" and "reason=scope-invalid" in summary


TOKEN_JOBS = {"pr-autoupdate.yml": ["update"], "merge-train.yml": ["train"], "ci-green.yml": ["advance"],
              "pr-heal.yml": ["claim", "push", "report"]}


def token_faults(wf: dict, jobs: list[str]) -> list[str]:
    """Why a reusable's token jobs do not take their token from the one pr-token step ([] = clean)."""
    out = []
    call = (wf.get("on") or wf.get(True))["workflow_call"]
    if (call["secrets"].get("EDGE_BOT_PRIVATE_KEY") or {}).get("required") is not False:
        out.append("EDGE_BOT_PRIVATE_KEY is not an optional workflow_call secret")
    if (call["inputs"].get("token-source") or {}).get("default") != "":
        out.append("no token-source input defaulting to '' (= the variable, else pat)")
    for j in jobs:
        job = wf["jobs"][j]
        steps = job["steps"]
        ids = [s.get("id") for s in steps]
        if "token" not in ids:
            out.append(f"{j}: no pr-token step")
            continue
        tok = steps[ids.index("token")]
        if tok.get("uses") != "mjdevaccount/ci-kit/.github/actions/pr-token@v1":
            out.append(f"{j}: the token step is not ci-kit's pr-token@v1")
        w = tok.get("with") or {}
        if w.get("scope") not in pt.PERMISSIONS:
            out.append(f"{j}: the token step names no known permission scope")
        if w.get("mode") != "${{ inputs.token-source || vars.PR_TOKEN_SOURCE }}" or w.get("pat") != "${{ secrets.token }}":
            out.append(f"{j}: the token step's mode or pat is not the staged wiring")
        rest = json.dumps([job.get("env", {}), [s for s in steps if s is not tok]])
        if "secrets.token" in rest:
            out.append(f"{j}: the PAT is used past the token step")
        if "steps.token.outputs.token" not in rest:
            out.append(f"{j}: nothing uses the chosen token")
        if "steps.token" in json.dumps(steps[:ids.index("token")]):
            out.append(f"{j}: a step uses the token before it is chosen")
    return out


def test_every_token_job_takes_its_token_from_the_one_step() -> None:
    for name, jobs in TOKEN_JOBS.items():
        assert token_faults(_yaml(WF / name), jobs) == [], name


def test_the_check_catches_a_job_that_still_reads_the_pat_must_fire() -> None:
    wf = _yaml(WF / "merge-train.yml")
    wf["jobs"]["train"]["env"]["GH_TOKEN"] = "${{ secrets.token }}"
    assert "train: the PAT is used past the token step" in token_faults(wf, ["train"])
    wf = _yaml(WF / "pr-heal.yml")
    wf["jobs"]["push"]["steps"] = [s for s in wf["jobs"]["push"]["steps"] if s.get("id") != "token"]
    assert "push: no pr-token step" in token_faults(wf, ["push"])
    wf = _yaml(WF / "ci-green.yml")
    (wf.get("on") or wf.get(True))["workflow_call"]["secrets"].pop("EDGE_BOT_PRIVATE_KEY")
    assert any("EDGE_BOT_PRIVATE_KEY" in f for f in token_faults(wf, ["advance"]))


def test_the_identity_reaches_every_tool_that_trusts_markers() -> None:
    for name, job, step in (("pr-autoupdate.yml", "update", "update"), ("merge-train.yml", "train", None)):
        steps = _yaml(WF / name)["jobs"][job]["steps"]
        s = next(x for x in steps if (x.get("id") == step if step else "ci-merge-train" in json.dumps(x)))
        assert s["env"]["CI_KIT_TOKEN_LOGIN"] == "${{ steps.token.outputs.login }}"
        assert s["env"]["CI_KIT_TRUSTED_LOGINS"] == "${{ steps.token.outputs.trusted }}"
