# codex-review-workflow

Reusable exact-snapshot Codex review for `happycatlabs/*` pull requests.

The V1 workflow has four jobs:

1. verify a base-controlled `pull_request_target` still targets the repository's
   current default branch and that its live base is an ancestor of the PR head;
2. prepare a bounded, strict UTF-8 `BASE..HEAD` diff packet with no secrets;
3. run Codex with no source checkout and both execution tools disabled;
4. refetch the PR snapshot, verify immutable workflow provenance, publish a
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
    with:
      allow-bot-users: dancer-automation[bot]
```

Replace `WORKFLOW_COMMIT_SHA` with the immutable SHA. Keep the caller limited
to invoking this reusable workflow; a `pull_request_target` caller must never
check out or execute pull-request code.

Set `OPENAI_API_KEY` as a consumer repository secret. `CODEX_AUTH_JSON` is a
fail-closed compatibility signal only: when it is the only credential, the
result is `AUTH_LEGACY_UNSAFE` and Codex does not run. Pass only these named
secrets; do not use `secrets: inherit` for the credential-bearing review job.

## Inputs

| Input | Default | Purpose |
|---|---|---|
| `model` | `gpt-5.5` | Codex model. |
| `codex-cli-version` | `0.144.1` | Codex CLI version installed by the pinned action. |
| `allow-users` | empty | Additional exact user actors accepted by codex-action. |
| `allow-bot-users` | empty | Additional exact bot actors accepted by codex-action. Wildcards are not allowed. |

Every job uses ephemeral GitHub-hosted Linux (`ubuntu-24.04`). A persistent
self-hosted runner is unsupported by this security contract.

## Review meaning

`review_scope: "diff_v1"` means the model reviews only the complete supplied
exact diff under trusted default-branch guidance. It cannot browse changed
files or surrounding source. A `clean` verdict means zero findings in that
bounded input; it is not whole-repository or feature correctness proof.

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
