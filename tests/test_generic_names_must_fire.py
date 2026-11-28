"""The generic names (v1.42.0) resolve to EXACTLY what the deprecated ones did -- must fire, both ways.

`v1` floats: every consumer picks this up unasked. A caller that passes only the OLD names
(`EDGE_BOT_APP_ID` variable, `EDGE_BOT_PRIVATE_KEY` secret, the CI_SECURE_RUNNER / CI_SEALED_RUNNER
switches) must get the same App id, key, decision and pool it got before; a caller using the NEW
names (`app-client-id` input, `app-private-key` secret, `secure-runner` / `sealed-runner`) the same
values; and with both set, the new name wins. Each reusable's token step is evaluated, then the
pr-token composite's resolution over what it passed -- the whole chain a run takes, minus GitHub.
The fixture callers (tests/fixtures/callers/) are what actionlint checks against these reusables.
"""
from __future__ import annotations

import json

import _runners
import pytest
from test_pr_token_aliases_must_fire import ID, KEY, gh
from test_pr_token_aliases_must_fire import _s as _gh_str
from test_pr_token_must_fire import ACTION, TOKEN_JOBS, WF, _yaml

from ci_kit import pr_admit
from ci_kit import pr_token as pt


def composite(inputs: dict[str, str], action: dict | None = None) -> dict[str, object]:
    """What the pr-token composite does with these inputs: the mint's client id and key, the decide
    step's flags, its decision, and the deprecation notices it prints."""
    action = action or _yaml(ACTION)
    ctx = {f"inputs.{k}": v for k, v in inputs.items()}
    steps = {s["id"]: s for s in action["runs"]["steps"]}
    env = {k: _gh_str(gh(v, ctx)) for k, v in steps["decide"]["env"].items()
           if k in ("HAS_APP_ID", "HAS_KEY", *pt.DEPRECATED)}
    on = [f for f in pt.DEPRECATED if env[f] == "true"]
    d = pt.decide(inputs.get("mode", ""), env["HAS_APP_ID"] == "true", env["HAS_KEY"] == "true",
                  bool(inputs.get("pat")))
    return {"client-id": gh(steps["mint"]["with"]["client-id"], ctx),
            "private-key": gh(steps["mint"]["with"]["private-key"], ctx),
            "has": (env["HAS_APP_ID"], env["HAS_KEY"]), "decision": d, "notices": pt.deprecations(on)}


def through(wf: str, job: str, caller: dict[str, str]) -> dict[str, object]:
    """A caller's context (`inputs.*` / `vars.*` / `secrets.*`) through a reusable's token step and
    the composite."""
    tok = next(s for s in _yaml(WF / wf)["jobs"][job]["steps"] if s.get("id") == "token")
    return composite({k: _gh_str(gh(str(v), caller)) if "${{" in str(v) else str(v) for k, v in tok["with"].items()})


OLD = {"vars.EDGE_BOT_APP_ID": ID, "secrets.EDGE_BOT_PRIVATE_KEY": KEY, "secrets.token": "PAT"}
NEW = {"inputs.app-client-id": ID, "secrets.app-private-key": KEY, "secrets.token": "PAT"}
CASES = [(wf, j) for wf, jobs in TOKEN_JOBS.items() for j in jobs]


@pytest.mark.parametrize(("wf", "job"), CASES)
@pytest.mark.parametrize("mode", ["", "app", "app-required"])
def test_old_and_new_names_resolve_to_identical_values(wf: str, job: str, mode: str) -> None:
    old = through(wf, job, {**OLD, "inputs.token-source": mode})
    new = through(wf, job, {**NEW, "inputs.token-source": mode})
    assert (old["client-id"], old["private-key"]) == (new["client-id"], new["private-key"]) == (ID, KEY)
    assert old["has"] == new["has"] == ("true", "true")
    assert old["decision"] == new["decision"]
    # the only difference: the old spelling is named, once per alias, with its replacement
    assert new["notices"] == []
    assert [n.split("::")[2].split(" -- ")[0] for n in old["notices"]] == [
        "`EDGE_BOT_APP_ID` is deprecated: use `app-client-id`",
        "`EDGE_BOT_PRIVATE_KEY` is deprecated: use `app-private-key`"]


@pytest.mark.parametrize(("wf", "job"), CASES)
def test_the_new_name_wins_when_both_are_set(wf: str, job: str) -> None:
    got = through(wf, job, {"vars.EDGE_BOT_APP_ID": "old-id", "secrets.EDGE_BOT_PRIVATE_KEY": "old-key",
                            "inputs.app-client-id": ID, "secrets.app-private-key": KEY})
    assert (got["client-id"], got["private-key"], got["notices"]) == (ID, KEY, [])
    # one of each: the generic one present wins, the other falls through to its alias (and is named)
    got = through(wf, job, {"vars.EDGE_BOT_APP_ID": "old-id", "secrets.EDGE_BOT_PRIVATE_KEY": "old-key",
                            "inputs.app-client-id": ID})
    assert (got["client-id"], got["private-key"]) == (ID, "old-key") and len(got["notices"]) == 1


@pytest.mark.parametrize(("wf", "job"), CASES)
def test_a_caller_that_sets_nothing_is_still_the_pat_with_no_app(wf: str, job: str) -> None:
    got = through(wf, job, {"secrets.token": "PAT"})
    assert (got["client-id"], got["private-key"], got["has"], got["notices"]) == ("", "", ("false", "false"), [])
    assert got["decision"] == pt.decide("", False, False, True)


def test_the_composite_own_app_id_alias_still_resolves_must_fire() -> None:
    # a direct caller of the composite written before v1.40.0 passes `app-id`
    old = composite({"scope": "push", "app-id": ID, "private-key": KEY, "pat": "PAT"})
    new = composite({"scope": "push", "client-id": ID, "private-key": KEY, "pat": "PAT"})
    assert (old["client-id"], old["has"], old["decision"]) == (new["client-id"], new["has"], new["decision"])
    assert len(old["notices"]) == 1 and "`app-id` is deprecated: use `client-id`" in old["notices"][0]


# ── the pools: no input = the v1.39 expression's value, exactly; an input wins ─────────────────────
@pytest.mark.parametrize(("new", "old", "inp", "var"), [
    (_runners.SECURE, _runners.OLD_SECURE, "inputs.secure-runner", "vars.CI_SECURE_RUNNER"),
    (_runners.SEALED, _runners.OLD_SEALED, "inputs.sealed-runner", "vars.CI_SEALED_RUNNER"),
    (_runners.SECURE_ENV, _runners.OLD_SECURE_ENV, "inputs.secure-runner", "vars.CI_SECURE_RUNNER"),
])
def test_a_pool_with_no_input_is_what_it_was(new: str, old: str, inp: str, var: str) -> None:
    for switch in ("", "1", "on"):
        assert gh(new, {var: switch}) == gh(old, {var: switch}), (new, switch)
    labels = '["self-hosted","mine"]'
    for switch in ("", "1"):
        got = gh(new, {inp: labels, var: switch})
        assert got == (json.loads(labels) if "fromJSON" in new else "ci-secure"), (new, switch)


# Where the pools are wired. Both maps are the WHOLE set: a job that gains or loses `environment:`,
# or a reusable that gains or loses a runner input, is a red here until the map says so.
ENV_JOBS = {"ci-green.yml": ["advance"], "merge-train.yml": ["train"], "pr-autoupdate.yml": ["update"],
            "pr-heal.yml": ["claim", "push", "report"]}
RUNNER_INPUTS = {"ci-green.yml": ["secure-runner"], "merge-train.yml": ["secure-runner"],
                 "pr-autoupdate.yml": ["secure-runner"], "relock.yml": ["secure-runner"],
                 "pr-heal.yml": ["secure-runner", "sealed-runner"],
                 "review-gate.yml": ["secure-runner", "sealed-runner"],
                 "app-audit.yml": ["secure-runner"]}


def _call_inputs(wf: dict) -> dict:
    on = wf.get("on", wf.get(True)) or {}   # YAML 1.1 reads a bare `on` key as True
    return (on.get("workflow_call") or {}).get("inputs") or {}


def test_every_environment_is_the_one_secure_pool_expression() -> None:
    seen = {}
    for path in sorted(WF.glob("*.yml")):
        for name, job in (_yaml(path).get("jobs") or {}).items():
            if "environment" in job:
                seen.setdefault(path.name, []).append(name)
                assert job["environment"] == _runners.SECURE_ENV, (path.name, name, job["environment"])
    assert seen == ENV_JOBS


def test_every_runner_input_defaults_to_empty_so_the_switch_still_decides() -> None:
    # '' is what makes "no input" fall through to CI_SECURE_RUNNER / CI_SEALED_RUNNER; any other
    # default ('[]' included) takes the pool away from every caller that sets nothing
    seen = {}
    for path in sorted(WF.glob("*.yml")):
        for name, spec in _call_inputs(_yaml(path)).items():
            if name in ("secure-runner", "sealed-runner"):
                seen.setdefault(path.name, []).append(name)
                assert (spec.get("type"), spec.get("default"), spec.get("required", False)) == \
                    ("string", "", False), (path.name, name, spec)
    assert seen == RUNNER_INPUTS


def test_review_gate_hands_the_scheduler_the_same_pool() -> None:
    # v1.39: POOL = vars.CI_SEALED_RUNNER && 'home-sealed' || ''; pr_admit now also reads a JSON list
    steps = [s for j in _yaml(WF / "review-gate.yml")["jobs"].values() for s in j.get("steps") or []
             if s.get("id") == "admit"]
    assert len(steps) == 2
    for s in steps:
        pool = s["env"]["POOL"]
        for switch in ("", "1"):
            assert gh(pool, {"vars.CI_SEALED_RUNNER": switch}) == (switch and "home-sealed")
        got = gh(pool, {"inputs.sealed-runner": '["self-hosted","sealed"]', "vars.CI_SEALED_RUNNER": "1"})
        assert pr_admit.pool_label(str(got)) == "sealed"
        assert pr_admit.pool_label(str(gh(pool, {"vars.CI_SEALED_RUNNER": "1"}))) == "home-sealed"
