"""The generic names (v1.40.0) resolve to EXACTLY what the deprecated ones did -- must fire, both ways.

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
import pathlib
import re

import _runners
import pytest
import yaml

from ci_kit import pr_admit
from ci_kit import pr_token as pt

ROOT = pathlib.Path(__file__).resolve().parent.parent
ACTION = ROOT / ".github" / "actions" / "pr-token" / "action.yml"
WF = ROOT / ".github" / "workflows"
TOKEN_JOBS = {"pr-autoupdate.yml": ["update"], "merge-train.yml": ["train"], "ci-green.yml": ["advance"],
              "pr-heal.yml": ["claim", "push", "report"], "relock.yml": ["push"]}
ID, KEY = "Iv1.example", "-----KEY-----"


def gh(expr: str, ctx: dict[str, str]) -> object:
    """The expression subset these files use, evaluated: `&&`/`||` return an OPERAND (Python's
    and/or), '' is falsy, `!` is not, a context name absent from `ctx` is ''."""
    body = expr.strip().removeprefix("${{").removesuffix("}}")
    body = re.sub(r"\b((?:inputs|vars|secrets)\.[\w-]+)", lambda m: f"_c({m.group(1)!r})", body)
    body = body.replace("fromJSON(", "_j(").replace("&&", " and ").replace("||", " or ")
    body = re.sub(r"!(?!=)", " not ", body)
    return eval(body, {"__builtins__": {}}, {"_c": lambda k: ctx.get(k, ""), "_j": json.loads})  # noqa: S307


def _yaml(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _gh_str(v: object) -> str:
    """How GitHub hands an expression's value to an input or env var."""
    return str(v).lower() if isinstance(v, bool) else str(v)


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


def test_a_broken_precedence_is_caught_must_fire() -> None:
    # the deprecated spelling placed FIRST in the mint: with both set the old one wins -- the
    # evaluation above would see it (and the notice flag, unchanged, would no longer match)
    action = _yaml(ACTION)
    mint = next(s for s in action["runs"]["steps"] if s.get("id") == "mint")
    assert mint["with"]["private-key"] == "${{ inputs.private-key || inputs.deprecated-private-key }}"
    mint["with"]["private-key"] = "${{ inputs.deprecated-private-key || inputs.private-key }}"
    assert composite({"private-key": KEY, "deprecated-private-key": "old-key"}, action)["private-key"] == "old-key"
    assert composite({"private-key": KEY, "deprecated-private-key": "old-key"})["private-key"] == KEY


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


@pytest.mark.parametrize(("raw", "want"), [
    ("", ""), ("home-sealed", "home-sealed"), ('["self-hosted","home-sealed"]', "home-sealed"),
    ('["sealed"]', "sealed"), ("[]", ""), ("[not json", ""), (" sealed ", "sealed"),
])
def test_pool_label(raw: str, want: str) -> None:
    assert pr_admit.pool_label(raw) == want
