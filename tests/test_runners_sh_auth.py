"""github-auth.sh (ci_kit/runners/sh/), the supervisors' ONE GitHub credential: the App's installation
token, else the PAT, loudly. Real bash, openssl, jq and date; `curl` is a stub on PATH.

Both directions. APP: a mint signs a JWT the App's PUBLIC key verifies (RS256, iss = App ID, a
9.5-min window), POSTs the installation's access_tokens and caches the token 0600; a cached token
with time left is reused with no call; one inside the 10-min margin is re-minted; a missing
installation file is resolved by GET /app/installations (the OWNER's) and cached, and a 404 mint
drops that cache. PAT: no App files, a refused mint or an unsigned key each fall back to
github.token and SAY so (stderr and <prefix>github_auth{mode="pat"}). No token, JWT or key ever
reaches stderr. The gauges carry CI_RUNNER_METRIC_PREFIX.

A missing tool FAILS in CI (`$CI` set) and skips only on a workstation. A workstation without flock
(Git Bash) gets a no-op stand-in: nothing here races two mints, and CI runs the real one.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time

import pytest

from ci_kit.runners.scripts import SH_DIR

HELPER = (SH_DIR / "github-auth.sh").as_posix()
APP_ID, INST = "5722", "1811"

_CURL = r"""#!/usr/bin/env bash
# logs "METHOD path" and the bearer it was handed (to ${CURL_LOG}.auth); answers from
# "$CURL_FX/<METHOD path, non-alnum -> _>" = status line, then body; no fixture = 404
out=/dev/null m=GET url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;; -X) m="$2"; shift ;; -w) shift ;;
    -H) [[ "$2" == @* ]] && sed -n 's/^Authorization: Bearer //p' "${2#@}" >> "${CURL_LOG}.auth"; shift ;;
    --connect-timeout|--max-time) shift ;;
    http*) url="$1" ;;
  esac
  shift
done
p="${url#http://gh}"
echo "${m} ${p}" >> "${CURL_LOG}"
f="${CURL_FX}/$(printf '%s %s' "${m}" "${p}" | tr -c 'A-Za-z0-9' '_')"
[ -f "${f}" ] || { echo '{"message":"Not Found"}' > "${out}"; printf 404; exit 0; }
tail -n +2 "${f}" > "${out}"
printf '%s' "$(head -n1 "${f}")"
"""
# jq.exe on a Windows workstation ends lines CRLF: strip it (a no-op on Linux)
_JQ = '#!/usr/bin/env bash\nset -o pipefail\n"$REAL_JQ" "$@" | tr -d \'\\r\'\n'


def _fx_name(method: str, path: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in f"{method} {path}")


class Box:
    """One supervisor host: conf/run dirs, the curl stub's fixtures and log, a key pair."""

    def __init__(self, tmp):
        self.bash = shutil.which("bash")  # resolved: a bare "bash" on Windows is System32's WSL launcher
        need = {"bash": self.bash, "openssl": shutil.which("openssl"), "jq": shutil.which("jq")}
        missing = [k for k, v in need.items() if v is None]
        if shutil.which("flock") is None and os.environ.get("CI"):
            missing.append("flock")
        if missing:
            if os.environ.get("CI"):
                pytest.fail(f"{', '.join(missing)} not on PATH: the auth helper cannot run without them")
            pytest.skip(f"{', '.join(missing)} not on PATH (workstation)")
        self.t = tmp
        for d in ("bin", "conf", "run", "fx"):
            (tmp / d).mkdir()
        (tmp / "bin" / "curl").write_text(_CURL, encoding="utf-8", newline="\n")
        (tmp / "bin" / "jq").write_text(_JQ, encoding="utf-8", newline="\n")
        if shutil.which("flock") is None:
            (tmp / "bin" / "flock").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8", newline="\n")
        (tmp / "curl.log").write_text("", encoding="utf-8")
        (tmp / "conf" / "github.token").write_text("ghp_the_pat\n", encoding="utf-8")
        subprocess.run(["openssl", "genrsa", "-out", str(tmp / "key.pem"), "2048"], capture_output=True, check=True)
        subprocess.run(["openssl", "rsa", "-in", str(tmp / "key.pem"), "-pubout", "-out", str(tmp / "pub.pem")],
                       capture_output=True, check=True)
        p = lambda *x: tmp.joinpath(*x).as_posix()  # noqa: E731
        self.env = {**os.environ, "CI_RUNNER_CONF": p("conf"), "CI_RUNNER_RUN": p("run"),
                    "CI_RUNNER_GH_API": "http://gh", "OWNER": "octo", "CI_RUNNER_METRIC_PREFIX": "ci_",
                    "CURL_LOG": p("curl.log"), "CURL_FX": p("fx"), "REAL_JQ": need["jq"]}
        self.env.pop("CI_RUNNER_AUTH_HINT", None)
        self.prefix_path = p("bin")

    def app(self, inst: bool = True) -> None:
        shutil.copyfile(self.t / "key.pem", self.t / "conf" / "github-app.pem")
        (self.t / "conf" / "github-app.id").write_text(APP_ID + "\n", encoding="utf-8")
        if inst:
            (self.t / "conf" / "github-app.installation").write_text(INST + "\n", encoding="utf-8")

    def fx(self, method: str, path: str, code: int, body) -> None:
        text = body if isinstance(body, str) else json.dumps(body)
        (self.t / "fx" / _fx_name(method, path)).write_text(f"{code}\n{text}\n", encoding="utf-8", newline="\n")

    def mint(self, inst: str, token: str, left: int, code: int = 201) -> None:
        exp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + left))
        self.fx("POST", f"/app/installations/{inst}/access_tokens", code, {"token": token, "expires_at": exp})

    def cache(self, token: str, left: int) -> None:
        (self.t / "run" / "github-app.token").write_text(f"{int(time.time()) + left}\n{token}\n", encoding="utf-8",
                                                        newline="\n")

    def sh(self, body: str, **env: str) -> str:
        # the stub dir through bash's own pwd: a drive letter's colon would split PATH on Windows
        script = (f'export PATH="$(cd "{self.prefix_path}" && pwd):$PATH"\nmetrics=(); emit() {{ metrics+=("$1"); }}\n'
                  f'. "{HELPER}"\n' + body)
        r = subprocess.run([self.bash, "-c", script], capture_output=True, text=True, env={**self.env, **env})
        return r.stdout + r.stderr

    def auth(self, fresh: bool = False, **env: str) -> str:
        return self.sh('gh_auth; rc=$?\n'
                       + ('GH_AUTH_EXPIRES=$(( $(date +%s) + 60 )); gh_auth_fresh\n' if fresh else "")
                       + 'gh_auth_emit\necho "rc=${rc} mode=${GH_AUTH_MODE} configured=${GH_AUTH_CONFIGURED}'
                       ' token=${TOKEN}"\nprintf "%s\\n" "${metrics[@]}"\n', **env)

    def calls(self, needle: str) -> int:
        return sum(needle in line for line in (self.t / "curl.log").read_text(encoding="utf-8").splitlines())


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def _b64d(x: str) -> bytes:
    return base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))


def test_mint_signs_a_jwt_the_public_key_verifies_and_caches_0600(box):
    box.app()
    box.mint(INST, "ghs_minted_1", 3600)
    out = box.auth()
    assert "rc=0 mode=app configured=1 token=ghs_minted_1" in out
    assert (box.t / "curl.log").read_text(encoding="utf-8").strip() == f"POST /app/installations/{INST}/access_tokens"
    jwt = (box.t / "curl.log.auth").read_text(encoding="utf-8").strip()
    h, p, s = jwt.split(".")
    head, body = json.loads(_b64d(h)), json.loads(_b64d(p))
    assert head["alg"] == "RS256" and body["iss"] == APP_ID and body["exp"] - body["iat"] == 570
    assert body["iat"] <= time.time() - 25
    (box.t / "sig").write_bytes(_b64d(s))
    (box.t / "signed").write_bytes(f"{h}.{p}".encode())
    subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(box.t / "pub.pem"), "-signature",
                    str(box.t / "sig"), str(box.t / "signed")], capture_output=True, check=True)
    cached = box.t / "run" / "github-app.token"
    assert cached.read_text(encoding="utf-8").splitlines()[1] == "ghs_minted_1"
    if os.name != "nt":  # NTFS has no POSIX mode bits to read back
        assert oct(cached.stat().st_mode & 0o777) == "0o600"
    for g in ('ci_github_auth{mode="app"} 1', 'ci_github_auth{mode="pat"} 0', "ci_github_app_configured 1",
              "ci_github_app_token_expiry_timestamp_seconds "):
        assert g in out, g
    # the log names a digest, never the token, the JWT or the key
    assert f"minted installation {INST} token sha256:" in out
    assert out.count("ghs_minted_1") == 1  # the test's own token= line only
    assert jwt not in out and "PRIVATE KEY" not in out


def test_the_gauges_carry_the_callers_prefix(box):
    out = box.auth(CI_RUNNER_METRIC_PREFIX="acme_ci_")
    assert 'acme_ci_github_auth{mode="pat"} 1' in out and "acme_ci_github_app_configured 0" in out
    assert "\nci_github" not in out


def test_a_cached_token_with_time_left_is_reused_with_no_call(box):
    box.app()
    box.mint(INST, "ghs_minted_1", 3600)
    box.auth()
    assert "mode=app configured=1 token=ghs_minted_1" in box.auth()
    assert box.calls("access_tokens") == 1


def test_a_token_inside_the_margin_is_re_minted_and_one_outside_is_not(box):
    box.app()
    box.mint(INST, "ghs_minted_1", 3600)
    box.cache("ghs_old", 300)
    assert "token=ghs_minted_1" in box.auth()
    box.cache("ghs_kept", 660)
    assert "token=ghs_kept" in box.auth()
    assert box.calls("access_tokens") == 1


def test_gh_auth_fresh_re_mints_only_inside_the_margin(box):
    box.app()
    box.mint(INST, "ghs_minted_2", 3600)
    box.cache("ghs_cached", 3000)
    # `fresh` pretends 60 s are left; gh_auth_fresh finds the CACHE still good (another supervisor
    # refreshed it) and takes that, no mint
    assert "token=ghs_cached" in box.auth(fresh=True)
    assert box.calls("access_tokens") == 0
    box.cache("ghs_cached", 60)
    assert "token=ghs_minted_2" in box.auth(fresh=True)


def test_no_installation_file_looks_it_up_caches_it_and_a_404_mint_drops_it(box):
    box.app(inst=False)
    box.mint(INST, "ghs_minted_1", 3600)
    box.fx("GET", "/app/installations", 200, [{"id": 1, "account": {"login": "someone-else"}},
                                               {"id": int(INST), "account": {"login": "octo"}}])
    assert "mode=app configured=1 token=ghs_minted_1" in box.auth()
    assert (box.t / "run" / "github-app.installation").read_text(encoding="utf-8").strip() == INST
    (box.t / "run" / "github-app.token").unlink()
    box.auth()
    assert box.calls("GET /app/installations") == 1  # the cached id, not a second lookup
    (box.t / "fx" / _fx_name("POST", f"/app/installations/{INST}/access_tokens")).unlink()
    (box.t / "run" / "github-app.token").unlink()
    out = box.auth()
    assert "mode=pat configured=1 token=ghp_the_pat" in out and "mint refused (HTTP 404)" in out
    assert not (box.t / "run" / "github-app.installation").exists()


def test_no_installation_for_the_owner_is_the_pat_loudly(box):
    box.app(inst=False)
    box.fx("GET", "/app/installations", 200, [{"id": 1, "account": {"login": "someone-else"}}])
    out = box.auth()
    assert "mode=pat configured=1 token=ghp_the_pat" in out
    assert "ci-github-auth: mode=pat -- no installation for octo" in out


def test_an_unset_owner_finds_no_installation_and_says_so(box):
    box.app(inst=False)
    box.fx("GET", "/app/installations", 200, [{"id": int(INST), "account": {"login": "octo"}}])
    out = box.auth(OWNER="", CI_RUNNER_OWNER="")
    assert "mode=pat configured=1" in out and "no installation for  (GET /app/installations)" in out


def test_no_app_files_is_the_pat_said_on_stderr_and_in_the_gauges(box):
    out = box.auth()
    assert "rc=0 mode=pat configured=0 token=ghp_the_pat" in out
    assert "ci-github-auth: mode=pat -- no GitHub App configured" in out
    assert 'ci_github_auth{mode="pat"} 1' in out and "ci_github_app_configured 0" in out
    assert box.calls("") == 0


def test_a_refused_mint_is_the_pat_with_the_code_named_and_nothing_cached(box):
    box.app()
    box.mint(INST, "ghs_never", 3600, code=500)
    out = box.auth()
    assert "mode=pat configured=1 token=ghp_the_pat" in out
    assert f"installation {INST} token mint refused (HTTP 500)" in out
    assert not (box.t / "run" / "github-app.token").exists()
    assert not [p for p in (box.t / "run").iterdir() if "resp" in p.name]  # a 201's body never lingers


def test_a_key_openssl_cannot_sign_with_is_the_pat_and_no_call(box):
    box.app()
    (box.t / "conf" / "github-app.pem").write_text("not a key\n", encoding="utf-8")
    out = box.auth()
    assert "mode=pat configured=1 token=ghp_the_pat" in out and "JWT signing failed" in out
    assert box.calls("") == 0


def test_no_credential_at_all_is_mode_none_nonzero_both_gauges_0(box):
    (box.t / "conf" / "github.token").unlink()
    out = box.auth()
    assert "rc=1 mode=none configured=0 token=" in out
    assert 'ci_github_auth{mode="app"} 0' in out and 'ci_github_auth{mode="pat"} 0' in out


def test_a_stale_installation_file_404s_then_githubs_id_is_used_and_named(box):
    box.app()
    (box.t / "conf" / "github-app.installation").write_text("111\n", encoding="utf-8")
    box.mint(INST, "ghs_new_inst", 3600)
    box.fx("GET", "/app/installations", 200, [{"id": int(INST), "account": {"login": "octo"}}])
    out = box.auth()
    assert "mode=app configured=1 token=ghs_new_inst" in out
    assert f"installation 111 is gone (404); GitHub says octo's is {INST} -- re-place it\n" in out
    assert box.calls("POST /app/installations/111/") == 1
    # the caller's hint, when set, names the command that re-places it
    (box.t / "run" / "github-app.token").unlink()
    out = box.auth(CI_RUNNER_AUTH_HINT="installer --app-installation")
    assert f"-- re-place it: installer --app-installation {INST}\n" in out


def test_a_failed_refresh_keeps_a_still_valid_app_token(box):
    box.app()
    box.cache("ghs_still_good", 3000)
    out = box.sh('gh_auth\nrm "${CI_RUNNER_RUN}/github-app.token"\n'  # the refresh finds no cache; the mint 404s
                 'GH_AUTH_EXPIRES=$(( $(date +%s) + 300 ))\ngh_auth_fresh\n'
                 'echo "mode=${GH_AUTH_MODE} token=${TOKEN}"\n')
    assert "mode=app token=ghs_still_good" in out and "refresh failed, keeping the app token" in out


def test_retry_brings_a_caller_on_the_pat_back_to_the_app(box):
    box.app()
    box.mint(INST, "ghs_minted_1", 3600, code=500)
    fx = (box.t / "fx" / _fx_name("POST", f"/app/installations/{INST}/access_tokens")).as_posix()
    out = box.sh('gh_auth; echo "first=${GH_AUTH_MODE}"\n'
                 f'printf "201\\n{{\\"token\\":\\"ghs_back\\",\\"expires_at\\":\\"%s\\"}}\\n" '
                 f'"$(date -u -d "+1 hour" +%Y-%m-%dT%H:%M:%SZ)" > "{fx}"\n'
                 'gh_auth_fresh; echo "plain=${GH_AUTH_MODE}"\n'
                 'gh_auth_fresh retry; echo "retry=${GH_AUTH_MODE} token=${TOKEN}"\n')
    assert "first=pat" in out and "plain=pat" in out and "retry=app token=ghs_back" in out
