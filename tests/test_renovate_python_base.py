"""A python base-image DIGEST or PATCH merges on green; a MINOR or major stays held -- both directions.

Operator ruling 2026-11-28: green tests = merge. A consumer's #968 (python:3.15-slim digest, its PR
image build green) sat CLEAN for eight hours with `Automerge: Disabled by config`, read by nobody,
until a session merged it by hand. The hold exists for a NEW interpreter X.Y under every image
(#608, python:3.14), which a digest or patch is not. Resolved over the WHOLE rule list
(test_renovate_floating_digest._resolve), so a later rule that re-holds a digest, or arms a minor,
turns this red.
"""
from __future__ import annotations

import pytest
from test_renovate_floating_digest import _resolve


def _dep(update: str) -> dict:
    return {"manager": "dockerfile", "datasource": "docker", "updateType": update, "packageName": "python",
            "depName": "python", "repository": "mjdevaccount/trade-infra"}


@pytest.mark.parametrize("update", ["digest", "patch", "pinDigest"])
def test_a_python_digest_or_patch_merges_on_green_must_fire(update: str) -> None:
    cfg = _resolve(_dep(update))
    assert cfg.get("automerge") is True, f"python {update}: held for a reader again"
    assert cfg.get("groupName") == "python base image"


@pytest.mark.parametrize("update", ["minor", "major"])
def test_a_new_python_minor_or_major_stays_held_must_not_fire(update: str) -> None:
    cfg = _resolve(_dep(update))
    assert cfg.get("automerge") is False, f"python {update} would merge a new interpreter unattended"
