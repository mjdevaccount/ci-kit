"""The pr-token composite's generic names and deprecated aliases (v1.40.0) -- must fire, both ways.

`v1` floats: once it carries this composite, every workflow at every ref still calls it the
v1.39.0 way (`app-id` + `private-key`). That call must get the SAME App id and key, the SAME
decision and the SAME `pr-token:` record line as the v1.39.0 composite gave it; the only addition
is a `::notice` under a title that is NOT the record's. The reusables' switch to the generic names
(v1.42.0) is tested in tests/test_generic_names_must_fire.py, over these helpers.
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest
from test_pr_token_must_fire import ACTION, _cli, _yaml

from ci_kit import pr_token as pt

ID, KEY = "Iv1.example", "-----KEY-----"
# THE v1.39.0 CALL SHAPE, verbatim from every token step of the reusables on main at v1.39.0
# (pr-autoupdate, merge-train, ci-green, pr-heal x3, relock; only `scope` differs per job).
OLD_CALL = {"mode": "${{ inputs.token-source || vars.PR_TOKEN_SOURCE }}", "app-id": "${{ vars.EDGE_BOT_APP_ID }}",
            "private-key": "${{ secrets.EDGE_BOT_PRIVATE_KEY }}", "pat": "${{ secrets.token }}"}
# THE v1.39.0 COMPOSITE's resolution, verbatim from its action.yml (decide env + mint `with`)
OLD_ACTION = {"HAS_APP_ID": "${{ inputs.app-id != '' }}", "HAS_KEY": "${{ inputs.private-key != '' }}",
              "client-id": "${{ inputs.app-id }}", "private-key": "${{ inputs.private-key }}"}


def gh(expr: str, ctx: dict[str, str]) -> object:
    """The expression subset these files use, evaluated: `&&`/`||` return an OPERAND (Python's
    and/or), '' is falsy, `!` is not, `fromJSON` parses, a context name absent from `ctx` is ''."""
    body = expr.strip().removeprefix("${{").removesuffix("}}")
    body = re.sub(r"\b((?:inputs|vars|secrets)\.[\w-]+)", lambda m: f"_c({m.group(1)!r})", body)
    body = body.replace("fromJSON(", "_j(").replace("&&", " and ").replace("||", " or ")
    body = re.sub(r"!(?!=)", " not ", body)
    return eval(body, {"__builtins__": {}}, {"_c": lambda k: ctx.get(k, ""), "_j": json.loads})  # noqa: S307


def _s(v: object) -> str:
    """How GitHub hands an expression's value to an input or env var."""
    return str(v).lower() if isinstance(v, bool) else str(v)


def new_composite(inputs: dict[str, str], action: dict | None = None) -> dict[str, str]:
    """This composite over `inputs`: the mint's id and key, and every flag the decide step reads."""
    steps = {s["id"]: s for s in (action or _yaml(ACTION))["runs"]["steps"]}
    ctx = {f"inputs.{k}": v for k, v in inputs.items()}
    env = {k: _s(gh(v, ctx)) for k, v in steps["decide"]["env"].items() if k in ("HAS_APP_ID", "HAS_KEY", *pt.DEPRECATED)}
    return {**env, "client-id": _s(gh(steps["mint"]["with"]["client-id"], ctx)),
            "private-key": _s(gh(steps["mint"]["with"]["private-key"], ctx))}


def old_composite(inputs: dict[str, str]) -> dict[str, str]:
    ctx = {f"inputs.{k}": v for k, v in inputs.items()}
    return {k: _s(gh(v, ctx)) for k, v in OLD_ACTION.items()}


def old_caller(mode: str, key: str) -> dict[str, str]:
    """What a v1.39.0 reusable hands the composite, for a caller repo with these values."""
    caller = {"inputs.token-source": mode, "vars.EDGE_BOT_APP_ID": ID, "secrets.EDGE_BOT_PRIVATE_KEY": key,
              "secrets.token": "PAT"}
    return {"scope": "update", **{k: _s(gh(v, caller)) for k, v in OLD_CALL.items()}}


def run_steps(monkeypatch, tmp_path, capsys, mode: str, flags: dict[str, str]) -> tuple[dict, str, list[str]]:
    """decide then report, as the composite runs them: (decide outputs, the record, stdout lines)."""
    rc, outs, _ = _cli(monkeypatch, tmp_path, "decide", CI_KIT_TOKEN_MODE=mode, CI_KIT_TOKEN_SCOPE="update",
                       HAS_PAT="true", **flags)
    lines = capsys.readouterr().out.splitlines()
    (tmp_path / "out").unlink(missing_ok=True)
    if rc == 0:
        _cli(monkeypatch, tmp_path, "report", CI_KIT_TOKEN_MODE=mode, CI_KIT_TOKEN_SCOPE="update", MINT=outs["mint"],
             SOURCE=outs["source"], REASON=outs["reason"], APP_SLUG="example-app",
             MINT_OUTCOME="success" if outs["mint"] == "true" else "skipped")
        lines += capsys.readouterr().out.splitlines()
        (tmp_path / "out").unlink(missing_ok=True)
    summary = (tmp_path / "summary").read_text(encoding="utf-8")
    (tmp_path / "summary").unlink()
    return outs, summary, lines


@pytest.mark.parametrize("key", [KEY, ""], ids=["key", "no-key"])
@pytest.mark.parametrize("mode", ["", "pat", "app", "app-required"])
def test_a_v1_39_shape_caller_gets_the_same_decision_and_record_must_fire(
        monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str],
        mode: str, key: str) -> None:
    inputs = old_caller(mode, key)
    old, new = old_composite(inputs), new_composite(inputs)
    assert {k: new[k] for k in old} == old            # same id, same key, same HAS_ flags
    assert (new["client-id"], new["private-key"]) == (ID, key)
    old_outs, old_rec, old_lines = run_steps(monkeypatch, tmp_path, capsys, mode, {k: old[k] for k in ("HAS_APP_ID", "HAS_KEY")})
    new_outs, new_rec, new_lines = run_steps(monkeypatch, tmp_path, capsys, mode,
                                             {k: new[k] for k in ("HAS_APP_ID", "HAS_KEY", *pt.DEPRECATED)})
    assert new_outs == old_outs and new_rec == old_rec and "pr-token: " in new_rec
    # the ONLY difference on stdout: one notice for the composite's own `app-id` alias, not the record's title
    extra = [x for x in new_lines if x not in old_lines]
    assert [x for x in old_lines if x not in new_lines] == []
    assert extra == pt.deprecations(["APP_ID_ALIAS"])


def test_the_generic_name_and_its_alias_resolve_identically_must_fire() -> None:
    old = new_composite({"scope": "push", "app-id": ID, "private-key": KEY, "pat": "PAT"})
    new = new_composite({"scope": "push", "client-id": ID, "private-key": KEY, "pat": "PAT"})
    for k in ("client-id", "private-key", "HAS_APP_ID", "HAS_KEY"):
        assert old[k] == new[k]
    assert [f for f in pt.DEPRECATED if old[f] == "true"] == ["APP_ID_ALIAS"]
    assert [f for f in pt.DEPRECATED if new[f] == "true"] == []
    dep = new_composite({"deprecated-client-id": ID, "deprecated-private-key": KEY})
    assert (dep["client-id"], dep["private-key"], dep["HAS_APP_ID"], dep["HAS_KEY"]) == (ID, KEY, "true", "true")
    assert [f for f in pt.DEPRECATED if dep[f] == "true"] == ["DEPRECATED_CLIENT_ID", "DEPRECATED_PRIVATE_KEY"]


def test_the_generic_name_wins_and_each_flag_matches_the_mint_chain_must_fire() -> None:
    both = new_composite({"client-id": ID, "app-id": "a", "deprecated-client-id": "d",
                          "private-key": KEY, "deprecated-private-key": "k"})
    assert (both["client-id"], both["private-key"]) == (ID, KEY)
    assert [f for f in pt.DEPRECATED if both[f] == "true"] == []
    mid = new_composite({"app-id": ID, "deprecated-client-id": "d"})
    assert mid["client-id"] == ID and [f for f in pt.DEPRECATED if mid[f] == "true"] == ["APP_ID_ALIAS"]


def test_a_broken_precedence_is_caught_must_fire() -> None:
    action = _yaml(ACTION)
    mint = next(s for s in action["runs"]["steps"] if s.get("id") == "mint")
    assert mint["with"]["private-key"] == "${{ inputs.private-key || inputs.deprecated-private-key }}"
    mint["with"]["private-key"] = "${{ inputs.deprecated-private-key || inputs.private-key }}"
    assert new_composite({"private-key": KEY, "deprecated-private-key": "old"}, action)["private-key"] == "old"
    assert new_composite({"private-key": KEY, "deprecated-private-key": "old"})["private-key"] == KEY


def test_cmd_decide_prints_a_notice_for_an_alias_flag_must_fire(
        monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, outs, _ = _cli(monkeypatch, tmp_path, "decide", CI_KIT_TOKEN_MODE="app", CI_KIT_TOKEN_SCOPE="update",
                       HAS_APP_ID="true", HAS_KEY="true", HAS_PAT="true", APP_ID_ALIAS="true", DEPRECATED_PRIVATE_KEY="true")
    out = capsys.readouterr().out.splitlines()
    assert rc == 0 and outs["source"] == "app"
    assert out == pt.deprecations(["APP_ID_ALIAS", "DEPRECATED_PRIVATE_KEY"])
    assert "`app-id` is deprecated: use `client-id`" in out[0]
    rc, _, _ = _cli(monkeypatch, tmp_path, "decide", CI_KIT_TOKEN_MODE="app", CI_KIT_TOKEN_SCOPE="update",
                    HAS_APP_ID="true", HAS_KEY="true", HAS_PAT="true")
    assert rc == 0 and capsys.readouterr().out == ""     # no alias, no notice


def test_the_notice_title_is_not_the_record_title_must_fire() -> None:
    # a detector greps `title=pr-token::` / `pr-token: ` for the record: a notice must never match
    assert pt.DEPRECATED_TITLE != "pr-token"
    for n in pt.deprecations(list(pt.DEPRECATED)):
        assert n.startswith(f"::notice title={pt.DEPRECATED_TITLE}::")
        assert "title=pr-token::" not in n and pt.PREFIX not in n
    assert pt.deprecations(["NOT_A_FLAG"])[0].endswith(" -- identical behaviour today; the old name is "
                                                       "removed in the next major version")
    assert pt.deprecations(["APP_ID_ALIAS", "APP_ID_ALIAS"]) == pt.deprecations(["APP_ID_ALIAS"])


def test_the_composite_declares_every_alias_flag() -> None:
    env = {s["id"]: s for s in _yaml(ACTION)["runs"]["steps"]}["decide"]["env"]
    assert set(pt.DEPRECATED) <= set(env)
    # every flag is a comparison (true/false), never a value a shell could print
    assert all("!=" in env[f] or "==" in env[f] for f in pt.DEPRECATED), json.dumps(env)
