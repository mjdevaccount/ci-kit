"""The pr-token composite's INPUT NAMES are a contract -- must fire.

Callers pass the composite's inputs BY NAME, from every ref they pin: the reusables at any `v1`
commit, and any workflow that calls the composite directly. A dropped or renamed input is not an
error on GitHub -- the value silently never arrives (the App id or key reads empty, and the run
falls back or goes red far from the cause). A newly REQUIRED input breaks every caller that does not
send it. So the declared set is pinned here, name by name, with its `required`/`default`: a rename,
a drop or a tightening is a red in this repo's CI, not a silent break in a caller's.
"""
from __future__ import annotations

import pytest
from test_pr_token_aliases_must_fire import OLD_CALL
from test_pr_token_must_fire import ACTION, TOKEN_JOBS, WF, _yaml

# name -> required. Every optional input defaults to '' (an absent value must read as empty).
# Removing a name is a MAJOR version; adding one is a minor (add it here in the same change).
PINNED = {
    "scope": True,
    "mode": False,
    "client-id": False,
    "app-id": False,                 # deprecated alias of client-id (v1.40.0)
    "private-key": False,
    "deprecated-client-id": False,   # a reusable's older id spelling (v1.40.0)
    "deprecated-private-key": False,  # a reusable's older key spelling (v1.40.0)
    "pat": False,
}


def input_faults(action: dict) -> list[str]:
    """How the composite's `inputs:` differ from PINNED ([] = the contract holds)."""
    declared = action.get("inputs") or {}
    out = []
    for name, required in PINNED.items():
        spec = declared.get(name)
        if spec is None:
            out.append(f"`{name}` is not declared: a caller passing it is silently dropped")
        elif bool(spec.get("required", False)) is not required:
            out.append(f"`{name}` required={spec.get('required')!r}, pinned {required}")
        elif not required and spec.get("default") != "":
            out.append(f"`{name}` default={spec.get('default')!r}, pinned ''")
    out += [f"`{name}` is declared but not pinned here" for name in sorted(set(declared) - set(PINNED))]
    return out


def sent_names() -> set[str]:
    """Every input name a caller sends: each reusable's token step today, and the v1.39.0 shape that
    older refs (and direct callers written then) still send."""
    names = set(OLD_CALL) | {"scope"}
    for wf, jobs in TOKEN_JOBS.items():
        for j in jobs:
            tok = next(s for s in _yaml(WF / wf)["jobs"][j]["steps"] if s.get("id") == "token")
            names |= set(tok.get("with") or {})
    return names


def test_the_composite_declares_exactly_the_pinned_inputs() -> None:
    assert input_faults(_yaml(ACTION)) == []


def test_every_name_a_caller_sends_is_declared() -> None:
    assert sent_names() - set(_yaml(ACTION)["inputs"]) == set()
    assert {"client-id", "private-key", "deprecated-client-id", "deprecated-private-key", "app-id"} <= sent_names()


@pytest.mark.parametrize(("mutate", "fault"), [
    (lambda i: i.pop("app-id"), "`app-id` is not declared"),
    (lambda i: i.pop("deprecated-private-key"), "`deprecated-private-key` is not declared"),
    (lambda i: i.__setitem__("client-key", i.pop("private-key")), "`private-key` is not declared"),
    (lambda i: i["client-id"].__setitem__("required", True), "`client-id` required=True"),
    (lambda i: i["scope"].__setitem__("required", False), "`scope` required=False"),
    (lambda i: i["pat"].__setitem__("default", "x"), "`pat` default='x'"),
    (lambda i: i.__setitem__("new-thing", {"required": False, "default": ""}), "`new-thing` is declared but not pinned"),
])
def test_a_dropped_renamed_or_tightened_input_is_caught_must_fire(mutate, fault: str) -> None:
    action = _yaml(ACTION)
    mutate(action["inputs"])
    got = input_faults(action)
    assert any(f.startswith(fault) for f in got), got
