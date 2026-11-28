# GitHub Apps (`ci-app-audit`, `apps` extra; `ci-pr-token-check`)

Two tools for the GitHub Apps a repository's automation acts as:

- **`ci-app-audit`** declares each App (its permissions, events and installations) in a manifest
  under review, and turns any difference from live into a red run.
- **`ci-pr-token-check`** and the `ci_kit.pr_token` reader parse the one record line every ci-kit
  token job prints ([pr-ops/push-token.md](pr-ops/push-token.md)), with the same module that writes
  it.

## Why an App needs a declaration

An App's grants change on its settings page and an installation's repositories change on the
installation page. Neither leaves a commit, a failed run or a log line. A permission widened by
hand, or a repository quietly added to an installation, keeps working, so nothing ever turns red.
The manifest makes the App's shape a reviewed file, and the scheduled audit makes drift a red run
that a schedule watcher already reads.

## The manifest

YAML or JSON. Where a key exists in GitHub's own [App manifest](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest),
it means the same thing.

```yaml
apps:
  - name: example-bot                 # the App's slug (GET /app .slug)
    client_id: Iv23liEXAMPLE          # the JWT issuer; or app_id: 123456 (either, or both)
    default_permissions:              # exactly the App's grants
      contents: write
      pull_requests: write
    default_events: []                # the webhook events it subscribes to
    installations:                    # every installation, by account; [] = installed nowhere
      - account: example-org
        repository_selection: selected
        repositories: [repo-a, repo-b]
      - account: example-user
        repository_selection: all
        permissions: {contents: read} # optional: this installation's own, when it differs
```

- `metadata: read` is implicit (every App holds it). Declaring it is allowed; omitting it is not drift.
- Levels are ordered `read < write < admin`, so a change is named **widened** or **narrowed**.
- `url`, `hook_attributes`, `redirect_url`, `callback_urls`, `setup_url`, `description`,
  `public`, `request_oauth_on_install` and `setup_on_update` are accepted and not audited. Any
  other key is treated as a typo: exit 2.
- An installation's permissions are held to its own `permissions` when declared, else to the
  App's. A grant changed on the App that an installation owner has not accepted yet shows up here.

The manifest belongs to the repository that owns the Apps, never to ci-kit.

## The live read

| App has | reads | sees |
|---|---|---|
| its private key (`--key-file NAME=PATH`, `--key-env NAME=VAR`) | a JWT (RS256, PyJWT) for `GET /app` and `GET /app/installations`; per `selected` installation, an installation token scoped to `metadata: read` for `GET /installation/repositories`, revoked after use | everything |
| no key, `--app-level NAME` | `GET /apps/<slug>` with `--token-env` (default `GH_TOKEN`, then `GITHUB_TOKEN`) | permissions and events only. The output names the installations it did not look at, on every run |
| no key, no `--app-level` | nothing | exit 2 |

Keys are never read from argv and nothing prints a key, a JWT or a token. Pages are followed to
the end (100 per page).

## Exit codes

| exit | when |
|---|---|
| 0 | every declared App matches live |
| 1 | drift: a permission widened or narrowed (added or dropped counts), an event added or dropped, an installation on an undeclared account or a declared one missing, a suspended installation, `all` vs `selected`, a repository installed but undeclared or declared but not installed, an installation's permissions or events differing from the declaration, an id that differs |
| 2 | could not look: the manifest unreadable or invalid, a key unreadable or refused, a key belonging to another App, any API read failing, a flag naming no declared App |

## CLI

```bash
ci-app-audit --manifest ci/github-apps.yml --key-env example-bot=EXAMPLE_BOT_KEY \
             --app-level other-app
ci-app-audit --manifest ci/github-apps.yml --key-file example-bot=/path/to/key.pem --only example-bot
ci-app-audit --selftest   # a synthetic App on an in-process fake GitHub: bad = 1, good = 0, blind = 2
```

## Scheduled, as a reusable workflow

[`app-audit.yml`](reusable-workflows.md#app-audityml) runs it on the caller's schedule, on the
secure pool (the job holds a private key). The red is the deliverable: run it on a `schedule:` and
let whatever watches your scheduled workflows (staleness and red streaks) watch this one too.

```yaml
on:
  schedule: [{cron: "17 6 * * *"}]
  workflow_dispatch:
jobs:
  apps:
    uses: mjdevaccount/ci-kit/.github/workflows/app-audit.yml@v1
    with:
      manifest: ci/github-apps.yml
      keyed-app: example-bot
      app-level: other-app
    secrets:
      app-private-key: ${{ secrets.EXAMPLE_BOT_PRIVATE_KEY }}
```

One key per call. An App whose key lives somewhere else (on a host, say) is audited `--app-level`
here, and fully by a second run where its key lives, with `--only` that App.

## Reading the pr-token record

The `pr-token:` record's grammar is owned by `ci_kit/pr_token.py`, in both directions: `line()`
writes it, and next to it

- `parse(text) -> Record` reads one record, STRICTLY: an unknown, missing or duplicated key, an
  empty value, a value holding `=`, whitespace or a `${` template, or a `source` / `app` /
  `reason` outside its set raises `RecordError`. It never returns a partial record.
- `classify(record, repo=None) -> list[str]` names what a record says went wrong: a failure-class
  reason, `app=failed` (the canary), an App mode that did not get the App, `source=app` in `pat`
  mode, `source=none`, or a record naming another repository. `REASON_CLASS` classifies every
  reason (`ok`, `fallback`, `failure`).
- `records_in_log(text)` finds the record lines in a job log. Only a line anchored on the
  `##[notice|warning|error]` rendering counts, because the runner echoes the composite's own
  script, record template included, into the log.
- `records_in_annotations(list)` finds them in a check run's annotations
  (`/check-runs/{id}/annotations`).

A test runs the composite's real `decide` and `report` steps over every input combination and
parses every record they print back to exactly what was written, so the grammar cannot change in
one direction only. A consumer pinned to a ci-kit version reads records with that version's rules.
A record from a newer producer carrying a value the pinned reader does not know is refused as
unparseable, never read as healthy.

```bash
gh run view <run> --log --job <job> | ci-pr-token-check --repo owner/name
gh api repos/owner/name/check-runs/<job>/annotations | ci-pr-token-check --format annotations
```

Exit 0: every record is clean. Exit 1: a finding, an unparseable record, or no record at all (a
token step that ran must print one). Exit 2: the input could not be read.
