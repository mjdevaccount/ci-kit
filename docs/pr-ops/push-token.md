# The push token: GitHub App or PAT

`ci_kit/pr_token.py` + `.github/actions/pr-token/action.yml` (v1.37.0).

A push made with `GITHUB_TOKEN` fires no workflow, so the token jobs (pr-autoupdate, merge-train,
ci-green, pr-heal's claim/push/report, relock's push) push, comment and merge with the caller's
token. That was always the PAT in `token`. A GitHub App installation token works too, and does not
expire with a person's PAT. Every token job gets its token from ONE composite that mints a GitHub
App installation token (`actions/create-github-app-token`, SHA-pinned; this repo only, the job's
permissions only) or falls back to the PAT, and names the acting identity
(`CI_KIT_TOKEN_LOGIN`) plus the other configured one (`CI_KIT_TRUSTED_LOGINS`) to base-retry and
the merge train, whose markers and trains survive a flip either way.

The App's id is the reusable input `app-client-id`, its key the secret `app-private-key` (generic
names since v1.42.0, below). Which one ACTS is `token-source` (a reusable input), else the caller's
`PR_TOKEN_SOURCE` repository variable, else `pat`:

| mode | acts | App absent | App mint fails |
|---|---|---|---|
| `pat` (default) | the PAT; the App, if its key is passed, is minted with `metadata: read` ONLY and NOT used (a canary) | PAT | `::warning::`, PAT acts |
| `app` | the App | PAT, `reason=var-absent` / `key-absent` | **red**: a broken key never hides behind the PAT |
| `app-required` | the App; `token` is never read | **red** | **red** |

**The canary** is on by default, not opt-in: it is the only thing that proves the key AND the
App's installation in a repo BEFORE that repo flips (opt-in would mean it never runs), and a
`pr-token: ... app=failed` warning across repos is the early signal. It is write-free: the mint
asks for `metadata: read` and nothing else, never NO permission input, which
`create-github-app-token` answers with the installation's WHOLE permission set. Cost: one API call
per job.

## Staged rollout

Each stage is config in the CALLER repo, no ci-kit change:

1. **Flip one repo.** Its caller stanzas map the App's key onto `app-private-key` under `secrets:`
   (pr-autoupdate, merge-train, ci-green, pr-heal, relock) and pass `app-client-id`, then set the
   repo variable `PR_TOKEN_SOURCE=app` (`gh variable set PR_TOKEN_SOURCE -R owner/repo --body app`).
   Until the variable is set the runs read `source=pat ... app=minted`: the key is proven before
   the flip. Rollback: delete the variable. Keep the key mapping when you do: the PAT run learns
   the App's login from the canary, so a caller that drops the key on rollback stops trusting the
   App's base-retry markers and trains (its record line then reads `app=absent`).
2. **Flip the default.** The same edits in every caller repo (the variable per repo: a personal
   account has no organisation variables). A repo without them stays on the PAT.
3. **Make the PAT fail loud.** `PR_TOKEN_SOURCE=app-required` per repo: an App that is absent or
   cannot mint is a red run, and `token` is no longer read; then the PAT secret can be retired.
   Markers and trains written under the PAT are still honoured only while `token` is passed (its
   login is read from it); after that they are history.

## Identity

The App acts as `<app-slug>[bot]` (REST spelling; GraphQL says `<app-slug>` with `__typename`
Bot). An installation token cannot call `GET /user`, so the composite resolves the login (the
mint's `app-slug`; `GET /user` with the PAT for the PAT) and passes it as `CI_KIT_TOKEN_LOGIN`,
with the other configured identity in `CI_KIT_TRUSTED_LOGINS`: base-retry markers and open merge
trains written by either side of a flip are honoured, nobody else's. An unreadable acting identity
is a red run where a tool trusts markers with it (scopes `update` and `train`: base-retry, the
merge train); in ci-green and pr-heal nothing reads it, so it is a `::warning::`, `login=unknown`,
and the token is still handed out. Train commits keep each rider as author; the committer is the
acting identity (`<id>+<slug>[bot]@users.noreply.github.com` for the App). Heal commits stay
`github-actions[bot]` whichever token pushes them.

## The record

THE RECORD: every token job prints ONE line, as a `::notice` (an `::error` when the decide step
could not run at all) and in the step summary:
`pr-token: source=app|pat|none mode=<mode> reason=<configured|mode-pat|var-absent|key-absent|mode-invalid|scope-invalid|mint-failed|identity-unreadable|decide-crashed> app=<minted|failed|absent> repo=<owner/repo> workflow=<file> job=<id> login=<login|unknown>`.

Its grammar is a CONTRACT: a detector parses it. Fields are space-separated `key=value` in this
order and no value holds `=` or a space; a new reason or field is a ci-kit release, listed here
(`tests/test_pr_token_must_fire.py` holds this list to what the code can emit). Read it from
outside with `gh run view <run> --log | grep 'pr-token: '` or the job's check-run annotations
(`gh api repos/<owner>/<repo>/check-runs/<job id>/annotations`); the step summary is for humans (no
REST endpoint serves it).

## The pr-token composite's input names (v1.40.0)

The composite (`.github/actions/pr-token`) takes the App's id as `client-id` (its client id, or app
id: `actions/create-github-app-token`'s own name). `app-id` is a DEPRECATED alias: a caller that
passes `app-id` + `private-key` exactly as before gets the same App id and key, the same decision
and the same `pr-token:` record line, plus one `::notice` titled `ci-kit deprecated name` (never
the record's `pr-token` title). Two more optional inputs, `deprecated-client-id` and
`deprecated-private-key`, are read only when `client-id`/`app-id` resp. `private-key` are empty,
each use a notice; generic first. The aliases are removed in v2.

`ci_kit.pr_admit --pool` also accepts a runs-on JSON label list, read as its LAST label, or a JSON
string; a value that names no label (malformed JSON, `[]`, a non-string item, quoted text) is
refused with an `::error` and exit 2, and the review then runs (v1.42.0).

## Generic names in the reusables (v1.42.0)

The token reusables (pr-autoupdate, merge-train, ci-green, pr-heal, relock) take the App as the
`app-client-id` INPUT (its client id, or app id) and the `app-private-key` SECRET; the caller maps
whatever it calls them. The older spellings, the repository variable `EDGE_BOT_APP_ID` and the
secret `EDGE_BOT_PRIVATE_KEY`, are DEPRECATED aliases: a caller that sets only them gets the same
values and the same decision, plus one `::notice` per alias used; when both are set the generic
name wins. Under `secrets: inherit` the old secret passes as is (the names match). They are removed
in v2. Each reusable hands both spellings to the pr-token composite, which alone resolves them
(`deprecated-client-id` / `deprecated-private-key`).

### Pools

Pools are inputs too, each a JSON label list read with `fromJSON`:

- `secure-runner`: the jobs that hold a write token (pr-autoupdate, merge-train, ci-green,
  pr-heal, relock, review-gate);
- `sealed-runner`: the job that holds the model credential AND runs PR code (pr-heal,
  review-gate); its LAST label names the pool to the scheduler.

Precedence: the input; else the caller repo's `CI_SECURE_RUNNER` / `CI_SEALED_RUNNER` variable,
an ON/OFF switch for the kit's default labels (`["self-hosted","home-secure"]` /
`["self-hosted","home-sealed"]`; a variable never carries labels, so a typo there cannot move a
token job onto another pool); else `ubuntu-latest`. The secure pool runs in the caller's
`ci-secure` environment wherever the workflow used one. A caller that sets no input runs on
exactly the pools it ran on before. Secret-holding jobs never run on the shared `CI_RUNNER` pool
([reusable-workflows.md](../reusable-workflows.md#runs-on)).

`sealed-runner` needs `kit-ref` at v1.40.0 or later: an older pinned kit's `pr_admit` reads the
JSON list as ONE label, which names no pool, so the review never parks, silently.

**Rollback floor:** once v1.42.0 is out, moving the floating `v1` tag back goes no lower than
v1.40.0. The reusables send the pr-token composite names (`client-id`, `deprecated-client-id`,
`deprecated-private-key`) that actions older than v1.40.0 do not declare
([versioning.md](../versioning.md)).
