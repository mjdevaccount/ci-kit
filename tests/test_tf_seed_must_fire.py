"""ci-tf-seed: a cold cache is filled from the lock file, a warm one is never fetched, a sha256
mismatch is refused (1), and a registry that cannot be read is blind (2). The network is stubbed at
`release_bin.fetch`, the one door every byte comes through.
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest

from ci_kit import release_bin, tf_seed

LOCK = '''# This file is maintained automatically by "terraform init".
provider "registry.terraform.io/bpg/proxmox" {
  version     = "0.120.0"
  constraints = "~> 0.120"
  hashes = [
    "h1:abc=",
  ]
}
'''


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("terraform-provider-proxmox_v0.120.0", b"\x7fELF fake")
        zf.writestr("LICENSE", b"MIT")
    return buf.getvalue()


ZIP = _zip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    (tmp_path / "tf").mkdir()
    (tmp_path / "tf" / ".terraform.lock.hcl").write_text(LOCK)
    monkeypatch.setenv("TF_PLUGIN_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(tf_seed, "tf_platform", lambda: ("linux", "amd64"))
    state = {"urls": [], "sha": hashlib.sha256(ZIP).hexdigest(), "disco": {"providers.v1": "/v1/providers/"}}

    def fetch(url, *, gate):
        state["urls"].append(url)
        if url.endswith("/.well-known/terraform.json"):
            return json.dumps(state["disco"]).encode()
        if "/download/linux/amd64" in url:
            return json.dumps({"download_url": "https://github.com/bpg/x/releases/download/v0.120.0/p.zip",
                               "shasum": state["sha"]}).encode()
        return ZIP

    monkeypatch.setattr(release_bin, "fetch", fetch)
    return tmp_path, state


def test_lock_parse():
    assert tf_seed.locked(LOCK) == [("registry.terraform.io", "bpg", "proxmox", "0.120.0")]


def test_cold_seeds_then_warm_fetches_nothing(world):
    tmp, state = world
    assert tf_seed.main(["--dir", str(tmp / "tf")]) == 0
    target = tmp / "cache" / "registry.terraform.io" / "bpg" / "proxmox" / "0.120.0" / "linux_amd64"
    assert (target / "terraform-provider-proxmox_v0.120.0").read_bytes() == b"\x7fELF fake"
    assert "https://registry.terraform.io/v1/providers/bpg/proxmox/0.120.0/download/linux/amd64" in state["urls"]
    state["urls"].clear()
    assert tf_seed.main(["--dir", str(tmp / "tf")]) == 0
    assert state["urls"] == []  # warm: not even the registry


def test_sha_mismatch_is_refused(world):
    tmp, state = world
    state["sha"] = "0" * 64
    assert tf_seed.main(["--dir", str(tmp / "tf")]) == 1
    assert not (tmp / "cache" / "registry.terraform.io" / "bpg" / "proxmox" / "0.120.0" / "linux_amd64").exists()


def test_registry_without_service_is_blind(world):
    tmp, state = world
    state["disco"] = {}
    with pytest.raises(SystemExit) as e:
        tf_seed.main(["--dir", str(tmp / "tf")])
    assert e.value.code == 2


def test_no_lock_or_empty_lock_is_blind(tmp_path, monkeypatch):
    monkeypatch.setenv("TF_PLUGIN_CACHE_DIR", str(tmp_path / "c"))
    with pytest.raises(SystemExit) as e:
        tf_seed.main(["--dir", str(tmp_path)])
    assert e.value.code == 2
    (tmp_path / ".terraform.lock.hcl").write_text("# empty\n")
    with pytest.raises(SystemExit) as e:
        tf_seed.main(["--dir", str(tmp_path)])
    assert e.value.code == 2


def test_no_cache_env_exports_one(tmp_path, monkeypatch):
    monkeypatch.delenv("TF_PLUGIN_CACHE_DIR", raising=False)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    gh_env = tmp_path / "env"
    monkeypatch.setenv("GITHUB_ENV", str(gh_env))
    assert tf_seed.cache_dir() == tmp_path / "tf-plugin-cache"
    assert gh_env.read_text() == f"TF_PLUGIN_CACHE_DIR={tmp_path / 'tf-plugin-cache'}\n"


def test_zip_slip_is_refused(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil", b"x")
    with pytest.raises(SystemExit) as e:
        tf_seed.unpack(buf.getvalue(), tmp_path / "t")
    assert e.value.code == 2
    assert not (tmp_path / "evil").exists()
