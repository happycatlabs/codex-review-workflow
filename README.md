# codex-review-workflow

Reusable exact-snapshot Codex review for `happycatlabs/*` pull requests.

The V2 workflow has five jobs:

1. verify a base-controlled `pull_request_target` still targets the repository's
   current default branch and that its live base is an ancestor of the PR head;
2. prepare a bounded, strict UTF-8 `BASE..HEAD` diff plus exact-head source
   context for changed files, unchanged direct callers, and direct relative or
   `@/` dependencies, with no secrets;
3. resolve one exact owner-bound FABLE ticket through protected read-only
   credentials, then seal the model prompt after those credentials leave scope;
4. run Codex with no source checkout and both execution tools disabled;
5. refetch the PR snapshot, verify immutable workflow provenance, publish a
   machine artifact and fresh comment, and fail closed unless the result is
   clean.

## Consumer setup

The caller must be base-controlled and pin the reusable workflow to one full
40-character commit SHA.

```yaml
name: Codex code review

on:
  pull_request_target:
    branches: [master]
    types: [opened, reopened, synchronize, ready_for_review, edited]

permissions:
  actions: read
  contents: read
  pull-requests: write
  issues: write

jobs:
  review:
    uses: happycatlabs/codex-review-workflow/.github/workflows/codex-code-review.yml@WORKFLOW_COMMIT_SHA
    secrets:
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
      CODEX_AUTH_JSON: ${{ secrets.CODEX_AUTH_JSON }}
      LINEAR_CLIENT_ID: ${{ secrets.LINEAR_CLIENT_ID }}
      LINEAR_CLIENT_SECRET: ${{ secrets.LINEAR_CLIENT_SECRET }}
    with:
      allow-bot-users: dancer-automation[bot],dependabot[bot]
      linear-team-key: FABLE
```

Replace `WORKFLOW_COMMIT_SHA` with the immutable SHA. Keep the caller limited
to invoking this reusable workflow; a `pull_request_target` caller must never
check out or execute pull-request code.

The workflow loads its trusted helper code from `job.workflow_repository` at
the exact `job.workflow_sha`. This currently works without a separate checkout
token because this repository is public. If the repository becomes private,
the checkout requires an explicit least-privilege cross-repository token with
contents read access or a different immutable packaging mechanism.

Set `OPENAI_API_KEY` plus a read-only Linear OAuth client as consumer repository
secrets. The Linear query is fixed to the exact ticket in the trusted PR-owner
marker and validates the protected team key; it cannot switch teams or run an
arbitrary tracker query. `CODEX_AUTH_JSON` is a
fail-closed compatibility signal only: when it is the only credential, the
result is `AUTH_LEGACY_UNSAFE` and Codex does not run. Pass only these named
secrets; do not use `secrets: inherit` for the credential-bearing review job.
If Dependabot updates the immutable workflow pin, list `dependabot[bot]`
explicitly alongside the automation actor so its update PR can be reviewed.

## Inputs

| Input | Default | Purpose |
|---|---|---|
| `model` | `gpt-5.5` | Codex model. |
| `codex-cli-version` | `0.144.1` | Codex CLI version installed by the pinned action. |
| `allow-users` | empty | Additional exact user actors accepted by codex-action. |
| `allow-bot-users` | empty | Additional exact bot actors accepted by codex-action. Wildcards are not allowed. |
| `linear-team-key` | required | Protected caller-owned team key required for the exact ticket. |

Every job uses ephemeral GitHub-hosted Linux (`ubuntu-24.04`). A persistent
self-hosted runner is unsupported by this security contract.

## Review meaning

`review_scope: "source_context_v1"` means trusted code assembles the complete
bounded diff, exact-ticket intent, and exact-head source for eligible changed
files plus unchanged direct callers and dependencies. The model cannot browse
or request more files. A `clean` verdict means zero findings in that complete
bounded packet; it is not whole-repository or feature correctness proof.

Any model finding produces `blocking_findings` and fails the workflow in V1.
The artifact retains `blocking_count` and `non_blocking_count` as metadata, but
neither classification permits automatic passage yet.

The publisher uploads `codex-review-result/codex-review-result.json`. See
[`codex-code-review.md`](codex-code-review.md) for the schema, trust boundaries,
coverage rules, error codes, canaries, and downstream consumption contract.

## Local validation

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
actionlint -color .github/workflows/codex-code-review.yml
git diff --check
```
