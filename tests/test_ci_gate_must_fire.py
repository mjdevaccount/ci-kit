"""ci-gate-lint must fire: a skip-green shape -> 1, the aggregate shape -> 0, cannot read -> 2.

The case that motivated it is trade-infra#806's ci.yml as it was on 2026-11-06: four jobs behind
`needs: detect` and no aggregate, so a dead detect merged four skipped "required" checks.
"""
from __future__ import annotations

import pathlib

import pytest

from ci_kit import ci_gate

SHAPE_806 = """
on: pull_request
jobs:
  detect:
    runs-on: x
    steps: [{uses: dorny/paths-filter@v4, id: filter}]
  validate: {needs: detect, if: "needs.detect.outputs.code == 'true'", runs-on: x, steps: [{run: 'true'}]}
  python: {needs: detect, if: "needs.detect.outputs.code == 'true'", runs-on: x, steps: [{run: 'true'}]}
  gitleaks: {uses: o/ci-kit/.github/workflows/gitleaks.yml@v1}
"""

FIXED = SHAPE_806 + """
  gate:
    if: always()
    needs: [detect, validate, python, gitleaks]
    runs-on: ubuntu-latest
    permissions: {}
    steps:
      - uses: re-actors/alls-green@b5b5b37504aa4183270bd3d855c52a67f212be35 # v1.3.0
        with:
          allowed-skips: validate, python
          jobs: ${{ toJSON(needs) }}
"""


def _repo(tmp_path: pathlib.Path, text: str) -> pathlib.Path:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(text, encoding="utf-8")
    return tmp_path


def test_the_806_shape_fails(tmp_path, capsys) -> None:
    assert ci_gate.main(["--root", str(_repo(tmp_path, SHAPE_806))]) == 1
    out = capsys.readouterr().out
    assert "no-aggregate" in out and "validate" in out and "python" in out


def test_the_aggregate_passes(tmp_path, capsys) -> None:
    assert ci_gate.main(["--root", str(_repo(tmp_path, FIXED))]) == 0
    assert "ci-gate-lint: OK" in capsys.readouterr().out


@pytest.mark.parametrize(("old", "new", "rule"), [
    ("if: always()", "if: ${{ !cancelled() }}", "not-always"),
    ("needs: [detect, validate, python, gitleaks]", "needs: [detect, validate, python]", "uncovered"),
    ("allowed-skips: validate, python", "allowed-skips: validate, python, detect", "skip-upstream"),
    ("allowed-skips: validate, python", "allowed-skips: validate, python, gitleaks", "skip-no-if"),
    ("jobs: ${{ toJSON(needs) }}", "jobs: ${{ toJSON(needs.detect) }}", "jobs-input"),
    ("runs-on: ubuntu-latest", "runs-on: ${{ fromJSON(vars.CI_RUNNER) }}", "no-python"),
])
def test_each_rot_of_the_aggregate_fails(tmp_path, capsys, old, new, rule) -> None:
    assert old in FIXED
    assert ci_gate.main(["--root", str(_repo(tmp_path, FIXED.replace(old, new)))]) == 1
    assert rule in capsys.readouterr().out


def test_a_workflow_with_no_needs_is_clean(tmp_path) -> None:
    text = "jobs:\n  test: {runs-on: x, steps: [{run: 'true'}]}\n"
    assert ci_gate.main(["--root", str(_repo(tmp_path, text))]) == 0


def test_two_aggregates_fail(tmp_path, capsys) -> None:
    text = FIXED + FIXED[len(SHAPE_806):].replace("  gate:", "  gate2:")
    assert ci_gate.main(["--root", str(_repo(tmp_path, text))]) == 1
    assert "many" in capsys.readouterr().out


def _rc(argv: list[str]) -> int:
    """main's return code, or the code of the SystemExit a could-not-look scan raises."""
    try:
        return ci_gate.main(argv)
    except SystemExit as e:
        return int(e.code)


@pytest.mark.parametrize("text", ["jobs: [", "on: push\n", ""])
def test_unreadable_or_jobless_is_2(tmp_path, text) -> None:
    assert _rc(["--root", str(_repo(tmp_path, text))]) == 2


def test_a_missing_workflow_is_2(tmp_path) -> None:
    assert _rc(["--root", str(tmp_path)]) == 2


def test_selftest_passes_and_names_its_mutations() -> None:
    assert ci_gate.selftest() == []
    assert {m[0] for m in ci_gate._MUTATIONS} >= {"no-aggregate", "not-always", "uncovered", "skip-upstream"}


def test_a_self_hosted_aggregate_with_setup_python_passes(tmp_path) -> None:
    text = FIXED.replace("runs-on: ubuntu-latest\n    permissions: {}\n    steps:\n",
                         "runs-on: [self-hosted, home]\n    permissions: {}\n    steps:\n"
                         "      - uses: actions/setup-python@v7\n")
    assert "setup-python" in text
    assert ci_gate.main(["--root", str(_repo(tmp_path, text))]) == 0
