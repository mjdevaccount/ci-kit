"""`ci-runners --verify-prom` (ci_kit/runners/verify_prom.py): both directions.

A complete tick's file verifies clean; every requirement, removed or wrong, is a finding that names
it; the states that are deliberate or external (the registry cache off, or not answering; no floor
pool) are NOTES; inputs it cannot read are exit 2, never a pass.
"""

from __future__ import annotations

import copy
import json

import pytest

from ci_kit.runners.cli import main
from ci_kit.runners.verify_prom import PER_POOL, REGISTRY, REQUIRED, SECURE, VerifyError, verify

MP = "ci_"
SOCKET = {"instance": "ci", "isolation": "socket", "prefix": "ci-", "source_sha256": "0123456789ab", "max_total": 4,
          "pools": [{"repo": "app", "min": 1}, {"repo": "lib", "min": 0}],
          "supervisor": {"metric_prefix": MP, "registry_cache": {"name": "reg"}}}
SECURE_R = {**SOCKET, "instance": "secure", "isolation": "secure", "prefix": "secure-",
            "supervisor": {"metric_prefix": MP, "registry_cache": None}}


def tick(render: dict) -> list[str]:
    """The lines a complete, healthy tick publishes for `render` (every sample supervisor-labelled)."""
    s = f'supervisor="{render["instance"]}"'
    lines = ["# HELP ci_runner_collect_ok 1 = looked", "# TYPE ci_runner_collect_ok gauge",
             f"{MP}runner_collect_ok{{{s}}} 1",
             f'{MP}runner_config_info{{{s},source_sha256="{render["source_sha256"]}"}} 1',
             f"{MP}runner_max_total{{{s}}} {render['max_total']}"]
    lines += [f"{MP}{n}{{{s}}} 7" for n in REQUIRED]
    for p in render["pools"]:
        lines += [f'{MP}{n}{{{s},repo="{p["repo"]}"}} 3' for n in PER_POOL]
        if p["min"] >= 1:
            lines.append(f'{MP}runner_mem_working_set_max_bytes{{{s},repo="{p["repo"]}"}} 1048576')
    if render["isolation"] == "secure":
        lines += [f"{MP}{n}{{{s}}} {v}" for n, v in SECURE] + [f"{MP}runner_isolation_checked{{{s}}} 2"]
    else:
        lines += [f"{MP}{n}{{{s}}} {1 if n in ('registry_cache_up', 'registry_upstream_ok', 'registry_mirror_configured') else 0}"
                  for n in REGISTRY]
    return lines


def text(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("render", [SOCKET, SECURE_R], ids=["socket", "secure"])
def test_a_complete_tick_verifies_clean(render):
    assert verify(text(tick(render)), render) == ([], [])


@pytest.mark.parametrize("render", [SOCKET, SECURE_R], ids=["socket", "secure"])
def test_every_required_sample_removed_is_a_finding_naming_it(render):
    """MUST-FIRE, one sample at a time: no line of a healthy tick is decoration."""
    lines = tick(render)
    for i, line in enumerate(lines):
        if line.startswith("#"):
            continue
        name = line.split("{", 1)[0]
        findings, _ = verify(text(lines[:i] + lines[i + 1:]), render)
        assert any(f.startswith(name) for f in findings), (line, findings)


@pytest.mark.parametrize("old,new,needle", [
    ("runner_collect_ok{supervisor=\"ci\"} 1", "runner_collect_ok{supervisor=\"ci\"} 0", "runner_collect_ok: want 1"),
    ('source_sha256="0123456789ab"', 'source_sha256="ba9876543210"', "runner_config_info{"),
    ("runner_max_total{supervisor=\"ci\"} 4", "runner_max_total{supervisor=\"ci\"} 5", "want 4, have 5"),
    ("registry_mirror_configured{supervisor=\"ci\"} 1", "registry_mirror_configured{supervisor=\"ci\"} 0",
     "registry_mirror_configured: want 1"),
    ("runner_vm_oom_kills_total{supervisor=\"ci\"} 7", "runner_vm_oom_kills_total{supervisor=\"ci\"} -1",
     "not a non-negative integer"),
    ("runner_vm_oom_kills_total{supervisor=\"ci\"} 7", "runner_vm_oom_kills_total{supervisor=\"ci\"} 1.5",
     "not a non-negative integer"),
])
def test_a_wrong_value_is_a_finding(old, new, needle):
    t = text(tick(SOCKET))
    assert old in t
    findings, _ = verify(t.replace(old, new), SOCKET)
    assert any(needle in f for f in findings), findings


@pytest.mark.parametrize("name,bad", [*[(n, 1 - v) for n, v in SECURE], ("runner_isolation_checked", 0)])
def test_each_secure_condition_unmet_is_a_finding(name, bad):
    lines = [ln.rsplit(" ", 1)[0] + f" {bad}" if ln.startswith(f"{MP}{name}{{") else ln for ln in tick(SECURE_R)]
    findings, _ = verify(text(lines), SECURE_R)
    assert any(name in f for f in findings), findings


def test_only_this_instances_samples_count():
    """Another instance's file, or an older script that did not label, satisfies nothing."""
    other = text(tick(SOCKET)).replace('supervisor="ci"', 'supervisor="other"')
    assert verify(other, SOCKET)[0] == ['no sample carries supervisor="ci": not this instance\'s file, or not its script']
    unlabelled = text(tick(SOCKET)).replace('{supervisor="ci"}', "").replace('supervisor="ci",', "")
    assert len(verify(unlabelled, SOCKET)[0]) == 1
    # one sample relabelled is that one sample missing, nothing else
    one = text(tick(SOCKET)).replace('runner_max_total{supervisor="ci"}', 'runner_max_total{supervisor="x"}')
    assert [f.split(":")[0] for f in verify(one, SOCKET)[0]] == [f"{MP}runner_max_total"]


def test_a_commented_sample_is_not_a_sample():
    t = text(tick(SOCKET)).replace(f"{MP}runner_collect_ok{{", f"# {MP}runner_collect_ok{{")
    assert any("runner_collect_ok" in f for f in verify(t, SOCKET)[0])


def test_the_registry_cache_off_or_down_is_a_note_not_a_finding():
    base = [ln for ln in tick(SOCKET) if not ln.startswith(f"{MP}registry_")]
    off = text(base + [f'{MP}registry_disabled{{supervisor="ci"}} 1'])
    findings, notes = verify(off, SOCKET)
    assert findings == [] and any("OFF" in n for n in notes)
    down = text(tick(SOCKET)).replace('registry_cache_up{supervisor="ci"} 1', 'registry_cache_up{supervisor="ci"} 0')
    findings, notes = verify(down, SOCKET)
    assert findings == [] and any("not answering" in n for n in notes)
    # ...but the cache's gauges absent with the cache ON is the guard that cannot fire
    assert {f.split(":")[0] for f in verify(text(base), SOCKET)[0]} == {MP + n for n in REGISTRY}


def test_no_registry_cache_requires_none_of_its_gauges():
    r = copy.deepcopy(SOCKET)
    r["supervisor"]["registry_cache"] = None
    assert verify(text([ln for ln in tick(r) if f"{MP}registry_" not in ln]), r) == ([], [])


def test_the_working_set_is_required_only_of_a_floor_pool():
    r = copy.deepcopy(SOCKET)
    r["pools"][0]["min"] = 0
    lines = [ln for ln in tick(SOCKET) if "mem_working_set_max_bytes" not in ln]
    findings, notes = verify(text(lines), r)
    assert findings == [] and notes == ["no pool keeps a floor: the working-set gauge is not checked"
                                        " (only a live runner is sampled)"]
    assert [f.split(":")[0] for f in verify(text(lines), SOCKET)[0]] == [
        f'{MP}runner_mem_working_set_max_bytes{{repo="app"}}']


@pytest.mark.parametrize("edit", [
    lambda r: r.pop("supervisor"), lambda r: r.pop("pools"), lambda r: r.pop("source_sha256"),
    lambda r: r.update(isolation="sealed"), lambda r: r.update(pools=None),
])
def test_a_render_it_cannot_read_is_could_not_check(edit):
    r = copy.deepcopy(SOCKET)
    edit(r)
    with pytest.raises(VerifyError):
        verify(text(tick(SOCKET)), r)


def test_the_cli_exits_0_1_2(tmp_path, capsys):
    prom, render = tmp_path / "x.prom", tmp_path / "r.json"
    render.write_text(json.dumps(SOCKET), encoding="utf-8")
    prom.write_text(text(tick(SOCKET)), encoding="utf-8")
    assert main(["--verify-prom", str(prom), "--render", str(render)]) == 0
    prom.write_text(text(tick(SOCKET)[:-1]), encoding="utf-8")
    assert main(["--verify-prom", str(prom), "--render", str(render)]) == 1
    assert "1 finding(s)" in capsys.readouterr().out
    assert main(["--verify-prom", str(prom)]) == 2
    assert main(["--verify-prom", str(tmp_path / "absent.prom"), "--render", str(render)]) == 2
    render.write_text("[1]", encoding="utf-8")
    assert main(["--verify-prom", str(prom), "--render", str(render)]) == 2
    render.write_text("{not json", encoding="utf-8")
    assert main(["--verify-prom", str(prom), "--render", str(render)]) == 2
    assert "CANNOT CHECK" in capsys.readouterr().err


@pytest.mark.parametrize("extra,needle", [
    ('ci_runner_collect_ok{supervisor="ci"} 0', "published twice"),
    ('ci_runner_max_total{supervisor="ci"} 4', "published twice"),
    ('ci_runner_vm_volumes{supervisor="ci",,garbage} 4', "not a sample"),
    ('ci_runner_vm_volumes{supervisor="ci" 4', "not a sample"),
    ("ci_runner_vm_volumes", "not a sample"),
])
def test_a_file_the_collector_would_reject_is_a_finding(extra, needle):
    """review 891 F2: node-exporter drops a file with a duplicate series or a broken line, so every
    series in it vanishes -- each one, read alone, looking present is not a pass."""
    lines = tick(SOCKET)
    findings, _ = verify(text([extra, *lines]), SOCKET)
    assert any(needle in f for f in findings), findings
    # the same series in another label order is the same series
    swapped = 'ci_runner_config_info{source_sha256="0123456789ab",supervisor="ci"} 1'
    assert any("published twice" in f for f in verify(text([swapped, *lines]), SOCKET)[0])


@pytest.mark.parametrize("edit", [
    lambda r: r["pools"][0].update(min=None), lambda r: r["pools"][0].update(min="1"),
    lambda r: r["supervisor"].update(metric_prefix=None), lambda r: r.update(instance=3),
    lambda r: r.update(max_total=True), lambda r: r.update(supervisor=[]),
])
def test_a_mistyped_render_is_could_not_check_not_a_finding(edit):
    """review 891 F3: a render this cannot read is exit 2, never a traceback read as findings."""
    r = copy.deepcopy(SOCKET)
    edit(r)
    with pytest.raises(VerifyError):
        verify(text(tick(SOCKET)), r)
