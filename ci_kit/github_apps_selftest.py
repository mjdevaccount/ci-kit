"""ci-app-audit's selftest: a synthetic App on an in-process fake GitHub, read the way the live run
reads (a real RS256 JWT, an installation token, pagination), held to bad = 1, good = 0, blind = 2.

`FakeGitHub` is also the must-fire suite's world (tests/test_github_apps_must_fire.py), so the
selftest and the suite cannot disagree about what GitHub answers.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import pathlib
import tempfile

from ci_kit import github_apps as ga

SLUG = "example-bot"
CLIENT_ID = "Iv23liEXAMPLE0000000"
APP_ID = 1000001
PERMS = {"contents": "write", "pull_requests": "write", "metadata": "read"}

MANIFEST = {"apps": [{
    "name": SLUG, "client_id": CLIENT_ID, "app_id": APP_ID,
    "default_permissions": {"contents": "write", "pull_requests": "write"},
    "default_events": [],
    "installations": [
        {"account": "example-org", "repository_selection": "selected", "repositories": ["repo-a", "repo-b"]},
        {"account": "example-user", "repository_selection": "all"},
    ],
}]}


def make_key() -> tuple[str, object]:
    """A throwaway RSA key (PEM text, public key object). Never a real App's."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()).decode()
    return pem, k.public_key()


class FakeGitHub:
    """GitHub's App endpoints, enough of them: it verifies the JWT, scopes and revokes tokens."""

    def __init__(self, public_key: object, repos_a: int = 2) -> None:
        self.public_key = public_key
        self.app = {"slug": SLUG, "id": APP_ID, "client_id": CLIENT_ID, "permissions": dict(PERMS), "events": []}
        self.installations = [
            {"id": 11, "account": {"login": "example-org"}, "repository_selection": "selected",
             "permissions": dict(PERMS), "events": [], "suspended_at": None},
            {"id": 12, "account": {"login": "example-user"}, "repository_selection": "all",
             "permissions": dict(PERMS), "events": [], "suspended_at": None},
        ]
        self.repos = {11: ["repo-a", "repo-b"][:repos_a] + [f"bulk-{i}" for i in range(max(0, repos_a - 2))]}
        self.tokens: dict[str, int] = {}
        self.revoked: list[str] = []
        self.scopes: list[dict] = []
        self.fail: str | None = None   # a path prefix that answers HTTP 500
        # PRIVATE Apps on the same account: GET /apps/<slug> answers only a live installation token
        # (GitHub: a workflow's GITHUB_TOKEN gets 403)
        self.private: dict[str, dict] = {}
        self.calls: list[str] = []

    def _jwt_ok(self, bearer: str) -> bool:
        import jwt
        try:
            claims = jwt.decode(bearer, self.public_key, algorithms=["RS256"], options={"verify_iat": False})
        except jwt.PyJWTError:
            return False
        return claims.get("iss") in (CLIENT_ID, str(APP_ID)) and claims["exp"] - claims["iat"] <= 600

    def __call__(self, method: str, path: str, bearer: str, body: dict | None) -> object:
        self.calls.append(f"{method} {path.split('?')[0]}")
        if self.fail and path.startswith(self.fail):
            raise ga.Blind(f"{method} {path}: HTTP 500")
        base, _, query = path.partition("?")
        q = dict(x.split("=", 1) for x in query.split("&") if x)
        page, per = int(q.get("page", 1)), int(q.get("per_page", 30))
        if base == "/app":
            if not self._jwt_ok(bearer):
                raise ga.Blind("GET /app: HTTP 401")
            return copy.deepcopy(self.app)
        if base == "/app/installations":
            if not self._jwt_ok(bearer):
                raise ga.Blind("GET /app/installations: HTTP 401")
            return copy.deepcopy(self.installations[(page - 1) * per:page * per])
        if base.startswith("/app/installations/") and base.endswith("/access_tokens"):
            if not self._jwt_ok(bearer):
                raise ga.Blind(f"POST {base}: HTTP 401")
            iid = int(base.split("/")[3])
            tok = f"ghs_fake{len(self.tokens)}"
            self.tokens[tok] = iid
            self.scopes.append(body or {})
            return {"token": tok}
        if base == "/installation/repositories":
            iid = self.tokens.get(bearer)
            if iid is None or bearer in self.revoked:
                raise ga.Blind("GET /installation/repositories: HTTP 401")
            names = self.repos.get(iid, [])
            return {"total_count": len(names),
                    "repositories": [{"name": n} for n in names[(page - 1) * per:page * per]]}
        if base == "/installation/token" and method == "DELETE":
            self.revoked.append(bearer)
            return {}
        if base.startswith("/apps/") and base[len("/apps/"):] in self.private:
            if bearer not in self.tokens or bearer in self.revoked:
                raise ga.Blind(f"GET {base}: HTTP 403")
            return copy.deepcopy(self.private[base[len("/apps/"):]])
        if base == f"/apps/{SLUG}":
            if not bearer:
                raise ga.Blind(f"GET {base}: HTTP 401")
            return {k: copy.deepcopy(v) for k, v in self.app.items()}
        raise ga.Blind(f"{method} {base}: HTTP 404")


OTHER = "other-bot"
OTHER_APP = {"slug": OTHER, "id": 7, "client_id": "Iv23liOTHER000000000", "permissions": {"actions": "read",
             "metadata": "read"}, "events": []}
WITH_OTHER = {"apps": [*MANIFEST["apps"], {"name": OTHER, "app_id": 7, "default_permissions": {"actions": "read"},
                                           "installations": [{"account": "example-org",
                                                              "repository_selection": "all"}]}]}


def run(manifest: dict | str, world: FakeGitHub, key_pem: str | None, *extra: str) -> tuple[int, str]:
    """One CLI run against `world`, the manifest and key written to a temp dir. (rc, output)."""
    with tempfile.TemporaryDirectory() as d:
        m = pathlib.Path(d) / "apps.json"
        m.write_text(manifest if isinstance(manifest, str) else json.dumps(manifest), encoding="utf-8")
        args = ["--manifest", str(m), *extra]
        if key_pem is not None:
            k = pathlib.Path(d) / "key.pem"
            k.write_text(key_pem, encoding="utf-8")
            args += ["--key-file", f"{SLUG}={k}"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ga.main(args, call=world)
        return rc, out.getvalue() + err.getvalue()


def selftest() -> list[str]:
    fails: list[str] = []
    pem, pub = make_key()
    w = FakeGitHub(pub)
    rc, out = run(MANIFEST, w, pem)
    if rc != 0:
        fails.append(f"the declared App read as itself was not clean: rc {rc} {out[-300:]}")
    if any(s != {"permissions": {"metadata": "read"}} for s in w.scopes) or sorted(w.revoked) != sorted(w.tokens):
        fails.append(f"installation tokens were not scoped to metadata:read and revoked: {w.scopes} {w.revoked}")
    if pem in out or any(t in out for t in w.tokens):
        fails.append("a key or token reached the output")
    w = FakeGitHub(pub)
    w.app["permissions"]["administration"] = "write"
    rc, out = run(MANIFEST, w, pem)
    if rc != 1 or "WIDENED administration" not in out:
        fails.append(f"a widened App permission was not drift: rc {rc}")
    w = FakeGitHub(pub)
    w.repos[11].append("repo-c")
    rc, out = run(MANIFEST, w, pem)
    if rc != 1 or "INSTALLED, not declared: repo-c" not in out:
        fails.append(f"an undeclared installed repository was not drift: rc {rc}")
    other, _ = make_key()
    rc, out = run(MANIFEST, FakeGitHub(pub), other)
    if rc != 2:
        fails.append(f"a key GitHub refuses was not could-not-look: rc {rc}")
    rc, out = run(MANIFEST, FakeGitHub(pub), None)
    if rc != 2:
        fails.append(f"no key and no --app-level was not could-not-look: rc {rc}")
    w = FakeGitHub(pub)
    w.private[OTHER] = copy.deepcopy(OTHER_APP)
    rc, out = run(WITH_OTHER, w, pem, "--app-level", OTHER, "--app-level-token-from", SLUG)
    if rc != 0 or sorted(w.revoked) != sorted(w.tokens) or "(installation 11), revoked" not in out:
        fails.append(f"a private App was not read with the keyed App's revoked reader token: rc {rc} {out[-300:]}")
    w.private[OTHER]["permissions"]["administration"] = "write"
    rc, out = run(WITH_OTHER, w, pem, "--app-level", OTHER, "--app-level-token-from", SLUG)
    if rc != 1 or "App other-bot: permission WIDENED administration" not in out:
        fails.append(f"a private App's widened permission, read app-level, was not drift: rc {rc}")
    w = FakeGitHub(pub)
    rc, out = run(MANIFEST, w, None, "--validate")
    if rc != 0 or w.calls:
        fails.append(f"--validate on a valid manifest was not 0 with nothing called: rc {rc} {w.calls}")
    rc, out = run({"apps": [{"name": "a", "app_id": 1, "installations": [], "permisions": {}}]}, w, None, "--validate")
    if rc != 1 or "INVALID" not in out:
        fails.append(f"--validate on a manifest with a typo key was not 1: rc {rc}")
    return fails
