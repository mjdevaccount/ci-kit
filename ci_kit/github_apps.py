"""ci-app-audit: a GitHub App's permissions, events and installations, DECLARED, and held to live.

WHY. A GitHub App's grants change on its settings page, and an installation's repositories change
on the installation page: neither leaves a commit, a failed run or a log line anywhere a repository
reads. A permission widened by hand (Administration read -> write) or a repository quietly added to
an installation works, so nothing ever turns red. This makes the App's shape a file under review,
and the difference from live a red run.

THE MANIFEST (YAML, or JSON: the GitHub App manifest's own keys where they fit):

    apps:
      - name: example-bot                # the App's slug (GET /app .slug)
        client_id: Iv23liEXAMPLE         # the JWT issuer; or app_id: 123456 (either, or both)
        default_permissions:             # exactly the App's grants; metadata: read is implicit
          contents: write
          pull_requests: write
        default_events: []               # the webhook events it subscribes to
        installations:                   # every installation, by account; [] = installed nowhere
          - account: example-org
            repository_selection: selected   # or all
            repositories: [repo-a, repo-b]   # selected only: exactly these
            permissions: {contents: read}    # optional: this installation's own, when it differs

`url`, `hook_attributes`, `redirect_url`, `callback_urls`, `setup_url`, `description`, `public`,
`request_oauth_on_install` and `setup_on_update` (GitHub's manifest keys) are accepted and not
audited; any other key is a typo and the run cannot judge the file (exit 2).

THE LIVE READ, per App:
  * with its private key (`--key-file NAME=PATH` or `--key-env NAME=VAR`, never argv): a JWT
    (RS256 via PyJWT, iat now-60s, exp now+9m) reads `GET /app` and `GET /app/installations`; each
    `selected` installation's repositories through an installation token scoped to metadata:read,
    revoked after use. Nothing prints a key, a JWT or a token.
  * without one: `GET /apps/<slug>` with `--token-env` (default GH_TOKEN, then GITHUB_TOKEN) reads
    the App level (permissions, events) only. Its installations are then NOT looked at, which is
    exit 2 -- unless `--app-level NAME` says so on purpose, and the output names it every run.
    A PRIVATE App answers GET /apps/<slug> only to an installation or user token (GITHUB_TOKEN:
    403): `--app-level-token-from KEYED` mints one from an App audited here with its key (scoped to
    metadata:read, revoked after), so no long-lived token is stored for it (v1.55.0).

`--validate` (v1.55.0) is the pre-merge half: the manifest parsed exactly as the audit parses it,
and the schedule's App flags held to it (each names a declared App; every App has a key flag or
--app-level; the reader App has a key flag), with no key read and no network call. Invalid = 1,
unreadable = 2.

DRIFT (exit 1), one line each: a permission widened or narrowed (read < write < admin; added or
dropped counts), an event added or dropped, an installation on an undeclared account or a declared
one missing, a suspended installation, `all` vs `selected`, a repository installed but undeclared
or declared but not installed, and an installation whose permissions differ from its declaration
(by default the App's: a changed grant an owner has not accepted yet shows here).

COULD NOT LOOK (exit 2): the manifest unreadable or invalid, a key unreadable or refused, the key
or the slug naming another App, any API read failing, a declared installation set no read reached.

    ci-app-audit --manifest ci/github-apps.yml [--key-file NAME=PATH] [--key-env NAME=VAR]
                 [--token-env VAR] [--app-level NAME] [--app-level-token-from NAME] [--only NAME]
                 [--validate] [--selftest]

Libraries checked (v1.53.0): probot/settings and github/safe-settings manage repository and
organization settings, not an App's own grants; the Terraform GitHub provider manages installation
repository membership (state, no audit) and has no resource for App permissions; octokit/app.js and
PyGithub are clients. PyJWT signs; the read and the comparison are this module.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import pathlib
import sys
import time
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

PROG = "ci-app-audit"
API = "https://api.github.com"
LEVEL = {"read": 1, "write": 2, "admin": 3}
SELECTIONS = ("all", "selected")
APP_KEYS = {"name", "app_id", "client_id", "default_permissions", "default_events", "installations"}
GITHUB_MANIFEST_KEYS = {"url", "hook_attributes", "redirect_url", "callback_urls", "setup_url", "description",
                        "public", "request_oauth_on_install", "setup_on_update"}
INSTALL_KEYS = {"account", "repository_selection", "repositories", "permissions"}
IMPLICIT = {"metadata": "read"}  # every App holds it; declaring it is allowed, omitting it is not drift
PER_PAGE = 100


class Blind(Exception):
    """Could not look: the run ends with exit 2."""


class Invalid(Blind):
    """The manifest itself is wrong. An audit cannot judge live against it (exit 2); `--validate`,
    whose subject IS the manifest, reports it as the finding (exit 1)."""


# ── the declaration ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Install:
    account: str
    selection: str
    repositories: frozenset[str] = frozenset()
    permissions: dict[str, str] | None = None


@dataclass(frozen=True)
class AppSpec:
    name: str
    app_id: int | None
    client_id: str | None
    permissions: dict[str, str]
    events: frozenset[str]
    installations: tuple[Install, ...]

    @property
    def issuer(self) -> str:
        return self.client_id or str(self.app_id)


def _perms(raw: object, where: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise Invalid(f"{where}: permissions must be a mapping, got {type(raw).__name__}")
    out = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str) or v not in LEVEL:
            raise Invalid(f"{where}: permission {k!r}: {v!r} is not one of {', '.join(LEVEL)}")
        out[k] = v
    return {**IMPLICIT, **out}


def _install(raw: object, where: str) -> Install:
    if not isinstance(raw, dict):
        raise Invalid(f"{where}: an installation must be a mapping")
    extra = set(raw) - INSTALL_KEYS
    if extra:
        raise Invalid(f"{where}: unknown key(s) {', '.join(sorted(extra))}")
    acct, sel = raw.get("account"), raw.get("repository_selection")
    if not isinstance(acct, str) or not acct:
        raise Invalid(f"{where}: `account` is required")
    if sel not in SELECTIONS:
        raise Invalid(f"{where}: repository_selection {sel!r} is not one of {', '.join(SELECTIONS)}")
    repos = raw.get("repositories")
    if sel == "all" and repos is not None:
        raise Invalid(f"{where}: repository_selection all takes no `repositories` list")
    if sel == "selected" and (not isinstance(repos, list) or not all(isinstance(r, str) and r for r in repos)):
        raise Invalid(f"{where}: repository_selection selected needs a `repositories` list of names")
    perms = _perms(raw["permissions"], where) if "permissions" in raw else None
    return Install(acct, sel, frozenset(r.lower() for r in repos or ()), perms)


def parse_manifest(data: object) -> list[AppSpec]:
    """A loaded manifest -> its Apps. Strict: anything it cannot read exactly is Blind."""
    if not isinstance(data, dict) or not isinstance(data.get("apps"), list) or not data["apps"]:
        raise Invalid("the manifest needs a non-empty `apps` list")
    out: list[AppSpec] = []
    for i, raw in enumerate(data["apps"]):
        where = f"apps[{i}]"
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not raw["name"]:
            raise Invalid(f"{where}: `name` (the App's slug) is required")
        where = f"apps[{i}] {raw['name']}"
        extra = set(raw) - APP_KEYS - GITHUB_MANIFEST_KEYS
        if extra:
            raise Invalid(f"{where}: unknown key(s) {', '.join(sorted(extra))}")
        app_id, client_id = raw.get("app_id"), raw.get("client_id")
        if app_id is None and client_id is None:
            raise Invalid(f"{where}: `client_id` or `app_id` is required (the JWT issuer)")
        if app_id is not None and (not isinstance(app_id, int) or isinstance(app_id, bool)):
            raise Invalid(f"{where}: app_id must be an integer")
        if client_id is not None and (not isinstance(client_id, str) or not client_id):
            raise Invalid(f"{where}: client_id must be a string")
        events = raw.get("default_events", [])
        if not isinstance(events, list) or not all(isinstance(e, str) for e in events):
            raise Invalid(f"{where}: default_events must be a list of names")
        if "installations" not in raw or not isinstance(raw["installations"], list):
            raise Invalid(f"{where}: `installations` is required (a list; [] = installed nowhere)")
        installs = tuple(_install(x, f"{where} installations[{j}]") for j, x in enumerate(raw["installations"]))
        accounts = [x.account.lower() for x in installs]
        if len(set(accounts)) != len(accounts):
            raise Invalid(f"{where}: an account is declared twice")
        out.append(AppSpec(raw["name"], app_id, client_id, _perms(raw.get("default_permissions", {}), where),
                           frozenset(events), installs))
    names = [a.name for a in out]
    if len(set(names)) != len(names):
        raise Invalid("an App is declared twice")
    return out


def load_manifest(path: pathlib.Path) -> list[AppSpec]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise Blind(f"manifest {path}: {type(e).__name__}: {e}") from e
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(text)
        else:
            import yaml  # the `apps` extra
            data = yaml.safe_load(text)
    except ImportError as e:
        raise Blind("reading a YAML manifest needs PyYAML (install ci-kit[apps])") from e
    except Exception as e:  # noqa: BLE001 -- JSONDecodeError, yaml.YAMLError: the file is not a manifest
        raise Invalid(f"manifest {path}: {type(e).__name__}: {e}") from e
    return parse_manifest(data)


# ── the live read ────────────────────────────────────────────────────────────────────────────────
Transport = Callable[[str, str, str, "dict | None"], object]  # (method, path, bearer, body) -> JSON


def urllib_transport(method: str, path: str, bearer: str, body: dict | None = None) -> object:
    """One GitHub REST call over urllib (the kit's stdlib HTTP, as migration_open_prs.http)."""
    req = urllib.request.Request(f"{API}{path}", method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {bearer}", "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": PROG,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - fixed https API host
            raw = r.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as e:
        raise Blind(f"{method} {path}: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
        raise Blind(f"{method} {path}: {type(e).__name__}: {e}") from e


def app_jwt(issuer: str, key_pem: str, now: float | None = None) -> str:
    """The App's JWT. Never printed, never on argv."""
    try:
        import jwt  # PyJWT, the `apps` extra
    except ImportError as e:
        raise Blind("minting an App JWT needs PyJWT (install ci-kit[apps])") from e
    t = int(time.time() if now is None else now)
    try:
        return jwt.encode({"iat": t - 60, "exp": t + 540, "iss": issuer}, key_pem, algorithm="RS256")
    except Exception as e:  # noqa: BLE001 -- a key PyJWT cannot sign with: say so, never echo it
        raise Blind(f"the private key could not sign ({type(e).__name__})") from e


def _pages(call: Transport, path: str, bearer: str, key: str | None) -> list[dict]:
    out: list[dict] = []
    for page in range(1, 1000):
        sep = "&" if "?" in path else "?"
        got = call("GET", f"{path}{sep}per_page={PER_PAGE}&page={page}", bearer, None)
        items = got.get(key) if key and isinstance(got, dict) else got
        if not isinstance(items, list):
            raise Blind(f"GET {path}: not a list ({type(items).__name__})")
        out += [x for x in items if isinstance(x, dict)]
        if len(items) < PER_PAGE:
            return out
    raise Blind(f"GET {path}: more than 1000 pages")


@dataclass
class Live:
    slug: str
    app_id: int | None
    client_id: str | None
    permissions: dict[str, str]
    events: frozenset[str]
    installations: list[dict] | None = None  # None = not read (app level only)
    repos: dict[int, frozenset[str]] = field(default_factory=dict)  # installation id -> names (selected ones)
    unrevoked: list[int] = field(default_factory=list)  # installations whose scoped token DELETE failed


def read_app_level(call: Transport, slug: str, token: str) -> Live:
    got = call("GET", f"/apps/{slug}", token, None)
    if not isinstance(got, dict) or not isinstance(got.get("permissions"), dict):
        raise Blind(f"GET /apps/{slug}: no permissions in the answer")
    return Live(str(got.get("slug") or ""), got.get("id"), got.get("client_id"), dict(got["permissions"]),
                frozenset(got.get("events") or ()))


def read_with_key(call: Transport, spec: AppSpec, key_pem: str, now: float | None = None) -> Live:
    bearer = app_jwt(spec.issuer, key_pem, now)
    got = call("GET", "/app", bearer, None)
    if not isinstance(got, dict) or not isinstance(got.get("permissions"), dict):
        raise Blind("GET /app: no permissions in the answer")
    live = Live(str(got.get("slug") or ""), got.get("id"), got.get("client_id"), dict(got["permissions"]),
                frozenset(got.get("events") or ()))
    if live.slug != spec.name:
        raise Blind(f"the key for {spec.name} is the key of App {live.slug or '?'}: nothing of {spec.name} was read")
    live.installations = _pages(call, "/app/installations", bearer, None)
    for inst in live.installations:
        if inst.get("repository_selection") != "selected" or inst.get("suspended_at"):
            continue
        iid = inst.get("id")
        tok = call("POST", f"/app/installations/{iid}/access_tokens", bearer, {"permissions": {"metadata": "read"}})
        token = tok.get("token") if isinstance(tok, dict) else None
        if not token:
            raise Blind(f"installation {iid}: no token in the answer")
        try:
            names = _pages(call, "/installation/repositories", token, "repositories")
        finally:
            try:
                call("DELETE", "/installation/token", token, None)
            except Exception:  # noqa: BLE001 - ANY failed revoke is a note; the read stands
                live.unrevoked.append(iid)  # said in the notes: it expires in an hour (metadata: read only)
        live.repos[iid] = frozenset(str(r.get("name") or "").lower() for r in names)
    return live


def mint_reader(call: Transport, spec: AppSpec, key_pem: str, now: float | None = None,
                prefer: frozenset[str] = frozenset()) -> tuple[str, int]:
    """(token, installation id): an installation token of the keyed App `spec`, scoped to
    metadata: read, for the app-level reads. GET /apps/<slug> answers a PRIVATE App only to an
    installation or user token, never to a workflow's GITHUB_TOKEN (HTTP 403), and this needs no
    long-lived token stored anywhere. The caller revokes it. `prefer`: the accounts the app-level
    Apps are declared on; an installation on one of them is taken first (a private App is visible
    on its own account). Never printed."""
    bearer = app_jwt(spec.issuer, key_pem, now)
    me = call("GET", "/app", bearer, None)
    slug = me.get("slug") if isinstance(me, dict) else None
    if slug != spec.name:
        raise Blind(f"the key for {spec.name} is the key of App {slug or '?'}")
    insts = [i for i in _pages(call, "/app/installations", bearer, None) if not i.get("suspended_at")]
    if not insts:
        raise Blind(f"{spec.name} has no unsuspended installation to mint a reader token on")
    same = [i for i in insts if str((i.get("account") or {}).get("login") or "").lower() in prefer]
    iid = (same or insts)[0].get("id")
    tok = call("POST", f"/app/installations/{iid}/access_tokens", bearer, {"permissions": {"metadata": "read"}})
    token = tok.get("token") if isinstance(tok, dict) else None
    if not token:
        raise Blind(f"installation {iid}: no token in the answer")
    return token, iid


# ── the comparison ───────────────────────────────────────────────────────────────────────────────
def perm_drift(declared: dict[str, str], live: dict[str, str], where: str) -> list[str]:
    """Every permission that differs, named widened or narrowed. Pure."""
    declared, live = {**IMPLICIT, **declared}, {**IMPLICIT, **live}
    out = []
    for k in sorted(set(declared) | set(live)):
        d, got = declared.get(k), live.get(k)
        if d == got:
            continue
        if LEVEL.get(got or "", 0) > LEVEL.get(d or "", 0):
            out.append(f"{where}: permission WIDENED {k}: declared {d or 'none'}, live {got}")
        else:
            out.append(f"{where}: permission narrowed {k}: declared {d}, live {got or 'none'}")
    return out


def compare(spec: AppSpec, live: Live) -> list[str]:
    """Every difference between the declaration and what was read. Pure."""
    f: list[str] = []
    app = f"App {spec.name}"
    if live.slug != spec.name:
        f.append(f"{app}: live slug is {live.slug or '?'}")
    if spec.app_id is not None and live.app_id is not None and live.app_id != spec.app_id:
        f.append(f"{app}: live app_id {live.app_id}, declared {spec.app_id}")
    if spec.client_id and live.client_id and live.client_id != spec.client_id:
        f.append(f"{app}: live client_id {live.client_id}, declared {spec.client_id}")
    f += perm_drift(spec.permissions, live.permissions, app)
    for e in sorted(live.events - spec.events):
        f.append(f"{app}: event ADDED {e} (not declared)")
    for e in sorted(spec.events - live.events):
        f.append(f"{app}: event dropped {e} (declared, not subscribed)")
    if live.installations is None:
        return f
    declared = {x.account.lower(): x for x in spec.installations}
    seen: set[str] = set()
    for inst in live.installations:
        acct = str((inst.get("account") or {}).get("login") or "?")
        key, where = acct.lower(), f"{app} on {acct}"
        if key in seen:
            f.append(f"{where}: installed twice")
            continue
        seen.add(key)
        d = declared.get(key)
        if d is None:
            f.append(f"{where}: installation NOT DECLARED (id {inst.get('id')}, {inst.get('repository_selection')})")
            continue
        if inst.get("suspended_at"):
            f.append(f"{where}: installation suspended since {inst['suspended_at']}")
            continue
        sel = inst.get("repository_selection")
        if sel != d.selection:
            f.append(f"{where}: repository_selection {sel}, declared {d.selection}")
        elif sel == "selected":
            got = live.repos.get(inst.get("id"))
            if got is None:
                raise Blind(f"{where}: the installation's repositories were not read")
            f += [f"{where}: repository INSTALLED, not declared: {r}" for r in sorted(got - d.repositories)]
            f += [f"{where}: repository declared, not installed: {r}" for r in sorted(d.repositories - got)]
        want = d.permissions if d.permissions is not None else spec.permissions
        f += perm_drift(want, dict(inst.get("permissions") or {}), f"{where} (installation)")
        events = frozenset(inst.get("events") or ())
        if events != spec.events:
            f.append(f"{where}: installation events {sorted(events)}, declared {sorted(spec.events)}")
    f += [f"{app} on {d.account}: declared installation MISSING" for k, d in declared.items() if k not in seen]
    return f


# ── the CLI ──────────────────────────────────────────────────────────────────────────────────────
def _pairs(values: list[str], flag: str) -> dict[str, str]:
    out = {}
    for v in values:
        name, sep, rest = v.partition("=")
        if not sep or not name or not rest:
            raise Blind(f"{flag} wants NAME=VALUE, got {v.split('=', 1)[0]!r}=...")
        out[name] = rest
    return out


def _key(name: str, files: dict[str, str], envs: dict[str, str]) -> str | None:
    if name in files:
        try:
            return pathlib.Path(files[name]).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise Blind(f"{name}: key file unreadable ({type(e).__name__})") from e
    if name in envs:
        pem = os.environ.get(envs[name], "")
        if not pem.strip():
            raise Blind(f"{name}: key env {envs[name]} is empty")
        return pem
    return None


def _crash(e: BaseException) -> str:
    """An unexpected exception, named by type and WHERE only: its message and locals may hold a
    value read from GitHub, so neither is printed (a crash is exit 2, never drift)."""
    tb = traceback.extract_tb(e.__traceback__)
    at = f" at {pathlib.Path(tb[-1].filename).name}:{tb[-1].lineno}" if tb else ""
    return f"CRASHED {type(e).__name__}{at}"


def audit_one(spec: AppSpec, key: str | None, token: str, app_level: set[str], call: Transport,
              now: float | None = None, token_error: str | None = None) -> tuple[list[str], list[str]]:
    """One App -> (drift findings, notes). Raises Blind when it could not be read. `token_error`:
    why there is no app-level token (the reader mint failed); it blinds the app-level Apps only."""
    notes: list[str] = []
    if key:
        live = read_with_key(call, spec, key, now)
        n_repos = sum(len(v) for v in live.repos.values())
        notes.append(f"App {spec.name}: read with its key: {len(live.installations or [])} installation(s), "
                     f"{n_repos} selected repo(s)")
        notes += [f"App {spec.name}: installation {i}'s scoped read token was NOT revoked (DELETE failed; "
                  "it expires within the hour, metadata: read only)" for i in live.unrevoked]
    else:
        if spec.name not in app_level:
            raise Blind(f"App {spec.name}: no key, so its installations cannot be read (pass --key-file/--key-env, "
                        f"or --app-level {spec.name} to audit its permissions and events only)")
        if token_error:
            raise Blind(f"App {spec.name}: {token_error}")
        if not token:
            raise Blind(f"App {spec.name}: no key and no token for GET /apps/{spec.name}")
        live = read_app_level(call, spec.name, token)
        notes.append(f"App {spec.name}: APP LEVEL ONLY (no key here): permissions and events read, "
                     f"{len(spec.installations)} declared installation(s) NOT looked at")
    return compare(spec, live), notes


def audit(specs: list[AppSpec], keys: dict[str, str | None], token: str, app_level: set[str],
          call: Transport, now: float | None = None,
          token_error: str | None = None) -> tuple[list[str], list[str], list[str]]:
    """(drift findings, notes, blind). Each App is read on its own: one App unread is a `blind`
    line (the run exits 2) and never hides another App's findings."""
    findings: list[str] = []
    notes: list[str] = []
    blind: list[str] = []
    for spec in specs:
        try:
            f, n = audit_one(spec, keys.get(spec.name), token, app_level, call, now, token_error)
        except Blind as e:
            blind.append(f"App {spec.name}: {e}" if not str(e).startswith(f"App {spec.name}") else str(e))
            continue
        except Exception as e:  # noqa: BLE001 - a crash reading one App is that App unread, exit 2
            blind.append(f"App {spec.name}: {_crash(e)}")
            continue
        findings += f
        notes += n
    return findings, notes, blind


def _reader(call: Transport, specs: list[AppSpec], keys: dict[str, str | None], app_level: set[str],
            name: str) -> tuple[str, int | None, str | None]:
    """--app-level-token-from NAME -> (token, installation id, why not). Minted only when an App is
    actually read app-level; a mint that fails blinds those Apps alone, never the keyed one."""
    spec = next((s for s in specs if s.name == name), None)
    if spec is None or not keys.get(name):
        raise Blind(f"--app-level-token-from {name}: that App is not audited here with its key")
    if not any(s.name in app_level and not keys.get(s.name) for s in specs):
        return "", None, None
    try:
        prefer = frozenset(x.account.lower() for s in specs if s.name in app_level and not keys.get(s.name)
                           for x in s.installations)
        token, iid = mint_reader(call, spec, keys[name] or "", prefer=prefer)
    except Blind as e:
        return "", None, f"no app-level reader token from {name}: {e}"
    except Exception as e:  # noqa: BLE001 - a crash minting is those Apps unread, exit 2
        return "", None, f"no app-level reader token from {name}: {_crash(e)}"
    return token, iid, None


def _flags_cover(specs: list[AppSpec], keyed: set[str], app_level: set[str], token_from: str | None) -> list[str]:
    """What the audit would refuse in these flags, decided without a key or a call. Pure. Every App
    needs a key flag or --app-level (else it is unread), and the reader App needs a key flag."""
    out = [f"App {s.name}: no key flag and no --app-level names it, so the audit cannot read it"
           for s in specs if s.name not in keyed | app_level]
    if token_from is not None and token_from not in keyed:
        out.append(f"--app-level-token-from {token_from!r}: no key flag names that App, so no reader can be minted")
    return out


def _validate(path: pathlib.Path, specs: list[AppSpec], keyed: set[str], app_level: set[str],
              token_from: str | None) -> int:
    if keyed or app_level or token_from is not None:  # the flags the schedule passes: hold them too
        problems = _flags_cover(specs, keyed, app_level, token_from)
        if problems:
            raise Invalid("; ".join(problems))
    print(f"{PROG}: manifest {path} is valid: {len(specs)} App(s), "
          f"{sum(len(s.installations) for s in specs)} installation(s) declared (nothing read from GitHub)")
    return 0


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=PROG, description=__doc__.split("\n", 1)[0])
    ap.add_argument("--manifest", type=pathlib.Path)
    ap.add_argument("--key-file", action="append", default=[], metavar="NAME=PATH")
    ap.add_argument("--key-env", action="append", default=[], metavar="NAME=VAR")
    ap.add_argument("--token-env", action="append", default=[], metavar="VAR",
                    help="token for app-level reads (default GH_TOKEN, then GITHUB_TOKEN)")
    ap.add_argument("--app-level", action="append", default=[], metavar="NAME",
                    help="audit this App's permissions and events only (its installations are not read)")
    ap.add_argument("--app-level-token-from", metavar="NAME",
                    help="read the app-level Apps with a metadata:read installation token of this keyed App, "
                         "revoked after (a private App is hidden from GITHUB_TOKEN)")
    ap.add_argument("--only", action="append", default=[], metavar="NAME")
    ap.add_argument("--validate", action="store_true",
                    help="check the manifest's shape only: no key read, no network (invalid = 1, unreadable = 2)")
    ap.add_argument("--selftest", action="store_true")
    return ap


def main(argv: list[str] | None = None, call: Transport = urllib_transport) -> int:
    a = _parser().parse_args(argv)
    if a.selftest:
        from ci_kit import github_apps_selftest
        fails = github_apps_selftest.selftest()
        for x in fails:
            print(f"{PROG} selftest: {x}", file=sys.stderr)
        print(f"{PROG} selftest: {'FAILED' if fails else 'ok'}")
        return 1 if fails else 0
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    reader: tuple[str, int | None] = ("", None)
    notes: list[str] = []
    try:
        if a.manifest is None:
            raise Blind("--manifest is required")
        specs = load_manifest(a.manifest)
        if a.only:
            unknown = set(a.only) - {s.name for s in specs}
            if unknown:
                raise Invalid(f"--only names App(s) the manifest does not declare: {', '.join(sorted(unknown))}")
            specs = [s for s in specs if s.name in a.only]
        files, envs = _pairs(a.key_file, "--key-file"), _pairs(a.key_env, "--key-env")
        stray = (set(files) | set(envs) | set(a.app_level) | {a.app_level_token_from} - {None}) \
            - {s.name for s in specs}
        if stray:
            raise Invalid("a key, --app-level or --app-level-token-from flag names App(s) not audited: "
                          f"{', '.join(repr(x) for x in sorted(stray))}")
        if a.app_level_token_from and a.token_env:
            raise Blind("--app-level-token-from and --token-env both name the app-level token: pass one")
        if a.validate:
            # names checked above; no key is read, nothing is called
            return _validate(a.manifest, specs, set(files) | set(envs), set(a.app_level), a.app_level_token_from)
        keys = {s.name: _key(s.name, files, envs) for s in specs}
        token_error = None
        if a.app_level_token_from:
            token, iid, token_error = _reader(call, specs, keys, set(a.app_level), a.app_level_token_from)
            reader = (token, iid)
        else:
            token = next((os.environ[v] for v in (a.token_env or ["GH_TOKEN", "GITHUB_TOKEN"])
                          if os.environ.get(v)), "")
        try:
            findings, notes, blind = audit(specs, keys, token, set(a.app_level), call, token_error=token_error)
        finally:
            notes = notes + _revoke(call, *reader, a.app_level_token_from)
    except Invalid as e:
        if not a.validate:
            return _blind(str(e))
        print(f"::error title={PROG}::{e}" if in_actions else f"INVALID {e}")
        _summary([f"**{PROG}: the manifest is invalid** -- {e}"])
        return 1
    except Blind as e:
        return _blind(str(e))
    except Exception as e:  # noqa: BLE001 - a crash is "could not look", never drift
        return _blind(_crash(e))
    return _report(specs, findings, notes, blind, in_actions)


def _report(specs: list[AppSpec], findings: list[str], notes: list[str], blind: list[str],
            in_actions: bool) -> int:
    for n in notes:
        print(n)
    for x in findings:
        print(f"::error title={PROG}::{x}" if in_actions else f"DRIFT {x}")
    for x in blind:
        print(f"{PROG}: COULD NOT LOOK: {x}", file=sys.stderr)
    print(f"{PROG}: apps={len(specs)} findings={len(findings)} unread={len(blind)}")
    _summary([f"**{PROG}**: {len(specs)} App(s), {len(findings)} drift finding(s), {len(blind)} App(s) unread",
              *notes, *findings, *(f"could not look: {x}" for x in blind)])
    return 2 if blind else 1 if findings else 0


def _revoke(call: Transport, token: str, iid: int | None, name: str | None) -> list[str]:
    """Revoke the app-level reader token, if one was minted -> the note that says what happened."""
    if not token:
        return []
    try:
        call("DELETE", "/installation/token", token, None)
    except Exception:  # noqa: BLE001 - a revoke that failed in ANY way is said; the report stands
        return [f"the app-level reader token ({name}, installation {iid}, metadata: read) was NOT revoked "
                "(DELETE failed; it expires within the hour)"]
    return [f"app-level reads used a metadata:read installation token of {name} (installation {iid}), revoked"]


def _blind(why: str) -> int:
    print(f"{PROG}: COULD NOT LOOK: {why}", file=sys.stderr)
    _summary([f"**{PROG}: could not look** -- {why}"])
    return 2


def _summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n\n".join(lines) + "\n\n")


if __name__ == "__main__":
    sys.exit(main())
