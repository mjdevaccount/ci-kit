"""The push token: a GitHub App installation token or the caller's PAT, and WHO it is (v1.37.0).

ci-kit's token jobs (pr-autoupdate, merge-train, ci-green, pr-heal's claim/push/report) pushed,
commented and merged with the caller's fine-grained PAT. A GitHub App (the reusables' `app-client-id`
input and `app-private-key` secret, v1.42.0) replaces it. ONE composite action
(`.github/actions/pr-token/action.yml`) mints the App token with `actions/create-github-app-token`
(no JWT code here), falls back to the PAT, and records which one each run used. This module is its
decision and its record, so both are unit-tested; the composite only wires it.

STAGED (`mode`, the reusables' `token-source` input, else the caller repo's `PR_TOKEN_SOURCE`
variable, else `pat`). A consumer may already hold the App's id and key, so keying on their
presence alone would cut every consumer over the moment `v1` moved:
  pat           the PAT acts, exactly as before. If the caller ALSO passes the key, the App token
                is minted anyway and NOT used: a canary that proves the key before the flip, and
                the App's login joins the trusted set (a rollback from `app` keeps its markers).
                A failed canary mint is a `::warning::`, never a red: the PAT is what acts.
  app           the App acts when its id AND key are present; either absent -> the PAT, with the
                reason (`var-absent` | `key-absent`). Configured but the MINT FAILS -> the job
                FAILS: a broken key must not hide behind a PAT that still works.
  app-required  the App or nothing: absent id or key FAILS too (the PAT is never used).

IDENTITY. An installation token cannot call `GET /user` (403), and that is where every "who am I"
read used to go (pr_baseretry.whoami, merge_train.ops.me). The App's login comes from the mint's
`app-slug` output (`<slug>[bot]`, the REST spelling: no User login can contain brackets, so it
cannot be registered by a person); the PAT's from `GET /user` with the PAT. The tools get
`CI_KIT_TOKEN_LOGIN` (the identity that acts) and `CI_KIT_TRUSTED_LOGINS` (it plus the OTHER
configured identity), so a marker or a train written under either side of a flip is honoured.
The acting identity unreadable = the step FAILS where a tool trusts markers with it
(IDENTITY_SCOPES), never "trust nobody" or "trust anybody"; in the other jobs nothing reads it,
so it is a `::warning::` and the record says `login=unknown`.

THE RECORD: one line per token job, as a `::notice` (in `gh run view --log` and the check run's
annotations API) and in the step summary, in ONE fixed shape -- a CONTRACT, a detector parses it:
  pr-token: source=app|pat|none mode=<mode> reason=<REASONS> app=<minted|failed|absent> repo=<o/r>
            workflow=<file> job=<id> login=<login|unknown>          (one line; wrapped here)
Every value is free of `=` and spaces. The decide step's shell writes `reason=decide-crashed`
itself when this module cannot run at all.

READING IT BACK (v1.53.0). The grammar's other direction lives here too, so producer and parser
cannot drift apart: `parse()` turns one record into a `Record` or refuses it with `RecordError`
(STRICT: an unknown, missing or duplicated key, an empty value, a value holding `=`, whitespace or a
`${` template, a `source` / `app` / `reason` outside its set -- each is refused, never skipped);
`classify()` names what a parsed record says went wrong (`REASON_CLASS` classifies every reason);
`records_in_log()` / `records_in_annotations()` find the record lines where the composite prints
them. `ci-pr-token-check` is the same over a log or annotation dump on stdin.

DEPRECATED NAMES. The App's id and key were first read under consumer-specific names (a repository
variable and a secret); the generic names are the reusables' `app-client-id` input and
`app-private-key` secret (v1.42.0), and this composite's `client-id` (v1.40.0; create-github-app-token
deprecated its own `app-id` the same way, and `app-id` stays an alias here). The old spellings still
resolve to the SAME value, and each one used is a `::notice` naming its replacement (`deprecations`):
never a different decision, never a different record line. Removing them is a major version. The
reusables hand the composite BOTH spellings (`deprecated-client-id` / `deprecated-private-key`, read
only when the generic input is empty) and the composite alone resolves them, so the precedence
(generic first) and the notice live in one place, not in every caller.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

MODES = ("pat", "app", "app-required")
# THE PERMISSION TABLE, per job `scope`: the App token asks for exactly these, on this repository only.
# .github/actions/pr-token/action.yml holds them as expressions; tests/test_pr_token_must_fire.py
# holds that file to this spec. Every grant is one the PAT's documented scopes already held.
PERMISSIONS: dict[str, dict[str, str]] = {
    "update": {"contents": "write", "workflows": "write", "pull-requests": "write", "issues": "write",
               "actions": "write", "statuses": "write", "checks": "read", "administration": "read"},
    "train": {"contents": "write", "workflows": "write", "pull-requests": "write", "issues": "write",
              "actions": "write", "statuses": "write", "checks": "read"},
    "push": {"contents": "write", "workflows": "write"},
    "comment": {"pull-requests": "write", "issues": "write"},
}
# Every `reason=` value the record can carry (docs/pr-ops/push-token.md "THE RECORD" lists the same; a test holds both
# to this, and to every reason this module and the composite's shell actually emit).
REASONS = ("configured", "mode-pat", "var-absent", "key-absent", "mode-invalid", "scope-invalid",
           "mint-failed", "identity-unreadable", "decide-crashed")
# The scopes whose job hands CI_KIT_TOKEN_LOGIN / CI_KIT_TRUSTED_LOGINS to a tool that trusts markers
# with them (pr-autoupdate's base-retry, merge-train's authority): there an unreadable identity is
# red. ci-green and pr-heal read neither (a test holds this set to the reusables' env).
IDENTITY_SCOPES = frozenset({"update", "train"})
LOGIN_ENV = "CI_KIT_TOKEN_LOGIN"
TRUSTED_ENV = "CI_KIT_TRUSTED_LOGINS"
PREFIX = "pr-token:"
# NOT the record's title (`pr-token`): a detector grepping the record must never match a notice
DEPRECATED_TITLE = "ci-kit deprecated name"
#: the composite's env flag -> the deprecated spelling it stood in for, and its replacement. Each flag
#: is true exactly when the mint's `||` chain fell through to that spelling (action.yml, decide step).
DEPRECATED = {
    "APP_ID_ALIAS": ("app-id", "client-id"),                            # the composite's own input
    "DEPRECATED_CLIENT_ID": ("EDGE_BOT_APP_ID", "app-client-id"),       # a reusable's repo variable
    "DEPRECATED_PRIVATE_KEY": ("EDGE_BOT_PRIVATE_KEY", "app-private-key"),  # a reusable's secret
}


@dataclass(frozen=True)
class Decision:
    mint: bool      # run create-github-app-token
    source: str     # app | pat | none (none = the job fails)
    reason: str     # one of REASONS (the record's `reason=`)
    error: str = ""  # non-empty = fail the job with this message


def decide(mode: str, has_app_id: bool, has_key: bool, has_pat: bool) -> Decision:
    """Which token acts, whether to mint, and why. Pure: the policy the tests pin."""
    mode = (mode or "pat").strip().lower()
    if mode not in MODES:
        return Decision(False, "none", "mode-invalid", f"token-source `{mode}` is not one of {', '.join(MODES)}")
    configured = has_app_id and has_key
    missing = "var-absent" if not has_app_id else "key-absent" if not has_key else ""
    if mode == "app-required":
        if not configured:
            return Decision(False, "none", missing, f"token-source app-required but the App is not configured ({missing}): "
                            "pass the App's key as the `app-private-key` secret and its client id as the `app-client-id` input; "
                            "the PAT is never used in this mode")
        return Decision(True, "app", "configured")
    if mode == "app" and configured:
        return Decision(True, "app", "configured")
    reason = "mode-pat" if mode == "pat" else missing
    if not has_pat:
        return Decision(configured, "none", reason, "secret `token` is empty and the App is not acting "
                        f"({reason}): nothing is pushed. Map the PAT onto `token`, or opt in with token-source app")
    return Decision(configured, "pat", reason)  # pat mode with the App configured: a canary mint


def bot_login(slug: str) -> str:
    """The App's login as REST reports it (comments' user.login, a PR's user.login). '' = unknown."""
    slug = (slug or "").strip()
    return f"{slug}[bot]" if slug else ""


def trusted_set(acting: str, others: list[str]) -> list[str]:
    """The acting identity first, then every OTHER configured identity, deduplicated. Pure."""
    out: list[str] = []
    for x in [acting, *others]:
        if x and x not in out:
            out.append(x)
    return out


def _field(value: str, empty: str) -> str:
    """One record value: every whitespace character (newlines included) and `=` -> `_`, so the line
    stays ONE line of space-separated key=value tokens whatever the caller passed. The decide step's
    shell fallback spells `mode` the same way (lowercase, `[[:space:]=]` -> `_`)."""
    return "".join("_" if c.isspace() or c == "=" else c for c in (value or "").strip()) or empty


def line(d: Decision, mode: str, app: str, repo: str, workflow: str, job: str, login: str) -> str:
    """The record, one fixed greppable shape. Pure."""
    fields = {"source": _field(d.source, "none"), "mode": _field((mode or "").lower(), "pat"),
              "reason": _field(d.reason, "?"), "app": _field(app, "absent"), "repo": _field(repo, "?"),
              "workflow": _field(workflow, "?"), "job": _field(job, "?"), "login": _field(login, "unknown")}
    return " ".join([PREFIX, *(f"{k}={v}" for k, v in fields.items())])


# ── THE RECORD, read back: the grammar's other direction (v1.53.0) ───────────────────────────────
#: the record's keys, in the order line() writes them (a test holds line() to this)
KEYS = ("source", "mode", "reason", "app", "repo", "workflow", "job", "login")
SOURCES = ("app", "pat", "none")
APPS = ("minted", "failed", "absent")
#: every reason, classified on purpose (a test holds the keys to REASONS). ok = the token acted as
#: configured; fallback = the App is not configured (a finding only where the mode asked for it);
#: failure = the job could not act, or could not say who acted.
REASON_CLASS = {"configured": "ok", "mode-pat": "ok", "var-absent": "fallback", "key-absent": "fallback",
                "mode-invalid": "failure", "scope-invalid": "failure", "mint-failed": "failure",
                "identity-unreadable": "failure", "decide-crashed": "failure"}
TITLE = "pr-token"  # the annotation title the record is printed under
_TOKEN = re.compile(r"^([a-z]+)=([^\s=]+)$")
# A log line from `gh run view --log` (job <TAB> step <TAB> stamp) or a raw runner log: only a line
# ANCHORED on the workflow command's rendering counts -- the runner echoes the composite's own
# script (which holds the record's template) into the log, and an unanchored grep reads that echo.
_LOG = re.compile(r"^(?:[^\t]*\t[^\t]*\t)?(?:\d{4}-\d\d-\d\dT[\d:.]+Z )?##\[(?:notice|warning|error)\](pr-token:.*)$")


class RecordError(ValueError):
    """A line that is not a valid record. str() is the reason it was refused."""


@dataclass(frozen=True)
class Record:
    source: str
    mode: str
    reason: str
    app: str
    repo: str
    workflow: str
    job: str
    login: str

    def asdict(self) -> dict[str, str]:
        return {k: getattr(self, k) for k in KEYS}


def parse(text: str) -> Record:
    """One record line -> Record, or RecordError naming why not. Pure and STRICT: never a partial
    record, never a skip. `text` is the record itself (an annotation message, or a log line with
    its `##[level]` prefix removed: records_in_log)."""
    if "${" in text:
        raise RecordError("template text (contains ${), not a record")
    s = text.strip()
    if "\n" in s or "\r" in s:
        raise RecordError("more than one line")
    if not s.startswith(PREFIX + " "):
        raise RecordError(f"does not start with '{PREFIX} '")
    got: dict[str, str] = {}
    for tok in s[len(PREFIX) + 1:].split(" "):
        m = _TOKEN.match(tok)
        if not m:
            raise RecordError(f"bad token {tok!r} (want key=value, no space or = in the value)")
        k, v = m.groups()
        if k not in KEYS:
            raise RecordError(f"unknown key {k!r}")
        if k in got:
            raise RecordError(f"duplicated key {k!r}")
        got[k] = v
    missing = [k for k in KEYS if k not in got]
    if missing:
        raise RecordError(f"missing key(s) {', '.join(missing)}")
    for k, allowed in (("source", SOURCES), ("app", APPS), ("reason", REASONS)):
        if got[k] not in allowed:
            raise RecordError(f"{k}={got[k]} is not one of {', '.join(allowed)} "
                              "(a newer ci-kit wrote it? read it with that version)")
    return Record(**got)


def classify(rec: Record, repo: str | None = None) -> list[str]:
    """What a parsed record says went wrong (empty = the token acted as configured). Pure.
    `repo` = the repository it was read from (a record naming another one is a finding).
    Not findings: app=absent in pat mode (no key passed yet), and login=unknown outside the
    identity scopes (decide/report already fail the job where a tool would trust it)."""
    f: list[str] = []
    cls = REASON_CLASS[rec.reason]
    if cls == "failure":
        f.append(f"reason={rec.reason} source={rec.source}: the token job could not act as configured")
    if rec.app == "failed":
        f.append(f"app=failed (mode={rec.mode}): the App mint FAILED -- fix the key or installation "
                 "before this repository uses token-source app")
    if rec.mode not in MODES and rec.reason != "mode-invalid":
        f.append(f"mode={rec.mode} is not one of {', '.join(MODES)}")
    if rec.mode in ("app", "app-required") and rec.source != "app" and cls != "failure":
        f.append(f"mode={rec.mode} but source={rec.source} (reason={rec.reason}): asked for the App, did not get it")
    if rec.mode == "pat" and rec.source == "app":
        f.append("source=app in mode=pat: decide() never does this -- contract violation")
    elif rec.source == "app" and (rec.app != "minted" or rec.reason != "configured" or rec.login == "unknown"):
        f.append(f"source=app with app={rec.app} reason={rec.reason} login={rec.login}: the App acted without a "
                 "minted token, a configured reason or an identity -- contract violation")
    if rec.mode == "pat" and rec.source == "pat" and rec.reason != "mode-pat":
        f.append(f"source=pat in mode=pat with reason={rec.reason}: decide() only says mode-pat -- contract violation")
    if rec.source == "none" and not f:
        f.append(f"source=none (reason={rec.reason}): no token acted")
    if repo is not None and rec.repo.lower() != repo.lower():
        f.append(f"record names repo={rec.repo}, read from {repo}")
    return f


def records_in_log(log: str) -> list[str]:
    """The record lines in a job log (`gh run view --log` shape, or the raw log), the `##[level]`
    prefix removed. Pure. Only an ANCHORED line counts, and never one holding a `${` template."""
    out = []
    for ln in log.splitlines():
        m = _LOG.match(ln.rstrip("\r"))
        if m and "${" not in m.group(1):
            out.append(m.group(1))
    return out


def records_in_annotations(annotations: list[dict]) -> list[str]:
    """The record messages among a check run's annotations (`/check-runs/{id}/annotations`). Pure.
    Other `pr-token`-titled annotations (the canary warning, the trusted-identities notice) are
    prose, not records."""
    return [str(a.get("message") or "") for a in annotations
            if isinstance(a, dict) and a.get("title") == TITLE
            and str(a.get("message") or "").startswith(PREFIX) and "${" not in str(a.get("message"))]


# ── what the tools read (pr_baseretry, merge_train) ─────────────────────────────────────────────
def acting_login(env: dict[str, str] | None = None) -> str:
    """The acting identity the composite resolved ('' = not run under it: the caller reads /user)."""
    return ((os.environ if env is None else env).get(LOGIN_ENV) or "").strip()


def trusted_logins(acting: str, env: dict[str, str] | None = None) -> frozenset[str]:
    """Whose markers count: the acting identity + CI_KIT_TRUSTED_LOGINS. Nothing when the acting
    identity is unknown (never "anybody")."""
    if not acting:
        return frozenset()
    raw = (os.environ if env is None else env).get(TRUSTED_ENV) or ""
    return frozenset(trusted_set(acting, [x.strip() for x in raw.split(",") if x.strip()]))


def deprecations(used: list[str]) -> list[str]:
    """One `::notice` line per deprecated spelling that supplied a value (`used` = keys of
    DEPRECATED, in order; an unknown key is named as it is, never dropped). Pure."""
    out = []
    for flag in dict.fromkeys(used):
        old, new = DEPRECATED.get(flag, (flag, ""))
        what = f"`{old}` is deprecated: use `{new}`" if new else f"a deprecated name was used ({old})"
        out.append(f"::notice title={DEPRECATED_TITLE}::{what} -- identical behaviour today; the old name is "
                   "removed in the next major version")
    return out


# ── the composite's two steps ────────────────────────────────────────────────────────────────────
def _out(**kv: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    text = "".join(f"{k}={v}\n" for k, v in kv.items())
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)


def _summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"`{text}`\n\n")


def _workflow() -> str:
    """The CALLER's workflow file (no spaces: greppable), from GITHUB_WORKFLOW_REF."""
    ref = os.environ.get("GITHUB_WORKFLOW_REF", "").split("@", 1)[0]
    return ref.rsplit("/", 1)[-1] or os.environ.get("GITHUB_WORKFLOW", "")


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == "true"


def _pat_login() -> str:
    """GET /user with the PAT (CI_KIT_PAT, never argv). '' = unreadable, no PAT, or no `gh`."""
    pat = os.environ.get("CI_KIT_PAT", "")
    gh = shutil.which("gh")  # resolved explicitly (PATHEXT on Windows); absent = unreadable, not a crash
    if not pat or not gh:
        return ""
    env = {**os.environ, "GH_TOKEN": pat}
    env.pop("GITHUB_TOKEN", None)
    try:
        r = subprocess.run([gh, "api", "user", "--jq", ".login"], capture_output=True, text=True, env=env, check=False)
    except OSError:
        return ""
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def cmd_decide() -> int:
    for n in deprecations([f for f in DEPRECATED if _flag(f)]):
        print(n)
    mode = os.environ.get("CI_KIT_TOKEN_MODE", "")
    d = decide(mode, _flag("HAS_APP_ID"), _flag("HAS_KEY"), _flag("HAS_PAT"))
    scope = os.environ.get("CI_KIT_TOKEN_SCOPE", "")
    if scope not in PERMISSIONS and not d.error:  # an unknown scope would mint a token with NO permission
        d = Decision(False, "none", "scope-invalid", f"scope `{scope}` is not one of {', '.join(PERMISSIONS)}")
    _out(mint=str(d.mint).lower(), source=d.source, reason=d.reason, mode=(mode or "pat").strip().lower())
    if d.error:
        rec = line(d, mode, "absent", os.environ.get("GITHUB_REPOSITORY", ""), _workflow(), os.environ.get("GITHUB_JOB", ""), "")
        print(f"::notice title=pr-token::{rec}")
        _summary(rec)
        print(f"::error title=pr-token::{d.error}")
        return 1
    return 0


def resolve(d: Decision, mint_outcome: str, slug: str, pat_login: str,
            needs_identity: bool = True) -> tuple[str, list[str], str, str]:
    """(acting login, trusted logins, app= field, error). Pure given the two identity reads.
    `needs_identity` False (a scope outside IDENTITY_SCOPES): an unreadable PAT login is acting ''
    with nothing trusted, not an error -- no tool in that job trusts a marker."""
    app = {"success": "minted", "failure": "failed"}.get(mint_outcome, "absent")
    app_login = bot_login(slug) if app == "minted" else ""
    if d.source == "app":
        if app != "minted" or not app_login:
            return "", [], app, "the App token was not minted (or named no app-slug): failing rather than using the PAT"
        return app_login, trusted_set(app_login, [pat_login]), app, ""
    if not pat_login:
        if not needs_identity:
            return "", [], app, ""
        return "", [], app, "the PAT's identity is unreadable (GET /user): no marker could be trusted, refusing to act"
    return pat_login, trusted_set(pat_login, [app_login]), app, ""


def cmd_report() -> int:
    """After the mint (or its skip): resolve WHO acts, write the outputs and the record."""
    mode = os.environ.get("CI_KIT_TOKEN_MODE", "")
    d = Decision(_flag("MINT"), os.environ.get("SOURCE", ""), os.environ.get("REASON", ""))
    outcome = os.environ.get("MINT_OUTCOME", "")
    needs = os.environ.get("CI_KIT_TOKEN_SCOPE", "") not in PERMISSIONS.keys() - IDENTITY_SCOPES  # unknown: needs it
    acting, trusted, app, err = resolve(d, outcome, os.environ.get("APP_SLUG", ""), _pat_login(), needs)
    if err:
        d = Decision(d.mint, "none", "mint-failed" if d.source == "app" else "identity-unreadable")
    rec = line(d, mode, app, os.environ.get("GITHUB_REPOSITORY", ""), _workflow(), os.environ.get("GITHUB_JOB", ""), acting)
    print(f"::notice title=pr-token::{rec}")
    _summary(rec)
    if d.source == "pat" and app == "failed":
        print("::warning title=pr-token::the App canary mint FAILED (the PAT acts, mode=pat): fix the App key or "
              "installation before flipping this repo to token-source app")
    if d.source != "none" and not acting:
        print("::warning title=pr-token::the PAT's identity is unreadable (GET /user): acting as login=unknown -- "
              "no tool in this job trusts markers, so the token is still handed out")
    # Only where the second identity was EXPECTED (an App mode, or a canary that failed): without the
    # key in pat mode one identity is the normal state, and an always-present notice says nothing.
    if d.source != "none" and acting and len(trusted) < 2 and (d.reason != "mode-pat" or app == "failed"):
        print(f"::notice title=pr-token::trusted identities: {', '.join(trusted)} only -- markers written by the other "
              "token identity (before or after a flip) are NOT honoured in this run")
    if err:
        print(f"::error title=pr-token::{err}")
        return 1
    _out(login=acting, trusted=",".join(trusted))
    return 0


def check(text: str, fmt: str = "log", repo: str | None = None) -> tuple[list[dict], list[str]]:
    """Records found in `text` (parsed, as dicts) and every finding: unparseable records, what
    classify() says, and NO record at all (a token step that ran must print one). Pure."""
    if fmt == "annotations":
        data = json.loads(text)
        if isinstance(data, list) and data and all(isinstance(x, list) for x in data):
            data = [a for page in data for a in page]  # `gh api --paginate --slurp`: a list of pages
        if not isinstance(data, list):  # an API error body ({"message": ...}) is a failed read, not "no record"
            raise ValueError(f"annotations input is a JSON {type(data).__name__}, not a list")
        lines = records_in_annotations(data)
    else:
        lines = records_in_log(text) if fmt == "log" else [x for x in text.splitlines() if x.strip()]
    recs: list[dict] = []
    findings: list[str] = []
    for ln in lines:
        try:
            rec = parse(ln)
        except RecordError as e:
            findings.append(f"unparseable record ({e}): {ln[:160]}")
            continue
        recs.append(rec.asdict())
        findings += classify(rec, repo)
    if not lines:
        findings.append("no pr-token record in the input: the token step did not say which token acted")
    return recs, findings


def check_main(argv: list[str] | None = None) -> int:
    """ci-pr-token-check: read records from a log, an annotations JSON array, or one record per
    line; print each record as JSON and each finding. Exit 0 clean, 1 findings, 2 could not read."""
    ap = argparse.ArgumentParser(prog="ci-pr-token-check", description=check_main.__doc__.split("\n", 1)[0])
    ap.add_argument("file", nargs="?", default="-", help="input (default stdin)")
    ap.add_argument("--format", dest="fmt", choices=("log", "annotations", "lines"), default="log",
                    help="log: `gh run view --log` output; annotations: the check-run annotations JSON; "
                         "lines: one record per line")
    ap.add_argument("--repo", help="the repository the input was read from (owner/name)")
    a = ap.parse_args(argv)
    try:
        if a.file == "-":
            text = sys.stdin.read()
        else:
            with open(a.file, encoding="utf-8") as fh:
                text = fh.read()
        recs, findings = check(text, a.fmt, a.repo)
    except (OSError, UnicodeDecodeError, ValueError) as e:
        print(f"ci-pr-token-check: COULD NOT LOOK: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    for r in recs:
        print(json.dumps(r, sort_keys=True))
    for x in findings:
        print(f"FINDING: {x}")
    return 1 if findings else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ci_kit.pr_token", description=__doc__.split("\n", 1)[0])
    ap.add_argument("step", choices=("decide", "report", "check"))
    a, rest = ap.parse_known_args(argv)
    if a.step == "check":
        return check_main(rest)
    if rest:
        ap.error(f"unrecognized arguments: {' '.join(rest)}")
    return cmd_decide() if a.step == "decide" else cmd_report()


if __name__ == "__main__":
    sys.exit(main())
