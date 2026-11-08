"""release_bin: a 5xx is retried and the binary lands; a 4xx is not retried; retries exhausted -> exit 2;
a cached binary is never fetched. Driven against a REAL local HTTP server serving a real tarball.
"""
from __future__ import annotations

import hashlib
import http.server
import io
import json
import tarfile
import threading

import pytest

from ci_kit import release_bin
from ci_kit.release_bin import Pin, binary, canonical_platform

EXE = b"#!/bin/sh\necho fake\n"


def _tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("pkg/fake" + (".exe" if canonical_platform().startswith("windows") else ""))
        info.size = len(EXE)
        tar.addfile(info, io.BytesIO(EXE))
    return buf.getvalue()


BLOB = _tarball()


@pytest.fixture
def server(monkeypatch):
    """Serves the queued status codes in order (then 200 with BLOB); records every request."""
    monkeypatch.setattr(release_bin, "FETCH_BACKOFF_S", (0.0, 0.0))
    state = {"codes": [], "hits": 0, "api_codes": [], "api_hits": 0}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/api/"):
                state["api_hits"] += 1
                state.setdefault("api_auth", []).append(self.headers.get("Authorization"))
                if self.path == "/api/asset/1" and state.get("redirect"):
                    self.send_response(302)
                    self.send_header("Location", f"{base}/blob")
                    self.end_headers()
                    return
                code = state["api_codes"].pop(0) if state["api_codes"] else 200
                self.send_response(code)
                self.end_headers()
                if code != 200:
                    return
                if "/releases/tags/" in self.path and state.get("api_garbage"):
                    self.wfile.write(b"<html>not json</html>")
                elif "/releases/tags/" in self.path:
                    name = f"{canonical_platform()}.tar.gz"
                    self.wfile.write(json.dumps({"assets": [{"name": name, "url": f"{base}/api/asset/1"}]}).encode())
                elif self.headers.get("Accept") == "application/octet-stream":
                    self.wfile.write(BLOB)
                return
            if self.path == "/blob":  # the signed-blob host a real asset URL redirects to
                state["blob_auth"] = self.headers.get("Authorization")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(BLOB)
                return
            state["hits"] += 1
            state.setdefault("primary_auth", []).append(self.headers.get("Authorization"))
            code = state["codes"].pop(0) if state["codes"] else 200
            self.send_response(code)
            self.end_headers()
            if code == 200:
                self.wfile.write(BLOB)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(release_bin, "GITHUB_WEB", base)
    monkeypatch.setattr(release_bin, "GITHUB_API", f"{base}/api")
    state["url"] = f"{base}/{{v}}/{{asset}}.tar.gz"  # not a release-download shape: no API fallback
    state["gh_url"] = f"{base}/o/r/releases/download/v{{v}}/{{asset}}.tar.gz"
    yield state
    httpd.shutdown()


def _pin(url: str) -> Pin:
    plat = canonical_platform()
    return Pin(name="fake", version="1.0.0", url=url, assets={plat: plat},
               sha256={plat: hashlib.sha256(BLOB).hexdigest()}, env="CI_KIT_FAKE_UNSET")


def test_a_504_is_retried_and_the_binary_lands(server, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("CI_KIT_CACHE_DIR", str(tmp_path))
    server["codes"] = [504]
    exe = binary(_pin(server["url"]), gate="t")
    assert exe.read_bytes() == EXE and server["hits"] == 2
    assert "retry 2/3" in capsys.readouterr().err
    assert [p.name for p in exe.parent.iterdir()] == [exe.name]  # no temp file left behind


def test_every_attempt_5xx_is_blind_exit_2(server, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("CI_KIT_CACHE_DIR", str(tmp_path))
    server["codes"] = [504, 502, 503]
    with pytest.raises(SystemExit) as e:
        binary(_pin(server["url"]), gate="t")
    assert e.value.code == 2 and server["hits"] == 3
    assert "DID NOT FINISH LOOKING" in capsys.readouterr().err


def test_a_404_is_not_retried(server, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CI_KIT_CACHE_DIR", str(tmp_path))
    server["codes"] = [404]
    with pytest.raises(SystemExit) as e:
        binary(_pin(server["url"]), gate="t")
    assert e.value.code == 2 and server["hits"] == 1


def test_a_cached_binary_is_not_fetched(server, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("CI_KIT_CACHE_DIR", str(tmp_path))
    first = binary(_pin(server["url"]), gate="t")
    assert "not in cache" in capsys.readouterr().err
    second = binary(_pin(server["url"]), gate="t")
    assert second == first and server["hits"] == 1
    assert "from cache" in capsys.readouterr().err


def test_a_dead_download_path_falls_back_to_the_api_asset(server, tmp_path, monkeypatch, capsys) -> None:
    # 2026-11-08: releases/download 504'd every time while the REST asset path served the bytes
    monkeypatch.setenv("CI_KIT_CACHE_DIR", str(tmp_path))
    server["codes"] = [504, 504, 504]
    exe = binary(_pin(server["gh_url"]), gate="t")
    assert exe.read_bytes() == EXE and server["hits"] == 1 and server["api_hits"] == 2
    assert "through the API" in capsys.readouterr().err


def test_both_paths_down_is_still_blind_exit_2(server, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CI_KIT_CACHE_DIR", str(tmp_path))
    server["codes"] = [504, 504, 504]
    server["api_codes"] = [503, 503, 503]
    with pytest.raises(SystemExit) as e:
        binary(_pin(server["gh_url"]), gate="t")
    assert e.value.code == 2 and server["hits"] == 3 and server["api_hits"] == 3


def test_a_404_download_does_not_try_the_api(server, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CI_KIT_CACHE_DIR", str(tmp_path))
    server["codes"] = [404]
    with pytest.raises(SystemExit):
        binary(_pin(server["gh_url"]), gate="t")
    assert server["hits"] == 1 and server["api_hits"] == 0


def test_an_api_4xx_does_not_stop_the_download_retry(server, tmp_path, monkeypatch, capsys) -> None:
    # review 2026-11-08: a rate-limited (403) API path after a download 504 ended at attempt 1/3
    monkeypatch.setenv("CI_KIT_CACHE_DIR", str(tmp_path))
    server["codes"] = [504]
    server["api_codes"] = [403]
    exe = binary(_pin(server["gh_url"]), gate="t")
    assert exe.read_bytes() == EXE and server["hits"] == 2
    assert "API path: HTTP Error 403" in capsys.readouterr().err


def test_unreadable_api_metadata_is_blind_not_a_traceback(server, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CI_KIT_CACHE_DIR", str(tmp_path))
    server["codes"] = [504, 504, 504]
    server["api_garbage"] = True
    with pytest.raises(SystemExit) as e:
        binary(_pin(server["gh_url"]), gate="t")
    assert e.value.code == 2 and server["hits"] == 3


def test_an_unwritable_cache_is_blind_exit_2(server, tmp_path, monkeypatch, capsys) -> None:
    blocker = tmp_path / "cache"
    blocker.write_text("a file where the cache dir should be")
    monkeypatch.setenv("CI_KIT_CACHE_DIR", str(blocker))
    with pytest.raises(SystemExit) as e:
        binary(_pin(server["url"]), gate="t")
    assert e.value.code == 2 and "could not write" in capsys.readouterr().err


def test_the_api_path_sends_the_token_and_the_blob_host_never_sees_it(server, tmp_path, monkeypatch) -> None:
    # unauthenticated, the fallback was 403 rate-limited on a shared hosted IP (ci-kit#44, 2026-11-08)
    monkeypatch.setenv("CI_KIT_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("CI_KIT_GITHUB_TOKEN", "tkn-test")
    server["codes"], server["redirect"] = [504, 504, 504], True
    exe = binary(_pin(server["gh_url"]), gate="t")
    assert exe.read_bytes() == EXE
    assert server["api_auth"] == ["Bearer tkn-test", "Bearer tkn-test"]
    assert server["blob_auth"] is None  # an unredirected header: the redirect drops it
    # the primary download path reaches non-GitHub hosts (ci-tf-seed: the terraform registry)
    assert server["primary_auth"] == [None]


def test_the_token_goes_to_api_github_only(server, tmp_path, monkeypatch) -> None:
    # an asset URL the release JSON points off the API host gets no token
    monkeypatch.setenv("CI_KIT_GITHUB_TOKEN", "tkn-test")
    monkeypatch.setattr(release_bin, "GITHUB_API", "https://api.github.invalid")
    release_bin._get(server["url"].format(v="1", asset="a"), auth=True)
    assert server["primary_auth"] == [None]


def test_no_token_is_unauthenticated_as_before(server, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CI_KIT_CACHE_DIR", str(tmp_path))
    for e in release_bin.TOKEN_ENVS:
        monkeypatch.delenv(e, raising=False)
    server["codes"] = [504, 504, 504]
    binary(_pin(server["gh_url"]), gate="t")
    assert server["api_auth"] == [None, None]
