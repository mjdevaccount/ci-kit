"""The ONE spelling of the secure / sealed pool selection every reusable uses (v1.42.0).

Precedence: the caller's `secure-runner` / `sealed-runner` input (a JSON label list, from the
caller's reviewed workflow file); else the caller repo's CI_SECURE_RUNNER / CI_SEALED_RUNNER
variable, an ON/OFF switch for the kit's default labels (a variable never carries labels: a typo
there must not move a token job onto a docker-socket runner); else hosted. The tests that pin a
job's pool import these, so the expression cannot drift between workflows.
"""
SECURE = ("${{ inputs.secure-runner && fromJSON(inputs.secure-runner) || vars.CI_SECURE_RUNNER "
          "&& fromJSON('[\"self-hosted\",\"home-secure\"]') || 'ubuntu-latest' }}")
SEALED = ("${{ inputs.sealed-runner && fromJSON(inputs.sealed-runner) || vars.CI_SEALED_RUNNER "
          "&& fromJSON('[\"self-hosted\",\"home-sealed\"]') || 'ubuntu-latest' }}")
SECURE_ENV = "${{ (inputs.secure-runner || vars.CI_SECURE_RUNNER) && 'ci-secure' || '' }}"
# the v1.39 expressions: what a caller that sets no input must still get, exactly
OLD_SECURE = "${{ vars.CI_SECURE_RUNNER && fromJSON('[\"self-hosted\",\"home-secure\"]') || 'ubuntu-latest' }}"
OLD_SEALED = "${{ vars.CI_SEALED_RUNNER && fromJSON('[\"self-hosted\",\"home-sealed\"]') || 'ubuntu-latest' }}"
OLD_SECURE_ENV = "${{ vars.CI_SECURE_RUNNER && 'ci-secure' || '' }}"
