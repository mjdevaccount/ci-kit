"""The push token: a GitHub App installation token or the caller's PAT, and WHO it is (v1.37.0).

ci-kit's token jobs (pr-autoupdate, merge-train, ci-green, pr-heal's claim/push/report) pushed,
commented and merged with the caller's fine-grained PAT. A GitHub App (`EDGE_BOT_APP_ID` variable,
`EDGE_BOT_PRIVATE_KEY` secret) replaces it. ONE composite action
(`.github/actions/pr-token/action.yml`) mints the App token with `actions/create-github-app-token`
(no JWT code here), falls back to the PAT, and records which one each run used. This module is its
decision and its record, so both are unit-tested; the composite only wires it.

STAGED (`mode`, the reusables' `token-source` input, else the caller repo's `PR_TOKEN_SOURCE`
variable, else `pat`). Every repo already holds the App's variable and key, so keying on their
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
The acting identity unreadable = the step FAILS, never "trust nobody" or "trust anybody".

THE RECORD: one line per token job, as a `::notice` (in `gh run view --log` and the check run's
annotations API) and in the step summary, in ONE fixed shape a detector can grep:
  pr-token: source=app|pat|none mode=<mode> reason=<why> app=<minted|failed|absent> repo=<o/r> workflow=<file> job=<id> login=<login>
"""
from __future__ import annotations

import argparse
import os
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
LOGIN_ENV = "CI_KIT_TOKEN_LOGIN"
TRUSTED_ENV = "CI_KIT_TRUSTED_LOGINS"
PREFIX = "pr-token:"


@dataclass(frozen=True)
class Decision:
    mint: bool      # run create-github-app-token
    source: str     # app | pat | none (none = the job fails)
    reason: str     # configured | mode=pat | var-absent | key-absent | ... (the record's `reason=`)
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
                            "pass EDGE_BOT_PRIVATE_KEY and set the EDGE_BOT_APP_ID variable; the PAT is never used in this mode")
        return Decision(True, "app", "configured")
    if mode == "app" and configured:
        return Decision(True, "app", "configured")
    reason = "mode=pat" if mode == "pat" else missing
    if not has_pat:
        return Decision(configured, "none", reason, "secret `token` is empty and the App is not acting "
                        f"({reason}): nothing is pushed. Map the PAT onto `token`, or opt in with token-source app")
    return Decision(configured, "pat", reason)  # pat mode with the App configured: a canary mint


def bot_login(slug: str) -> str:
    """The App's login as REST reports it (comments' user.login, a PR's user.login). '' = unknown."""
    slug = (slug or "").strip()
    return f"{slug}[bot]" if slug else ""


def rest_login(login: str, typename: str = "") -> str:
    """ONE spelling for comparisons: GraphQL names an App `renovate` / `mjdev-edge-bot` with
    __typename Bot, REST `renovate[bot]`. Only a Bot gets the suffix, so a User who registered
    the bare slug never compares equal to the App. Pure."""
    login = (login or "").strip()
    if typename == "Bot" and login and not login.endswith("[bot]"):
        return f"{login}[bot]"
    return login


def trusted_set(acting: str, others: list[str]) -> list[str]:
    """The acting identity first, then every OTHER configured identity, deduplicated. Pure."""
    out: list[str] = []
    for x in [acting, *others]:
        if x and x not in out:
            out.append(x)
    return out


def line(d: Decision, mode: str, app: str, repo: str, workflow: str, job: str, login: str) -> str:
    """The record, one fixed greppable shape. Pure."""
    return (f"{PREFIX} source={d.source} mode={(mode or 'pat').strip().lower()} reason={d.reason} app={app} "
            f"repo={repo or '?'} workflow={workflow or '?'} job={job or '?'} login={login or 'unknown'}")


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
    """GET /user with the PAT (CI_KIT_PAT, never argv). '' = unreadable or no PAT."""
    pat = os.environ.get("CI_KIT_PAT", "")
    if not pat:
        return ""
    env = {**os.environ, "GH_TOKEN": pat}
    env.pop("GITHUB_TOKEN", None)
    try:
        r = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True, env=env, check=False)
    except OSError:
        return ""
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def cmd_decide() -> int:
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


def resolve(d: Decision, mint_outcome: str, slug: str, pat_login: str) -> tuple[str, list[str], str, str]:
    """(acting login, trusted logins, app= field, error). Pure given the two identity reads."""
    app = {"success": "minted", "failure": "failed"}.get(mint_outcome, "absent")
    app_login = bot_login(slug) if app == "minted" else ""
    if d.source == "app":
        if app != "minted" or not app_login:
            return "", [], app, "the App token was not minted (or named no app-slug): failing rather than using the PAT"
        return app_login, trusted_set(app_login, [pat_login]), app, ""
    if not pat_login:
        return "", [], app, "the PAT's identity is unreadable (GET /user): no marker could be trusted, refusing to act"
    return pat_login, trusted_set(pat_login, [app_login]), app, ""


def cmd_report() -> int:
    """After the mint (or its skip): resolve WHO acts, write the outputs and the record."""
    mode = os.environ.get("CI_KIT_TOKEN_MODE", "")
    d = Decision(_flag("MINT"), os.environ.get("SOURCE", ""), os.environ.get("REASON", ""))
    outcome = os.environ.get("MINT_OUTCOME", "")
    acting, trusted, app, err = resolve(d, outcome, os.environ.get("APP_SLUG", ""), _pat_login())
    if err:
        d = Decision(d.mint, "none", "mint-failed" if d.source == "app" else "identity-unreadable")
    rec = line(d, mode, app, os.environ.get("GITHUB_REPOSITORY", ""), _workflow(), os.environ.get("GITHUB_JOB", ""), acting)
    print(f"::notice title=pr-token::{rec}")
    _summary(rec)
    if d.source == "pat" and app == "failed":
        print("::warning title=pr-token::the App canary mint FAILED (the PAT acts, mode=pat): fix the App key or "
              "installation before flipping this repo to token-source app")
    if d.source != "none" and len(trusted) < 2:
        print(f"::notice title=pr-token::trusted identities: {', '.join(trusted)} only -- markers written by the other "
              "token identity (before or after a flip) are NOT honoured in this run")
    if err:
        print(f"::error title=pr-token::{err}")
        return 1
    _out(login=acting, trusted=",".join(trusted))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ci_kit.pr_token", description=__doc__.split("\n", 1)[0])
    ap.add_argument("step", choices=("decide", "report"))
    a = ap.parse_args(argv)
    return cmd_decide() if a.step == "decide" else cmd_report()


if __name__ == "__main__":
    sys.exit(main())
