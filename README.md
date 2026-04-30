# codex-review-workflow

Centralized **Codex Code Review** reusable workflow for `happycatlabs/*` repos.

This repo owns the canonical `.github/workflows/codex-code-review.yml`. Each consumer repo has a thin caller that delegates here, so the prompt, auth model, incremental-review state, and comment lifecycle live in one place and update org-wide on push to `main`.

## Quick start (consumer repo)

Add `.github/workflows/codex-code-review.yml`:

```yaml
on:
  pull_request:
    types: [opened, reopened, synchronize, ready_for_review]

jobs:
  review:
    uses: happycatlabs/codex-review-workflow/.github/workflows/codex-code-review.yml@main
    secrets: inherit
```

Set the `CODEX_AUTH_JSON` secret on the consumer repo (contents of `~/.codex/auth.json` from `codex login`):

```
gh secret set CODEX_AUTH_JSON -R <owner>/<consumer-repo> < ~/.codex/auth.json
```

Optionally drop a `REVIEW.md` at the repo root with project conventions, escalation rules, and gotchas the reviewer should know — the workflow reads it at runtime.

## Optional inputs

All inputs have defaults; most consumers won't need to set any.

| Input | Default | Purpose |
|---|---|---|
| `runner` | `ubuntu-latest` | Runner label. Override for self-hosted or larger runners. |
| `model` | `gpt-5.5` | Codex model. Must be subscription-eligible. |
| `codex-cli-version` | `0.124.0` | Pinned `@openai/codex` npm version. |
| `sentry-project` | _(empty)_ | Sentry project slug. If unset, the Sentry-context step is skipped. |
| `sentry-org` | `happycatlabs` | Sentry org slug. Ignored if `sentry-project` is empty. |
| `sentry-ticket-regex` | _(empty)_ | Regex like `\bMYREPO-\d+\b` for detecting Sentry tickets in PR title/body. |

Pass them via `with:`:

```yaml
jobs:
  review:
    uses: happycatlabs/codex-review-workflow/.github/workflows/codex-code-review.yml@main
    secrets: inherit
    with:
      sentry-project: my-project
      sentry-ticket-regex: '\bMYREPO-\d+\b'
```

## What's in here

- `.github/workflows/codex-code-review.yml` — the reusable workflow.
- `codex-code-review.md` — architecture and extension guide. Read it before changing the workflow.

## Refreshing `CODEX_AUTH_JSON`

The OAuth refresh token in `auth.json` is long-lived (months) but eventually expires. When CI starts failing with a clear codex-CLI auth error, refresh:

```
codex login                                                      # regenerates ~/.codex/auth.json
gh secret set CODEX_AUTH_JSON -R <owner>/<repo> < ~/.codex/auth.json
```

Each consumer repo has its own `CODEX_AUTH_JSON` (passed via `secrets: inherit`), so refresh per-repo.
