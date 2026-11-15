"""The reusable declared-literal workflow asserts the gate SCANNED (.github/workflows/declared-literal.yml).

An exit 0 is not proof the tree was read: the gate step must leave a receipt, a stale one is
removed first, and the step after the gate refuses a missing receipt or one that read nothing --
before, and independent of, the selftest step. Both directions: the real file passes, and each
way of removing or weakening the check is named.
"""
from __future__ import annotations

import copy
import pathlib

import pytest
import yaml

from ci_kit import declared_literal as dl

ROOT = pathlib.Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows" / "declared-literal.yml"
RECEIPT = "${{ runner.temp }}/declared-literal.receipt"
ASSERT = "grep -Eq '^read=[1-9][0-9]* rules=[1-9]' \"$RECEIPT\""


def _steps() -> list[dict]:
    return yaml.safe_load(WF.read_text(encoding="utf-8"))["jobs"]["lint"]["steps"]


def _tool(step: dict) -> tuple[str, str]:
    w = step.get("with") or {}
    return (str(w.get("tool", "")), str(w.get("args", ""))) if "ci-kit" in str(step.get("uses", "")) else ("", "")


def faults(steps: list[dict]) -> list[str]:
    gates = [i for i, s in enumerate(steps) if _tool(s) == ("ci-declared-literal", "${{ inputs.args }}")]
    if len(gates) != 1:
        return [f"want exactly one gate step on inputs.args, found {len(gates)}"]
    g = gates[0]
    out = []
    if (steps[g].get("env") or {}).get(dl.RECEIPT_ENV) != RECEIPT:
        out.append(f"gate step does not set {dl.RECEIPT_ENV} to {RECEIPT}")
    before = steps[g - 1] if g else {}
    if (before.get("env") or {}).get("RECEIPT") != RECEIPT or 'rm -f "$RECEIPT"' not in str(before.get("run", "")):
        out.append("the step before the gate does not remove a stale receipt")
    after = steps[g + 1] if g + 1 < len(steps) else {}
    if (after.get("env") or {}).get("RECEIPT") != RECEIPT or ASSERT not in str(after.get("run", "")):
        out.append("the step after the gate does not assert the receipt read >= 1 file")
    for s in (before, after):
        if "if" in s or s.get("continue-on-error"):
            out.append(f"step {s.get('name')!r} can be skipped or ignored")
    selftests = [i for i, s in enumerate(steps) if _tool(s) == ("ci-declared-literal", "--selftest")]
    if any(i < g + 1 for i in selftests):
        out.append("a selftest step runs before the receipt is asserted")
    return out


def test_the_real_workflow_asserts_the_receipt() -> None:
    assert faults(_steps()) == []


def _drop(name_part: str):
    def f(steps: list[dict]) -> list[dict]:
        return [s for s in steps if name_part not in str(s.get("name", ""))]
    return f


def _weaken(steps: list[dict]) -> list[dict]:
    for s in steps:
        if ASSERT in str(s.get("run", "")):
            s["run"] = s["run"].replace("[1-9][0-9]*", "[0-9]+")
    return steps


def _skippable(steps: list[dict]) -> list[dict]:
    for s in steps:
        if ASSERT in str(s.get("run", "")):
            s["continue-on-error"] = True
    return steps


def _selftest_first(steps: list[dict]) -> list[dict]:
    st = [s for s in steps if _tool(s)[1] == "--selftest"]
    rest = [s for s in steps if s not in st]
    return rest[:1] + st + rest[1:]


def _no_env(steps: list[dict]) -> list[dict]:
    for s in steps:
        if _tool(s)[0] == "ci-declared-literal" and _tool(s)[1] != "--selftest":
            s.pop("env", None)
    return steps


@pytest.mark.parametrize("mutate", [_drop("scanned the tree"), _drop("stale receipt"), _weaken, _skippable,
                                    _selftest_first, _no_env],
                         ids=["no-assert", "no-rm", "weak-regex", "continue-on-error", "selftest-first", "no-env"])
def test_each_way_of_losing_the_check_is_named(mutate) -> None:
    assert faults(mutate(copy.deepcopy(_steps())))
