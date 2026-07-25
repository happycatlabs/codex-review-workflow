# Codex review architecture

This document defines the `source_context_v1` trust and consumption contract
for `.github/workflows/codex-code-review.yml`.

## Five-job trust flow

### 1. Guard

The base-controlled caller must use:

```yaml
on:
  pull_request_target:
    branches: [master]
    types: [opened, reopened, synchronize, ready_for_review, edited]
```

The guard refetches repository, pull request, and default-branch state and
requires an open PR whose `base.ref` is the current default branch and whose
`base.sha` is the independently resolved current default-branch commit. It
exports exact pull, head, base, ref, and state identities. Every later job
depends on this guard. The caller must contain only the reusable invocation; it
must never check out or execute PR code.

### 2. Prepare exact-head data

Preparation has no secrets. It checks out the guarded head with persisted
credentials disabled and treats every repository blob as data. It runs no
repository program, hook, package manager, build, or script.

Before assembly, `git merge-base --is-ancestor BASE_SHA HEAD_SHA` must succeed.
A behind-base head returns `BASE_NOT_ANCESTOR`, not a misleading reverse diff.

Trusted code reads immutable `HEAD` blobs with `git ls-tree` and `git cat-file`.
It scans only a fixed source-extension set, excludes symlinks, generated/build
paths, declaration files, binary data, and invalid UTF-8, and enforces file,
byte, tracked-file, and time caps. For eligible changed files it includes:

- the changed file;
- unchanged direct callers;
- unchanged direct dependencies.

Resolution is deliberately fixed: relative imports and Fable's `@/` repository
root alias only, including deterministic iOS/native/index candidates. An
unresolved source-shaped `@/` import fails closed. Trusted code does not execute
or interpret `tsconfig.json`, package resolvers, or PR-controlled configuration.

The exact diff uses external diff and text conversion disabled. Binary numstat
entries and invalid UTF-8 fail preparation. Default-branch guidance is read by
exact base SHA and separately delimited as trusted.

### 3. Resolve exact-ticket intent and seal prompt

This is the only job that receives the Linear OAuth client. Trusted code reads
GitHub comments for the exact repository and PR, accepts one complete,
Dancer-authored `fable-pr-owner/v1` marker with trusted edit history, and binds
it to the guarded pull/head/base generation.

Only `FABLE-N` from that marker can select a ticket. The Linear GraphQL document
is static and receives that identifier as its only selector. The response must
match the protected caller-owned team key. GraphQL errors, pagination,
malformed data, missing credentials, wrong team, size overflow, or stale
binding produce explicit non-clean errors. Raw credentials and transient tokens
are never written to artifacts.

After the credential-bearing step ends, a separate no-secret step validates and
hashes source and intent manifests, rejects reserved-boundary injection, and
builds the bounded prompt. Ticket text, source, status, and diff are all
delimited as untrusted evidence.

The prompt cap is 2,000,000 bytes. Any truncation prevents Codex from running
and returns `INPUT_TRUNCATED`.

### 4. No-tools Codex

The review job has no checkout and no Linear credential. Its working directory
contains only the generated prompt, JSON output schema, and eventual structured
output. The pinned action receives:

```json
["--ephemeral", "--disable", "shell_tool", "--disable", "unified_exec"]
```

It also uses `permission-profile: :read-only` and `safety-strategy: drop-sudo`.
The model receives no shell, process, network, credential, patch, approval,
merge, deployment, or external-write capability and cannot request more source.

### 5. Trusted publish

Publication has no model or Linear credential. It refetches current PR and
default-branch state and rejects closure, retargeting, head drift, base drift,
lookup failure, or invalid identity before accepting model output.

It reads the Actions run with `actions: read`, requires exactly one immutable
reusable-workflow provenance entry for
`happycatlabs/codex-review-workflow/.github/workflows/codex-code-review.yml`,
and reports GitHub's exact SHA. The job uploads one machine artifact, posts a
fresh comment, and fails unless `verdict == "clean"`. Comments and summaries
are not authority.

## Machine result

Artifact: `codex-review-result/codex-review-result.json`

```json
{
  "schema_version": "codex-review-result/v2",
  "verdict": "clean | blocking_findings | error",
  "pull_number": 198,
  "head_sha": "40-character reviewed PR head SHA",
  "base_ref": "master",
  "base_sha": "40-character reviewed base SHA",
  "state": "open",
  "review_scope": "source_context_v1",
  "activated_packets": ["general"],
  "coverage": {
    "complete": true,
    "truncated": false,
    "prompt_limit_bytes": 2000000,
    "prompt_bytes_original": 1234,
    "prompt_bytes_included": 1234,
    "prompt_sha256": "64-character SHA-256",
    "diff_bytes_original": 900,
    "diff_bytes_included": 900,
    "diff_sha256": "64-character SHA-256",
    "diff_encoding": "utf-8",
    "binary_files": false,
    "status_bytes_original": 80,
    "trusted_guidance_bytes": 254,
    "source_context_bytes": 400,
    "intent_context_bytes": 300
  },
  "lookup_context": {
    "complete": true,
    "source": {
      "complete": true,
      "truncated": false,
      "pull_number": 198,
      "head_sha": "40-character reviewed PR head SHA",
      "base_ref": "master",
      "base_sha": "40-character reviewed base SHA"
    },
    "intent": {
      "complete": true,
      "truncated": false,
      "pull_number": 198,
      "head_sha": "40-character reviewed PR head SHA",
      "base_ref": "master",
      "base_sha": "40-character reviewed base SHA",
      "ticket_identifier": "FABLE-198",
      "team_key": "FABLE",
      "collected_at_epoch": 0
    }
  },
  "blocking_count": 0,
  "non_blocking_count": 0,
  "finding_fingerprints": [],
  "workflow_revision": "GitHub-reported reusable-workflow SHA",
  "reviewer_revision": "codex-action@SHA;codex-cli@VERSION;model@MODEL",
  "error": null
}
```

Source and intent manifests also contain bounded counts and SHA-256 hashes. A
`clean` result requires complete manifests, complete coverage, exact current
generation identity, supported revisions, valid model output, and zero model
findings. Any finding produces `blocking_findings`, including a non-blocking
`RISK`.

Downstream consumers must independently require:

- terminal overall run `conclusion: success`;
- schema `codex-review-result/v2` and scope `source_context_v1`;
- matching pull, head, base, state, and current default branch;
- complete non-truncated coverage and lookup manifests;
- intent ticket/team matching the trusted task contract;
- accepted immutable workflow and reviewer revisions;
- `verdict: "clean"`;
- matching immutable `referenced_workflows` provenance.

## Failure contract

Preparation and lookup failures are explicit and can never become clean:

| Codes | Meaning |
|---|---|
| `PREPARE_FAILED`, `BASE_NOT_ANCESTOR` | Exact generation data could not be prepared. |
| `SOURCE_CONTEXT_FAILED`, `SOURCE_CONTEXT_TIMEOUT` | Trusted source lookup failed or timed out. |
| `SOURCE_CONTEXT_STALE`, `SOURCE_CONTEXT_TRUNCATED` | Source binding or bounds are incomplete. |
| `TICKET_CONTEXT_AUTH_MISSING`, `TICKET_CONTEXT_GRAPHQL_ERROR` | Protected read path is unavailable. |
| `TICKET_CONTEXT_INVALID`, `TICKET_CONTEXT_MISSING` | Exact owner/ticket contract is malformed or absent. |
| `TICKET_CONTEXT_STALE`, `TICKET_CONTEXT_TEAM_MISMATCH`, `TICKET_CONTEXT_TRUNCATED` | Intent is stale, outside the protected team, or incomplete. |
| `UNTRUSTED_MARKER_COLLISION`, `INPUT_TRUNCATED`, `COVERAGE_INVALID` | Prompt boundaries or bounded coverage are unsafe. |
| `MODEL_OUTPUT_MISSING`, `MODEL_OUTPUT_MALFORMED`, `MODEL_OUTPUT_INVALID`, `REVIEW_FAILED` | Review execution did not yield valid output. |
| `PR_STATE_LOOKUP_FAILED`, `PR_STATE_INVALID`, `BASE_BRANCH_INVALID`, `BASE_REF_DRIFT`, `STALE_HEAD`, `STALE_BASE` | Current exact generation no longer matches. |
| `WORKFLOW_PROVENANCE_MISSING` | Immutable reusable-workflow provenance is absent. |

For `error`, the reason is bounded workflow-owned text; raw provider bodies,
tokens, and credentials are never copied into the result.

## Immutable helper packaging

The reusable workflow checks out its own `src/` at
`job.workflow_repository` and `job.workflow_sha`. This keeps helper code pinned
to the same immutable workflow generation while avoiding a large embedded YAML
program. The repository is currently public, so no separate checkout token is
required. Making it private would require an explicit least-privilege
cross-repository contents-read token or different immutable packaging; the
caller token must not be assumed to grant that access.

## Delivery proof

After merging a workflow revision, the consumer must pin that full SHA and run
disposable PRs that prove:

1. exact helper checkout works from the base-controlled caller;
2. an `@/`-aliased unchanged caller/dependency regression missed by diff-only
   review is found;
3. clean, finding, missing/malformed context, wrong team, provider failure,
   head drift, and base drift remain exact-generation bound;
4. command-shaped source/ticket data cannot execute or obtain credentials.

Do not merge a disposable proof PR. Until the immutable pin and proof exist,
this artifact is not merge authority.

## Local validation

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
actionlint -ignore 'property "workflow_(repository|sha)" is not defined' \
  .github/workflows/codex-code-review.yml
git diff --check
```

The actionlint ignore is limited to job identity properties already documented
by GitHub but not yet modeled by actionlint 1.7.12.
