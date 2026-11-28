"""ci-runners: the golden render, both directions of every schema rule, and the CLI's 0 / 1 / 2.

THE GOLDEN TEST is the live-safety contract: a supervisor reinstalls whenever its render's
`source_sha256` changes, so the library must render BYTE-IDENTICAL JSON to the hand-rolled renderer
it replaces. tests/fixtures/runners/golden/expected/ was rendered by that original renderer from
golden/runners.yml; any byte this library renders differently fails here.
"""

from __future__ import annotations

import json
import pathlib
import shutil

import pytest

from ci_kit.runners import JQ_PATH, Block, Duty, Kit, KitError, kit_from_dict, load_kit, render, validate
from ci_kit.runners.cli import main
from ci_kit.runners.selftest import ACCEPTS, DEMO_KIT, REFUSALS, good, refused, selftest

yaml = pytest.importorskip("yaml")

GOLDEN = pathlib.Path(__file__).parent / "fixtures" / "runners" / "golden"
OUTPUTS = {"runners": "ci-runners.json", "secure_runners": "ci-runners-secure.json",
           "sealed_runners": "ci-runners-sealed.json"}


def _golden():
    text = (GOLDEN / "runners.yml").read_text(encoding="utf-8")
    return yaml.safe_load(text), text, load_kit(GOLDEN / "kit.yml")


@pytest.mark.parametrize("block", sorted(OUTPUTS))
def test_golden_render_is_byte_identical_to_the_original_renderer(block):
    cfg, text, kit = _golden()
    assert validate(cfg, kit) == []
    want = (GOLDEN / "expected" / OUTPUTS[block]).read_bytes()
    assert render(cfg, text, block, kit).encode("utf-8") == want


def test_golden_check_is_clean(tmp_path, capsys):
    rc = main(["--check", "--config", str(GOLDEN / "runners.yml"), "--kit", str(GOLDEN / "kit.yml"),
               "--root", str(GOLDEN)])
    assert rc == 0, capsys.readouterr()


@pytest.mark.parametrize("name", sorted(ACCEPTS))
def test_accepts(name):
    assert ACCEPTS[name]() == []


@pytest.mark.parametrize("name", sorted(REFUSALS))
def test_refuses(name):
    assert refused(name), f"{name}: accepted, should be rejected"


def test_live_room_refusal_names_the_live_room():
    assert any("never fits the live room" in x for x in refused("peak over the live room"))


def test_selftest_passes():
    assert selftest() == []


def test_a_python_rule_runs_only_after_the_built_ins_pass():
    seen = []

    def rule(cfg, kit):
        seen.append(1)
        return ["domain rule fired"]

    kit = Kit(blocks=DEMO_KIT.blocks, rules=(rule,))
    assert validate(good(), kit) == ["domain rule fired"]
    bad = good()
    bad["runners"].pop("max_total")
    seen.clear()
    assert validate(bad, kit) and not seen


@pytest.mark.parametrize("edit", [
    lambda d: d.update(blocks={}),
    lambda d: d.update(nope=1),
    lambda d: d.update(source_hash="md5"),
    lambda d: d["blocks"]["secure_runners"].update(prefix="home-"),   # two instances, one prefix
    lambda d: d["blocks"]["secure_runners"].update(label="home"),     # two blocks, one tier
    lambda d: d["blocks"]["runners"].pop("variable"),
    lambda d: d["blocks"]["runners"].update(variable="ci-runner"),
    lambda d: d["blocks"].update(host={**d["blocks"]["runners"], "prefix": "h-", "label": "h", "instance": "h",
                                       "output": "h.json"}),
    lambda d: d["share_duties"]["drill"].pop("text"),
])
def test_a_malformed_kit_is_refused(edit):
    d = yaml.safe_load((GOLDEN / "kit.yml").read_text(encoding="utf-8"))
    edit(d)
    with pytest.raises(KitError):
        kit_from_dict(d)


def test_tier_is_the_longest_family():
    assert DEMO_KIT.tier("ci-sealed-x") == "ci-sealed"
    assert DEMO_KIT.tier("ci-heavy") == "ci"
    assert DEMO_KIT.tier("ci-sealedx") == "ci"
    assert DEMO_KIT.tier("self-hosted") is None


def test_raw_hash_follows_a_comment_and_canonical_does_not():
    cfg, text, kit = _golden()
    edited = text + "# a comment-only edit\n"
    raw = [json.loads(render(cfg, t, "runners", kit))["source_sha256"] for t in (text, edited)]
    assert raw[0] != raw[1]
    canon = Kit(blocks=kit.blocks, source_hash="canonical")
    c = [json.loads(render(yaml.safe_load(t), t, "runners", canon))["source_sha256"] for t in (text, edited)]
    assert c[0] == c[1] != raw[0]
    changed = text.replace("max_total: 7", "max_total: 6")
    assert json.loads(render(yaml.safe_load(changed), changed, "runners", canon))["source_sha256"] != c[0]


def _copy(tmp_path) -> pathlib.Path:
    root = tmp_path / "repo"
    shutil.copytree(GOLDEN, root)
    return root


def _run(root, *extra) -> int:
    return main([*extra, "--config", str(root / "runners.yml"), "--kit", str(root / "kit.yml"), "--root", str(root)])


def test_a_stale_render_is_exit_1_and_write_fixes_it(tmp_path, capsys):
    root = _copy(tmp_path)
    (root / "expected" / "ci-runners.json").write_text("{}\n", encoding="utf-8")
    assert _run(root, "--check") == 1
    assert "ci-runners.json is stale" in capsys.readouterr().err
    assert _run(root, "--write") == 0
    assert _run(root, "--check") == 0
    assert (root / "expected" / "ci-runners.json").read_bytes() == (GOLDEN / "expected" / "ci-runners.json").read_bytes()


def test_an_invalid_capacity_file_is_exit_1(tmp_path):
    root = _copy(tmp_path)
    p = root / "runners.yml"
    p.write_text(p.read_text(encoding="utf-8").replace("max_total: 7", "max_total: 70"), encoding="utf-8")
    assert _run(root, "--check") == 1
    assert _run(root, "--write") == 1


def test_an_orphan_render_and_an_unmet_share_duty_are_findings(tmp_path, capsys):
    root = _copy(tmp_path)
    p = root / "runners.yml"
    text = p.read_text(encoding="utf-8")
    p.write_text(text[:text.index("sealed_runners:")], encoding="utf-8")
    (root / "docs" / "DRILL.md").write_text("nothing here\n", encoding="utf-8")
    assert _run(root, "--write") == 0
    assert _run(root, "--check") == 1
    err = capsys.readouterr().err
    assert "ci-runners-sealed.json exists but runners.yml has no sealed_runners" in err
    assert "does not carry the duty" in err


def test_alloc_jq_copy_is_checked_and_written(tmp_path, capsys):
    root = _copy(tmp_path)
    k = root / "kit.yml"
    k.write_text(k.read_text(encoding="utf-8") + "alloc_jq: alloc.jq\n", encoding="utf-8")
    assert _run(root, "--check") == 1
    assert "alloc.jq is not ci-kit's allocator" in capsys.readouterr().err
    assert _run(root, "--write") == 0
    assert (root / "alloc.jq").read_bytes() == JQ_PATH.read_bytes()
    assert _run(root, "--check") == 0


def test_out_dir_renders_elsewhere(tmp_path):
    root = _copy(tmp_path)
    out = tmp_path / "shadow"
    assert _run(root, "--write", "--out-dir", str(out)) == 0
    for name in OUTPUTS.values():
        assert (out / name).read_bytes() == (GOLDEN / "expected" / name).read_bytes()


@pytest.mark.parametrize("how", ["no-kit", "bad-yaml", "bad-kit", "missing-config"])
def test_could_not_look_is_exit_2(tmp_path, how):
    root = _copy(tmp_path)
    if how == "no-kit":
        assert main(["--check", "--config", str(root / "runners.yml"), "--root", str(root)]) == 2
        return
    if how == "bad-yaml":
        (root / "runners.yml").write_text("host: [unclosed\n", encoding="utf-8")
    elif how == "bad-kit":
        (root / "kit.yml").write_text("blocks: {}\n", encoding="utf-8")
    else:
        (root / "runners.yml").unlink()
    assert _run(root, "--check") == 2


def test_alloc_cli(monkeypatch, capsys):
    import io

    doc = {"max_total": 2, "mem_budget_mb": 4096, "peaks": {"a": 1024},
           "pools": [{"repo": "a", "min": 0, "max": 2, "running": 0, "busy": 0, "idle": 0, "queued": 1, "oldest": 5}]}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(doc)))
    assert main(["--alloc"]) == 0
    assert json.loads(capsys.readouterr().out)["pools"][0]["start"] == 1
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({**doc, "mem_budget_mb": None})))
    assert main(["--alloc"]) == 2


def test_kit_round_trip_from_dataclasses():
    kit = Kit(blocks=(Block("runners", "i", "p-", "socket", "lbl", "VAR", "o.json"),),
              share_duties={"r": Duty("d.md", "t")})
    assert kit.keys == ("runners",) and kit.share_duties["r"].text == "t"
