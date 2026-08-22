# codex-review-workflow

Reusable exact-snapshot Codex review for `happycatlabs/*` pull requests.

The V2 workflow has five review-gate jobs followed by three isolated,
non-gating thread-resolution jobs:

1. verify a base-controlled `pull_request_target` is either an exact current
   default-branch PR or a proven dependent child in one bounded native GitHub
   Stack, and that every dependency edge from the active root through the
   target has commit ancestry;
2. prepare a bounded, strict UTF-8 `BASE..HEAD` diff plus exact-head source
   context for changed files, unchanged direct callers, and direct relative or
   `@/` dependencies, with no secrets;
3. resolve one exact owner-bound FABLE ticket through protected read-only
   credentials, then seal the model prompt after those credentials leave scope;
4. fail closed with a zero-model-call receipt until a supported
   ChatGPT-managed subscription auth path is integrated;
5. refetch the PR snapshot, verify immutable workflow provenance, derive
   commentable lines from the exact diff in trusted code, mint one repository-
   scoped Dancer installation token, and publish one verified `COMMENT` review
   plus machine evidence before failing closed unless the result is clean.
6. collect at most 20 unresolved single-root modern Dancer threads whose exact
   review and comment identity can be rebuilt from retained v3 results and
   publication receipts;
7. record the same zero-model-call subscription receipt for candidates whose
   prior fingerprint is absent from the current result; and
8. mint separate Dancer authority only for resolve decisions, revalidate the
   exact generation, receipt, thread, and provenance, then apply one fixed
   `resolveReviewThread` mutation per candidate with exact readback.

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
  pull-requests: read
  issues: read

jobs:
  review:
    uses: happycatlabs/codex-review-workflow/.github/workflows/codex-code-review.yml@WORKFLOW_COMMIT_SHA
    secrets:
      CODEX_AUTH_JSON: ${{ secrets.CODEX_AUTH_JSON }}
      LINEAR_CLIENT_ID: ${{ secrets.LINEAR_CLIENT_ID }}
      LINEAR_CLIENT_SECRET: ${{ secrets.LINEAR_CLIENT_SECRET }}
      DANCER_APP_ID: ${{ secrets.DANCER_APP_ID }}
      DANCER_PRIVATE_KEY: ${{ secrets.DANCER_PRIVATE_KEY }}
    with:
      allow-bot-users: dancer-automation[bot],dependabot[bot]
      linear-team-key: FABLE
```

Replace `WORKFLOW_COMMIT_SHA` with the immutable SHA. Keep the caller limited
to invoking this reusable workflow; a `pull_request_target` caller must never
check out or execute pull-request code.

The example keeps the existing `branches: [master]` consumer filter. The
reusable workflow can prove dependent stack children, but those events do not
reach it until a separately reviewed consumer change broadens that filter.

The workflow loads its trusted helper code from `job.workflow_repository` at
the exact `job.workflow_sha`. This currently works without a separate checkout
token because this repository is public. If the repository becomes private,
the checkout requires an explicit least-privilege cross-repository token with
contents read access or a different immutable packaging mechanism.

Set the read-only Linear OAuth client fields as consumer repository secrets.
The Linear query is fixed to the exact ticket in the trusted PR-owner
marker and validates the protected team key; it cannot switch teams or run an
arbitrary tracker query. `CODEX_AUTH_JSON` is a
fail-closed compatibility signal only: when it is the only credential, the
result is `AUTH_LEGACY_UNSAFE` and Codex does not run. Without it, the result is
`AUTH_SUBSCRIPTION_UNAVAILABLE`. Both paths record `model_invocations: 0` and
`billing_mode: none`; the secret content is never read. Pass only these named
secrets; do not use `secrets: inherit` for the credential-bearing review job.
If Dependabot updates the immutable workflow pin, list `dependabot[bot]`
explicitly alongside the automation actor so its update PR can be reviewed.

Set `DANCER_APP_ID` and `DANCER_PRIVATE_KEY` only in the caller repository's
secret store. The private key is passed directly to the pinned official token
broker in the trusted publish job; it is never placed in an environment
variable, artifact, source checkout, intent job, or model job. The broker mints
a short-lived token scoped to the current repository with `contents: read` and
`pull-requests: write`, then revokes it after the job. Missing or invalid Dancer
authority fails publication without falling back to `${{ github.token }}` or a
human token.

## Inputs

| Input | Default | Purpose |
|---|---|---|
| `model` | `gpt-5.6-sol` | Codex model. |
| `effort` | `none` | Explicit Codex reasoning effort for review and resolution. |
| `codex-cli-version` | `0.144.1` | Reserved Codex CLI pin for a supported producer. |
| `allow-users` | empty | Reserved exact user actors for a supported producer. |
| `allow-bot-users` | empty | Reserved exact bot actors for a supported producer. Wildcards are not allowed. |
| `linear-team-key` | required | Protected caller-owned team key required for the exact ticket. |

Every job uses Happycat's ephemeral Blacksmith Linux runner pool
(`blacksmith-2vcpu-ubuntu-2404`). The zero-call jobs create no Codex user, API
proxy, provider process, or model action. Persistent or repository-controlled
runners are unsupported by this security contract.

## Re-enable gate

Keep every caller disabled until a reviewed producer uses a supported
ChatGPT-managed Codex subscription session, records sanitized before/after auth
status plus runner/model/usage evidence, and retains zero API-key fallback. The
shared workflow must land first; callers may then pin that exact revision while
preserving their own no-API-key forwarding test. Because GitHub rejects an
undeclared reusable-workflow secret before any receipt can be written, every
caller must remove `OPENAI_API_KEY` in the same commit that updates this exact
pin. Workflow enablement, secret removal, default activation, deployment, and
release remain separate actions.

## Review meaning

`review_scope: "source_context_v1"` means trusted code assembles the complete
bounded diff, exact-ticket intent, and exact-head source for eligible changed
files plus unchanged direct callers and dependencies. The model cannot browse
or request more files. A `clean` verdict means zero findings in that complete
bounded packet; it is not whole-repository or feature correctness proof.
Trusted code also supplies the exact right-side diff intervals as a bounded,
untrusted inline-anchor map. The model uses those candidates for structured
locations, while the publisher independently validates every location and
falls back to the complete summary if an anchor is unavailable.
The source packet is capped at 150 files and 1,250,000 total bytes, with a
100,000-byte per-file limit. Any overflow fails closed as
`SOURCE_CONTEXT_TRUNCATED`; the workflow never silently drops source files.
The complete logical packet may be up to 2,000,000 bytes. When it exceeds the
900,000-byte future model-call limit, trusted code deterministically partitions
the diff and source context, but every partition currently records the same
validated zero-call stop. No partition can emit `NO_ISSUES`, findings, or a
completed-review claim without a supported producer.

Any model finding produces `blocking_findings` and fails the workflow in V1.
The artifact retains `blocking_count` and `non_blocking_count` as metadata, but
neither classification permits automatic passage yet.

A pull request generation without trusted task context is an expected
ineligible state, not an infrastructure failure. The review is reported as
skipped and automatic approval remains disabled; all machine gates continue to
fail closed.

The publisher uploads the unchanged v3
`codex-review-result/codex-review-result.json` plus a bounded
`codex-review-publication/v1` receipt containing the verified Dancer actor,
review id, request digest, exact observed generation, and idempotent-reuse
status. Direct behavior is unchanged; stack receipts seal the validated active
root-to-target lineage without binding descendants or draft-only state. After
one successful review POST, bounded read-only evidence retries
with at most 31 seconds of backoff allow GitHub's review/comment indexes to
converge; they never repeat the mutation or relax actor, body, commit,
coordinate, or `COMMENTED` validation. The review-scoped comment collection is
used only as a bounded index of exact comment ids. Each indexed comment is then
read from its exact resource so modern `line` and `side` evidence is available
for strict validation; the publisher never broadens to the PR-wide comment
collection.
A line-addressable finding becomes a resolvable inline thread only when its
model-supplied file/range matches right-side added lines in the exact diff.
Model output never chooses GitHub `side`, diff position, or review event. If
any finding cannot be anchored, the result exceeds the 20-comment publication
limit, the generation changes, or GitHub rejects the inline review, the
publisher submits one complete summary `COMMENT` review instead. Findings are
never partially published or discarded.

Public reviews use a deterministic risk-first layout: `## Codex review`, a
GitHub `NOTE` or `CAUTION` callout, `### Production impact`, and `### Evidence`.
Every structured code location is an immutable exact-head blob link. Finding
fingerprints remain in the v3 machine result and never appear in public prose.
Before mutation the publisher revalidates the open PR, head, base ref, base
SHA, current default-branch SHA, and the complete prepared base provenance. A
stack topology change fails with zero publication mutation; it never becomes
an unbound stale summary. A failed complete root-to-target ancestry proof also
has zero publication authority. It also reads back the Dancer-authored
review and every inline comment. An exact hidden request marker makes response
recovery and identical reruns idempotent; it does not resolve or alter any old
thread.

The later resolution path is deliberately narrower. Human-replied,
legacy-position-only, ambiguous, and missing/expired-provenance threads remain
untouched. Outdated state is evidence only and never authorizes resolution by
itself; trusted original modern coordinates remain eligible for a separate
addressed-or-superseded decision. More than 20 proven candidates causes zero
mutations. An exact prior
fingerprint still present in the current v3 result is deterministically kept
open, even when the current review has unrelated findings. Resolver failure is
recorded only in a separate `codex-review-resolution/v1` receipt and cannot
change the v3 verdict, publication, or review gate.

The resolver re-proves the same receipt-bound base provenance immediately
before any thread mutation. It proves write authorship from the freshly verified, repository-
scoped Dancer App token immediately before its one-shot mutation. GitHub types
`PullRequestReviewThread.resolvedBy` as `User`, so that field is informational
only and is never used or reported as Dancer identity. The exact client
mutation id, resolved thread id/state readback, and generation rechecks prove
the mutation outcome. GitHub rejects `resolveReviewThread` for an App token
limited to `contents: read` even when `viewerCanResolve` is true, so the
conditionally minted resolver token requests `contents: write` and
`pull-requests: write`. The independent publisher token remains limited to
`contents: read` and `pull-requests: write`.

See
[`codex-code-review.md`](codex-code-review.md) for the schema, trust boundaries,
coverage rules, error codes, canaries, and downstream consumption contract.

## Local validation

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
actionlint -color .github/workflows/codex-code-review.yml
git diff --check
```
