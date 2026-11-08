"""release_bin: a 5xx is retried and the binary lands; a 4xx is not retried; retries exhausted -> exit 2;
a cached binary is never fetched. Driven against a REAL local HTTP server serving a real tarball.
"""
from __future__ import annotations

import hashlib
import http.server
import io
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
    state = {"codes": [], "hits": 0}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            state["hits"] += 1
            code = state["codes"].pop(0) if state["codes"] else 200
            self.send_response(code)
            self.end_headers()
            if code == 200:
                self.wfile.write(BLOB)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{httpd.server_address[1]}/{{v}}/{{asset}}.tar.gz"
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
