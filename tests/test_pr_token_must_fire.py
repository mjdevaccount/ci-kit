"""The push token: GitHub App or PAT, staged, and WHO it is (ci_kit/pr_token.py, .github/actions/pr-token).

v1.37.0. MUST FIRE: app mode with the App configured -> the App acts; app-required with it absent ->
red; app mode with a FAILED mint -> red, never a silent PAT; an unreadable acting identity -> red;
base-retry under an App token reads no GET /user and honours the PAT's old markers (and the reverse
on rollback). MUST NOT FIRE: the default (no mode) stays the PAT; a forged marker by any other
login -- another Bot included -- grants nothing; the no-author case never admits the App.
The composite and the five reusables are checked as YAML: what cannot be run here is named in
the PR (the mint itself, the post-step revoke, `vars` resolution in a called workflow).
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest
import yaml

from ci_kit import pr_baseretry as br
from ci_kit import pr_token as pt

ROOT = pathlib.Path(__file__).resolve().parent.parent
ACTION = ROOT / ".github" / "actions" / "pr-token" / "action.yml"
WF = ROOT / ".github" / "workflows"
PAT_USER, BOT = "octocat", "example-app[bot]"
BASH = shutil.which("bash")


# ── the policy ───────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("mode,app_id,key,pat,want", [
    # the DEFAULT is the PAT whatever is configured: v1 moving cuts nobody over
    ("", True, True, True, ("pat", "mode-pat", True)),           # key passed: a canary mint, unused
    ("pat", True, False, True, ("pat", "mode-pat", False)),
    ("PAT", False, False, True, ("pat", "mode-pat", False)),
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
    ("pat", True, True, False, "mode-pat"),               # nothing to act with: red, as before
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
    assert pt.resolve(app, "success", "example-app", PAT_USER) == (BOT, [BOT, PAT_USER], "minted", "")
    canary = pt.decide("pat", True, True, True)            # rollback: the bot's markers still count
    assert pt.resolve(canary, "success", "example-app", PAT_USER) == (PAT_USER, [PAT_USER, BOT], "minted", "")
    plain = pt.decide("pat", False, False, True)
    assert pt.resolve(plain, "skipped", "", PAT_USER) == (PAT_USER, [PAT_USER], "absent", "")


def test_an_unreadable_acting_identity_is_red_must_fire() -> None:
    assert pt.resolve(pt.decide("pat", False, False, True), "skipped", "", "")[3]           # GET /user failed
    assert pt.resolve(pt.decide("app", True, True, True), "success", "", PAT_USER)[3]       # no app-slug


def test_an_unreadable_identity_is_not_red_where_nothing_trusts_markers() -> None:
    plain = pt.decide("pat", False, False, True)
    assert pt.resolve(plain, "skipped", "", "", needs_identity=False) == ("", [], "absent", "")
    assert pt.resolve(pt.decide("app", True, True, True), "success", "", PAT_USER, needs_identity=False)[3]  # still red


def test_the_app_login_is_the_rest_spelling() -> None:
    assert pt.bot_login("example-app") == BOT and pt.bot_login("") == ""


def test_trusted_logins_never_widen_an_unknown_identity() -> None:
    env = {pt.TRUSTED_ENV: f"{BOT}, {PAT_USER}"}
    assert pt.trusted_logins(BOT, env) == {BOT, PAT_USER}
    assert pt.trusted_logins("", env) == frozenset()                     # unknown = nobody, never "anybody"
    assert pt.trusted_logins(PAT_USER, {}) == {PAT_USER}


def test_the_record_is_one_fixed_greppable_line() -> None:
    d = pt.decide("app", False, True, True)
    got = pt.line(d, "app", "absent", "o/r", "pr-autoupdate.yml", "update", PAT_USER)
    assert got == ("pr-token: source=pat mode=app reason=var-absent app=absent repo=o/r "
                   "workflow=pr-autoupdate.yml job=update login=octocat")


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
                             REASON="configured", MINT_OUTCOME="success", APP_SLUG="example-app")
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


def test_the_app_never_qualifies_for_the_no_author_case() -> None:
    # it is a Bot too, and it pushes onto PRs -- but the case keys on the PR's AUTHOR, exactly listed
    p = {"author": {"__typename": "Bot", "login": "example-app"}}
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
    for s in steps.values():  # no shell ever holds the key or the App token (the PAT only for GET /user)
        env = list((s.get("env") or {}).values())
        assert "${{ inputs.private-key }}" not in env and not any("mint.outputs.token" in v for v in env)


# The expression subset the composite's `with:`/`outputs:` use, evaluated (no GitHub expression
# library on PyPI covers it; this is the one-off a test needs): `&&`/`||` return an OPERAND, like
# Python's and/or, and '' is falsy in both.
_NAMES = {"steps.decide.outputs.source": "source", "steps.mint.outputs.token": "mint_token",
          "inputs.pat": "pat", "inputs.scope": "scope"}


def gh_eval(expr: str, **ctx: str) -> str:
    body = expr.strip().removeprefix("${{").removesuffix("}}")
    body = re.sub(r"contains\(fromJSON\('(\[[^']*\])'\), (inputs\.scope)\)", r"(\2 in \1)", body)
    for k, v in _NAMES.items():
        body = body.replace(k, v)
    body = body.replace("&&", " and ").replace("||", " or ")
    return eval(body, {"__builtins__": {}}, ctx)  # noqa: S307 -- the test's own fixture text


def granted(action: dict, source: str) -> dict[str, dict[str, str]]:
    """The App permissions the composite's mint step asks for, per scope, when `source` acts."""
    mint = next(s for s in action["runs"]["steps"] if s.get("id") == "mint")
    out: dict[str, dict[str, str]] = {}
    for scope in pt.PERMISSIONS:
        got = {k[len("permission-"):]: gh_eval(v, source=source, scope=scope)
               for k, v in mint["with"].items() if k.startswith("permission-")}
        out[scope] = {k: v for k, v in got.items() if v}
    return out


def test_the_app_asks_for_exactly_the_permission_table() -> None:
    assert granted(_yaml(ACTION), "app") == pt.PERMISSIONS


def test_the_pat_mode_canary_asks_for_metadata_read_and_nothing_else_must_fire() -> None:
    assert granted(_yaml(ACTION), "pat") == {s: {"metadata": "read"} for s in pt.PERMISSIONS}


def test_no_mint_ever_requests_an_empty_set_which_inherits_everything() -> None:
    # create-github-app-token: NO permission input => the installation's WHOLE permission set
    for source in ("app", "pat"):
        assert all(granted(_yaml(ACTION), source).values()), source


def test_a_widened_grant_is_caught_must_fire() -> None:
    action = _yaml(ACTION)
    mint = next(s for s in action["runs"]["steps"] if s.get("id") == "mint")
    mint["with"]["permission-administration"] = "${{ contains(fromJSON('[\"update\",\"comment\"]'), inputs.scope) && 'write' || '' }}"
    assert granted(action, "app") != pt.PERMISSIONS
    assert granted(action, "pat") != {s: {"metadata": "read"} for s in pt.PERMISSIONS}  # ungated: the canary gets it


@pytest.mark.parametrize(("source", "mint_token", "pat", "want"), [
    ("app", "APP", "PAT", "APP"),
    ("app", "", "PAT", ""),        # an empty App token is '', NEVER the PAT
    ("pat", "CANARY", "PAT", "PAT"),  # the canary is never handed out
    ("pat", "", "PAT", "PAT"),
    ("", "", "PAT", ""),           # no decided source: nothing
])
def test_the_token_output_is_fail_closed(source: str, mint_token: str, pat: str, want: str) -> None:
    out = _yaml(ACTION)["outputs"]["token"]["value"]
    assert gh_eval(out, source=source, mint_token=mint_token, pat=pat) == want


def test_an_unknown_scope_is_red(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    rc, outs, summary = _cli(monkeypatch, tmp_path, "decide", CI_KIT_TOKEN_MODE="app", CI_KIT_TOKEN_SCOPE="everything",
                             HAS_APP_ID="true", HAS_KEY="true", HAS_PAT="true")
    assert rc == 1 and outs["source"] == "none" and "reason=scope-invalid" in summary


# THE GENERIC NAMES (v1.40.0) and the deprecated aliases they replace, each still accepted
NEW_KEY, OLD_KEY = "app-private-key", "EDGE_BOT_PRIVATE_KEY"
NEW_ID, OLD_ID = "app-client-id", "EDGE_BOT_APP_ID"
TOKEN_WITH = {
    "client-id": "${{ inputs.app-client-id || vars.EDGE_BOT_APP_ID }}",
    "private-key": "${{ secrets.app-private-key || secrets.EDGE_BOT_PRIVATE_KEY }}",
    "deprecated": "${{ !inputs.app-client-id && vars.EDGE_BOT_APP_ID && 'EDGE_BOT_APP_ID>app-client-id' || '' }} "
                  "${{ !secrets.app-private-key && secrets.EDGE_BOT_PRIVATE_KEY && 'EDGE_BOT_PRIVATE_KEY>app-private-key' || '' }}",
}

TOKEN_JOBS = {"pr-autoupdate.yml": ["update"], "merge-train.yml": ["train"], "ci-green.yml": ["advance"],
              "pr-heal.yml": ["claim", "push", "report"], "relock.yml": ["push"]}


def token_faults(wf: dict, jobs: list[str]) -> list[str]:
    """Why a reusable's token jobs do not take their token from the one pr-token step ([] = clean)."""
    out = []
    call = (wf.get("on") or wf.get(True))["workflow_call"]
    for secret in (NEW_KEY, OLD_KEY):   # the generic name AND its deprecated alias (v1.40.0)
        if (call["secrets"].get(secret) or {}).get("required") is not False:
            out.append(f"{secret} is not an optional workflow_call secret")
    for name in ("token-source", "app-client-id"):
        if (call["inputs"].get(name) or {}).get("default") != "":
            out.append(f"no {name} input defaulting to ''")
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
        for k, want in TOKEN_WITH.items():   # every alias resolves to the same input, and is noticed
            if " ".join(str(w.get(k, "")).split()) != want:
                out.append(f"{j}: the token step's {k} is not {want}")
        if "app-id" in w:
            out.append(f"{j}: the token step passes the composite's deprecated app-id")
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
    for secret in (OLD_KEY, NEW_KEY):
        wf = _yaml(WF / "ci-green.yml")
        (wf.get("on") or wf.get(True))["workflow_call"]["secrets"].pop(secret)
        assert any(secret in f for f in token_faults(wf, ["advance"])), secret


def test_the_identity_reaches_every_tool_that_trusts_markers() -> None:
    for name, job, step in (("pr-autoupdate.yml", "update", "update"), ("merge-train.yml", "train", None)):
        steps = _yaml(WF / name)["jobs"][job]["steps"]
        s = next(x for x in steps if (x.get("id") == step if step else "ci-merge-train" in json.dumps(x)))
        assert s["env"]["CI_KIT_TOKEN_LOGIN"] == "${{ steps.token.outputs.login }}"
        assert s["env"]["CI_KIT_TRUSTED_LOGINS"] == "${{ steps.token.outputs.trusted }}"


def _all_workflows() -> dict[str, dict]:
    return {p.name: _yaml(p) for p in sorted(WF.glob("*.yml"))}


def pr_token_jobs(workflows: dict[str, dict]) -> dict[str, list[str]]:
    """Every job, in every workflow, with a step that `uses:` the pr-token composite -- discovered,
    never a hand list."""
    out: dict[str, list[str]] = {}
    for name, wf in workflows.items():
        for j, job in (wf.get("jobs") or {}).items():
            if any("/actions/pr-token" in str(s.get("uses", "")) for s in job.get("steps") or []):
                out.setdefault(name, []).append(j)
    return out


def identity_faults(workflows: dict[str, dict]) -> list[str]:
    """A job whose pr-token scope is in IDENTITY_SCOPES must pass the identity to its tools, and
    one outside must not -- the identity counted at workflow, job AND step level ([] = clean)."""
    out = []
    for name, jobs in pr_token_jobs(workflows).items():
        wf = workflows[name]
        for j in jobs:
            job = wf["jobs"][j]
            steps = job.get("steps") or []
            scopes = {(s.get("with") or {}).get("scope") for s in steps if "/actions/pr-token" in str(s.get("uses", ""))}
            envs = [wf.get("env") or {}, job.get("env") or {}, *((s.get("env") or {}) for s in steps)]
            reads = any(k in e for e in envs for k in (pt.LOGIN_ENV, pt.TRUSTED_ENV))
            for sc in scopes:
                if (sc in pt.IDENTITY_SCOPES) != reads:
                    out.append(f"{name}:{j}: scope {sc} {'passes' if reads else 'does not pass'} the identity")
    return out


def test_identity_scopes_are_exactly_the_jobs_that_hand_the_identity_to_a_tool() -> None:
    wfs = _all_workflows()
    assert pr_token_jobs(wfs) == TOKEN_JOBS  # the hand list elsewhere in this file cannot drift either
    assert identity_faults(wfs) == []


def _with_job(wfs: dict[str, dict], name: str, scope: str, where: str) -> dict[str, dict]:
    wfs = {**wfs, name: {"on": {"workflow_call": {}}, "jobs": {"j": {"runs-on": "x", "steps": [
        {"id": "token", "uses": "mjdevaccount/ci-kit/.github/actions/pr-token@v1", "with": {"scope": scope}},
        {"run": "tool", "env": {}}]}}}}
    env = {pt.LOGIN_ENV: "${{ steps.token.outputs.login }}"}
    target = {"workflow": wfs[name], "job": wfs[name]["jobs"]["j"], "step": wfs[name]["jobs"]["j"]["steps"][1]}[where]
    target["env"] = env
    return wfs


@pytest.mark.parametrize("where", ["workflow", "job", "step"])
@pytest.mark.parametrize("scope", ["push", "comment"])
def test_a_new_workflow_passing_the_identity_under_a_non_identity_scope_is_caught_must_fire(
        where: str, scope: str) -> None:
    wfs = _with_job(_all_workflows(), "new-tool.yml", scope, where)
    assert pr_token_jobs(wfs)["new-tool.yml"] == ["j"]
    assert identity_faults(wfs) == [f"new-tool.yml:j: scope {scope} passes the identity"]


def test_identity_mutations_in_todays_workflows_are_caught_must_fire() -> None:
    wfs = _all_workflows()
    wfs["ci-green.yml"]["env"] = {pt.TRUSTED_ENV: "${{ steps.token.outputs.trusted }}"}      # workflow level
    assert "ci-green.yml:advance: scope push passes the identity" in identity_faults(wfs)
    wfs = _all_workflows()
    wfs["pr-heal.yml"]["jobs"]["claim"]["env"] = {pt.LOGIN_ENV: "x"}                           # job level
    assert "pr-heal.yml:claim: scope comment passes the identity" in identity_faults(wfs)
    wfs = _all_workflows()
    for s in wfs["pr-autoupdate.yml"]["jobs"]["update"]["steps"]:                               # identity dropped
        for k in (pt.LOGIN_ENV, pt.TRUSTED_ENV):
            (s.get("env") or {}).pop(k, None)
    assert "pr-autoupdate.yml:update: scope update does not pass the identity" in identity_faults(wfs)


# ── the record line is ONE line of key=value tokens, whatever a field holds ───────────────────────
KEYS = ["source", "mode", "reason", "app", "repo", "workflow", "job", "login"]
HOSTILE = ["a b=c", "x\ny", "app now", "a=b", " \t", "k=v login=evil", "line1\r\nline2", ""]


def _tokens(rec: str) -> list[tuple[str, str]]:
    assert "\n" not in rec and "\r" not in rec
    head, *rest = rec.split(" ")
    assert head == pt.PREFIX
    pairs = [tuple(t.split("=", 1)) for t in rest]
    assert all(len(p) == 2 and p[1] and "=" not in p[1] for p in pairs), rest
    return pairs


@pytest.mark.parametrize("mode", ["a b=c", "app\nnow", "APP"])
def test_a_hostile_mode_stays_one_parseable_line_must_fire(mode: str) -> None:
    d = pt.decide(mode, True, True, True)
    rec = pt.line(d, mode, "absent", "o/r", "w.yml", "j", "")
    assert [k for k, _ in _tokens(rec)] == KEYS
    assert dict(_tokens(rec))["mode"] == "".join("_" if c.isspace() or c == "=" else c for c in mode.strip().lower())


@pytest.mark.parametrize("field", ["mode", "repo", "workflow", "job", "login", "app", "reason", "source"])
@pytest.mark.parametrize("bad", HOSTILE)
def test_every_field_survives_hostile_input_must_fire(field: str, bad: str) -> None:
    args = {"mode": "pat", "app": "absent", "repo": "o/r", "workflow": "w.yml", "job": "j", "login": PAT_USER}
    d = pt.Decision(False, "pat", "mode-pat")
    if field in ("reason", "source"):
        d = pt.Decision(False, bad if field == "source" else "pat", bad if field == "reason" else "mode-pat")
    else:
        args[field] = bad
    assert [k for k, _ in _tokens(pt.line(d, **args))] == KEYS


@pytest.mark.skipif(BASH is None, reason="no bash on this host")
@pytest.mark.parametrize("mode", [" App Now ", "a b=c", "APP-REQUIRED", ""])
def test_the_shell_fallback_spells_mode_as_line_does(tmp_path: pathlib.Path, mode: str) -> None:
    _, said, _ = _run_decide_shell(tmp_path, "exit 1", mode=mode)
    shell = dict(_tokens(said.split("::error title=pr-token::", 1)[1].strip()))["mode"]
    assert shell == dict(_tokens(pt.line(pt.Decision(False, "none", "decide-crashed"), mode, "", "", "", "", "")))["mode"]


# ── the REAL GET /user read (_pat_login), through a fake `gh` on PATH ─────────────────────────────
def _fake_gh(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, rc: int) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if sys.platform == "win32":
        (bin_dir / "gh.cmd").write_text(f"@exit /b {rc}", encoding="utf-8")
    else:
        gh = bin_dir / "gh"
        gh.write_text(f"#!/bin/sh\nexit {rc}\n", encoding="utf-8")
        gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))


def _report_real(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, scope: str) -> tuple[int, dict[str, str], str]:
    out, summ = tmp_path / "out", tmp_path / "summary"
    for k, v in {"GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summ), "GITHUB_REPOSITORY": "o/r",
                 "GITHUB_JOB": "j", "CI_KIT_TOKEN_MODE": "", "CI_KIT_TOKEN_SCOPE": scope, "MINT": "false",
                 "SOURCE": "pat", "REASON": "mode-pat", "MINT_OUTCOME": "", "APP_SLUG": "",
                 "CI_KIT_PAT": "not-a-real-token"}.items():
        monkeypatch.setenv(k, v)
    rc = pt.main(["report"])
    outs = dict(x.split("=", 1) for x in out.read_text(encoding="utf-8").splitlines()) if out.exists() else {}
    return rc, outs, summ.read_text(encoding="utf-8") if summ.exists() else ""


@pytest.mark.parametrize("gh_rc", [1, 0], ids=["gh-exits-nonzero", "gh-prints-nothing"])
def test_unreadable_pat_identity_is_red_where_a_tool_trusts_markers_must_fire(
        monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], gh_rc: int) -> None:
    _fake_gh(tmp_path, monkeypatch, gh_rc)
    rc, outs, summary = _report_real(monkeypatch, tmp_path, "update")
    assert rc == 1 and outs == {} and "reason=identity-unreadable" in summary
    assert "::error title=pr-token::" in capsys.readouterr().out


@pytest.mark.parametrize("gh_rc", [1, 0], ids=["gh-exits-nonzero", "gh-prints-nothing"])
@pytest.mark.parametrize("scope", ["push", "comment"])
def test_unreadable_pat_identity_is_a_warning_where_nothing_trusts_markers_must_fire(
        monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str],
        gh_rc: int, scope: str) -> None:
    _fake_gh(tmp_path, monkeypatch, gh_rc)
    rc, outs, summary = _report_real(monkeypatch, tmp_path, scope)
    said = capsys.readouterr().out
    assert rc == 0 and outs == {"login": "", "trusted": ""}
    assert "source=pat mode=pat reason=mode-pat app=absent" in summary and summary.rstrip().endswith("login=unknown`")
    assert "::warning title=pr-token::" in said and "::error" not in said


# ── the "one identity" notice only where a second one was expected ───────────────────────────────
@pytest.mark.parametrize(("env", "pat_login", "noticed"), [
    ({"CI_KIT_TOKEN_MODE": "", "SOURCE": "pat", "REASON": "mode-pat", "MINT": "false"}, PAT_USER, False),  # no key: normal
    ({"CI_KIT_TOKEN_MODE": "", "SOURCE": "pat", "REASON": "mode-pat", "MINT": "true",
      "MINT_OUTCOME": "failure"}, PAT_USER, True),                                                      # canary failed
    ({"CI_KIT_TOKEN_MODE": "app", "SOURCE": "pat", "REASON": "key-absent", "MINT": "false"}, PAT_USER, True),
    ({"CI_KIT_TOKEN_MODE": "app", "SOURCE": "app", "REASON": "configured", "MINT": "true",
      "MINT_OUTCOME": "success", "APP_SLUG": "example-app"}, "", True),                               # no PAT login
])
def test_the_one_identity_notice_fires_only_where_two_were_expected(
        monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str],
        env: dict[str, str], pat_login: str, noticed: bool) -> None:
    rc, _, _ = _cli(monkeypatch, tmp_path, "report", pat_login, CI_KIT_TOKEN_SCOPE="update", **env)
    assert rc == 0 and ("trusted identities:" in capsys.readouterr().out) == noticed


# ── the record's grammar is a contract ───────────────────────────────────────────────────────────
def _emitted_reasons() -> set[str]:
    """Every `reason` the code can put in the record: decide()'s table, the literals given to
    Decision(...) in pr_token.py, and the composite shell's own crash line."""
    got = {pt.decide(m, a, k, p).reason for m in (*pt.MODES, "bogus") for a in (True, False)
           for k in (True, False) for p in (True, False)}
    tree = ast.parse((ROOT / "ci_kit" / "pr_token.py").read_text(encoding="utf-8"))
    def values(n: ast.expr) -> set[str]:  # a literal, or either branch of `x if cond else y` -- never a cond
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            return {n.value}
        return values(n.body) | values(n.orelse) if isinstance(n, ast.IfExp) else set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Decision" and len(node.args) >= 3:
            got |= values(node.args[2])
    got |= set(re.findall(r"reason=([A-Za-z0-9_-]+)", ACTION.read_text(encoding="utf-8")))
    return got - {""}


def test_every_reason_the_code_emits_is_listed_and_parseable_must_fire() -> None:
    emitted = _emitted_reasons()
    assert {"decide-crashed", "scope-invalid", "mint-failed", "identity-unreadable"} <= emitted  # the scan sees them
    assert emitted <= set(pt.REASONS)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    listed = re.search(r"reason=<([^>]+)>", readme.split("THE RECORD:", 1)[1]).group(1).split("|")
    assert set(listed) == set(pt.REASONS)
    assert all("=" not in r and " " not in r for r in pt.REASONS)


# ── the decide step's shell: a decide that cannot run still writes its record ─────────────────────


def _run_decide_shell(tmp_path: pathlib.Path, fake_python: str, mode: str = "app") -> tuple[int, str, str]:
    bin_dir = tmp_path / "pybin"
    bin_dir.mkdir()
    py = bin_dir / "python3"
    py.write_text(f"#!/bin/sh\n{fake_python}\n", encoding="utf-8", newline="\n")
    py.chmod(0o755)
    script = tmp_path / "decide.sh"
    run = next(s for s in _yaml(ACTION)["runs"]["steps"] if s["id"] == "decide")["run"]
    script.write_text(run, encoding="utf-8", newline="\n")
    out, summ = tmp_path / "out", tmp_path / "summary"
    out.write_text("", encoding="utf-8")
    env = {**os.environ, "GITHUB_OUTPUT": out.as_posix(), "GITHUB_STEP_SUMMARY": summ.as_posix(),
           "GITHUB_REPOSITORY": "o/r", "GITHUB_JOB": "update", "CI_KIT_TOKEN_MODE": mode,
           "GITHUB_WORKFLOW_REF": "o/r/.github/workflows/pr-autoupdate.yml@refs/heads/main",
           "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "")}  # bash converts a Windows PATH
    r = subprocess.run([BASH, "--noprofile", "--norc", "-eo", "pipefail", "-c", f'. "{script.as_posix()}"'],
                       capture_output=True, text=True, env=env, check=False)
    return r.returncode, r.stdout, summ.read_text(encoding="utf-8") if summ.exists() else ""


@pytest.mark.skipif(BASH is None, reason="no bash on this host")
def test_a_decide_that_cannot_run_still_writes_a_crash_record_must_fire(tmp_path: pathlib.Path) -> None:
    rc, said, summary = _run_decide_shell(tmp_path, "echo 'No module named ci_kit' >&2; exit 1")
    line = ("pr-token: source=none mode=app reason=decide-crashed app=absent repo=o/r "
            "workflow=pr-autoupdate.yml job=update login=unknown")
    assert rc == 1 and f"::error title=pr-token::{line}" in said and line in summary


@pytest.mark.skipif(BASH is None, reason="no bash on this host")
@pytest.mark.parametrize("fake", ['echo source=none >> "$GITHUB_OUTPUT"; exit 1', "exit 0"],
                         ids=["ran-and-refused", "ran-ok"])
def test_a_decide_that_ran_keeps_its_own_outcome(tmp_path: pathlib.Path, fake: str) -> None:
    rc, said, _ = _run_decide_shell(tmp_path, fake)
    assert rc == (1 if "exit 1" in fake else 0) and "decide-crashed" not in said
