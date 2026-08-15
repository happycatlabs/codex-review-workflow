# Codex review architecture

This document defines the `source_context_v1` trust and consumption contract
for `.github/workflows/codex-code-review.yml`.

## Eight-job trust flow

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

The exact diff uses `--function-context --unified=20 --find-renames
--no-ext-diff --no-textconv`. That byte recipe is part of the downstream
identity contract: callers that compare `coverage.diff_sha256` must hash the
same exact `BASE..HEAD` output and use compatible Git diff behavior for that
recipe. A Git upgrade that changes function-context hunk selection must be
validated across producer and consumer before rollout. Binary numstat entries
and invalid UTF-8 fail preparation. Default-branch guidance is read by exact
base SHA and separately delimited as trusted.

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
builds the bounded logical packet. Ticket text, source, status, and diff are all
delimited as untrusted evidence.

The complete logical packet cap is 2,000,000 bytes. Any truncation prevents
Codex from running and returns `INPUT_TRUNCATED`. A packet above the
900,000-byte per-call ceiling is deterministically split into diff and source
groups. Trusted code chooses the lowest-call complete grouping, records every
prompt hash and byte count, and never splits an individual file diff or source
file. Each source group is reviewed against each required diff group so no
source/diff relationship is silently skipped.

### 4. No-tools Codex

The review job has no checkout and no Linear credential. A matrix runs one
model call per generated partition. Each model working directory contains only
its generated prompt; the JSON output schema and eventual structured output
remain generated workflow files. The pinned action receives:

```json
["--ephemeral", "--disable", "shell_tool", "--disable", "unified_exec"]
```

It also uses `permission-profile: :read-only` and `safety-strategy: drop-sudo`.
The model receives no shell, process, network, credential, patch, approval,
merge, deployment, or external-write capability and cannot request more source.
Each output is validated independently. Trusted aggregation deduplicates exact
finding fingerprints and emits the existing result contract only after every
expected partition succeeds. A missing, malformed, mismatched, or failed
partition makes the whole review an error while preserving any concrete
findings already returned by successful partitions.

### 5. Trusted publish

Publication has no model or Linear credential. It refetches current PR and
default-branch state and rejects closure, retargeting, head drift, base drift,
lookup failure, or invalid identity before accepting model output. The caller's
`${{ github.token }}` is read-only in this job and can never author a review.

It reads the Actions run with `actions: read`, requires exactly one immutable
reusable-workflow provenance entry for
`happycatlabs/codex-review-workflow/.github/workflows/codex-code-review.yml`,
and reports GitHub's exact SHA. Trusted helper code independently derives
right-side added-line ranges from the exact `BASE..HEAD` diff. The model supplies
only candidate file/range hints; it cannot supply GitHub side, diff position,
commit, or review event.

The trusted publish job gives the pinned official GitHub App token action only
the caller-owned Dancer App id and private key. That broker mints a short-lived
token limited to the current repository with `contents: read` and
`pull-requests: write`; the key never reaches a shell environment, artifact,
source checkout, intent job, or model job. The publisher proves the token's
GraphQL viewer is exactly `dancer-automation[bot]` actor id `266699010` and has
no `${{ github.token }}` or human-author fallback.

The publisher reads bounded, paginated review evidence for an exact hidden
request digest, then re-fetches the PR again immediately before writing and
submits one review with `event: COMMENT`. When every finding is addressable and
the count is at most 20, the review contains resolvable inline threads bound to
the exact head. Invalid or stale coordinates, comment overflow, generation
drift, and inline API rejection produce one complete summary review with a
stable fallback reason. Publication is all-inline or all-summary, never
partial. After a mutation or an ambiguous response, the publisher reads back
the exact review and its complete inline-comment set and verifies Dancer actor,
body, commit, locations, and comment bodies. An identical request reuses only
that fully verified review; it never mutates or resolves an older thread.

Public prose is deterministic and risk-first: title, `NOTE` or `CAUTION`,
production impact, then evidence. Structured code locations are immutable
`blob/<head>/<safe-path>#Lx-Ly` links with escaped display labels. The trusted
renderer flattens model-controlled prose, escapes HTML controls, and redacts
every exact machine fingerprint before publication. A
publication failure invalidates a clean result. The job then uploads the
unchanged v3 result plus a separate `codex-review-publication/v1` receipt and
fails unless both `verdict == "clean"` and publication succeeded. Reviews,
comments, and summaries are not approval or merge authority.

### 6. Read-only resolution preparation

Only after a complete successful publication for the still-current exact head,
trusted preparation reads bounded GraphQL review threads, each exact REST review
comment and review, and retained v3 result/publication-receipt artifacts. It
accepts at most 20 unresolved, single-root, modern right-side
Dancer threads whose body, coordinates, commit, review id, actor, and request
digest have one unambiguous prior provenance match. An outdated thread remains
eligible only when its trusted original modern coordinates still match that
provenance; outdated state alone never authorizes resolution. Human replies,
legacy-position-only threads, missing/expired artifacts, and ambiguous matches
remain untouched. Candidate overflow produces zero mutations.

### 7. No-tools resolution decision

The resolution model receives generated candidate evidence only, with no source
checkout, GitHub authority, or execution tools. It returns exactly one of
`RESOLVE_ADDRESSED`, `RESOLVE_SUPERSEDED`, `KEEP_STILL_VALID`, or
`KEEP_AMBIGUOUS` for every model candidate. If the exact prior fingerprint is
still present in the current v3 result, trusted code deterministically selects
`KEEP_STILL_VALID` without asking the model. The current review may contain
other, unrelated findings.

### 8. Trusted non-gating resolution apply

The apply job has no OpenAI credential or pull-request checkout. It receives
trusted helpers through the immutable preparation artifact and mints a separate
repository-scoped Dancer token only when a resolve decision exists. Before any
mutation it revalidates the open PR, exact head/base/default branch, unchanged
current publication receipt and review, every single-root thread, exact REST
comment, and retained prior provenance. It then sends one fixed
`resolveReviewThread(threadId)` mutation per resolve decision, never retries a
mutation blindly, requires the exact client mutation id and resolved thread in
the response, and reads the exact resolved thread back before rechecking the
generation. GitHub exposes `resolvedBy` only as the `User` GraphQL type, so it
is informational and cannot prove the Dancer Bot. Authorship instead comes
from the freshly verified, repository-scoped Dancer token immediately before
the one-shot mutation. Keeps cause no mutation.

Resolution writes only a separate `codex-review-resolution/v1` receipt. Every
resolution step is non-gating: failure cannot alter the v3 result, publication,
approval, merge, deployment, or release authority.

## Machine result

Artifact: `codex-review-result/codex-review-result.json`

```json
{
  "schema_version": "codex-review-result/v3",
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
  "summary": "Complete concise review summary.",
  "findings": [
    {
      "severity": "BUG",
      "blocking": true,
      "file": "lib/example.ts",
      "start_line": 42,
      "line": 42,
      "title": "Concrete failure",
      "body": "Standalone finding body with trigger and impact.",
      "fingerprint": "64-character stable SHA-256"
    }
  ],
  "blocking_count": 1,
  "non_blocking_count": 0,
  "finding_fingerprints": ["64-character stable SHA-256"],
  "workflow_revision": "GitHub-reported reusable-workflow SHA",
  "reviewer_revision": "codex-action@SHA;codex-cli@VERSION;model@MODEL",
  "error": null,
  "publication": {
    "status": "published | failed",
    "mode": "inline | summary",
    "fallback_reason": null,
    "inline_comment_count": 0
  }
}
```

Source and intent manifests also contain bounded counts and SHA-256 hashes. A
`clean` result requires complete manifests, complete coverage, exact current
generation identity, supported revisions, successful receipt-bound execution of
every generated partition, valid model output, and zero model findings. The
short-lived input artifact also contains `review-shards.json` with logical
packet identity, partition assignments, prompt hashes, and byte counts. Any
finding produces `blocking_findings`, including a non-blocking `RISK`.

Downstream consumers must independently require:

- terminal overall run `conclusion: success`;
- schema `codex-review-result/v3` and scope `source_context_v1`;
- matching pull, head, base, state, and current default branch;
- complete non-truncated coverage and lookup manifests;
- intent ticket/team matching the trusted task contract;
- accepted immutable workflow and reviewer revisions;
- `verdict: "clean"`;
- matching immutable `referenced_workflows` provenance.
- successful publication; inline and summary reviews remain report-only.

## Failure contract

Preparation and lookup failures are explicit and can never become clean:

| Codes | Meaning |
|---|---|
| `PREPARE_FAILED`, `BASE_NOT_ANCESTOR` | Exact generation data could not be prepared. |
| `SOURCE_CONTEXT_FAILED`, `SOURCE_CONTEXT_TIMEOUT` | Trusted source lookup failed or timed out. |
| `SOURCE_CONTEXT_STALE`, `SOURCE_CONTEXT_TRUNCATED` | Source binding or bounds are incomplete. |
| `TICKET_CONTEXT_AUTH_MISSING`, `TICKET_CONTEXT_GRAPHQL_ERROR` | Protected read path is unavailable. |
| `TICKET_CONTEXT_INVALID` | Exact owner/ticket contract is malformed. |
| `TICKET_CONTEXT_MISSING` | Exact owner/ticket context is absent. This is reported as a skipped review, not an infrastructure failure, while machine gates remain fail-closed. |
| `TICKET_CONTEXT_STALE`, `TICKET_CONTEXT_TEAM_MISMATCH`, `TICKET_CONTEXT_TRUNCATED` | Intent is stale, outside the protected team, or incomplete. |
| `UNTRUSTED_MARKER_COLLISION`, `INPUT_TRUNCATED`, `COVERAGE_INVALID` | Prompt boundaries or bounded coverage are unsafe. |
| `MODEL_OUTPUT_MISSING`, `MODEL_OUTPUT_MALFORMED`, `MODEL_OUTPUT_INVALID`, `REVIEW_FAILED` | Review execution did not yield valid output. |
| `PR_STATE_LOOKUP_FAILED`, `PR_STATE_INVALID`, `BASE_BRANCH_INVALID`, `BASE_REF_DRIFT`, `STALE_HEAD`, `STALE_BASE` | Current exact generation no longer matches. |
| `WORKFLOW_PROVENANCE_MISSING` | Immutable reusable-workflow provenance is absent. |
| `STALE_BEFORE_PUBLICATION`, `PUBLICATION_FAILED` | The final write-time generation check drifted or the review could not be published. |

For `error`, the reason is bounded workflow-owned text; raw provider bodies,
tokens, and credentials are never copied into the result.

Publication fallback reasons are a separate, stable diagnostic enum:

| Codes | Meaning |
|---|---|
| `INVALID_LOCATION`, `COMMENT_MAP_INVALID` | At least one finding cannot be proven to address an exact right-side changed line. |
| `INLINE_COMMENT_LIMIT_EXCEEDED` | The finding count exceeds the bounded 20-comment inline review limit. |
| `GITHUB_422` | GitHub definitively rejected the validated inline review, so the exact generation was revalidated and published as a summary. |
| `STALE_BEFORE_PUBLICATION` | The pull request generation changed during the final write boundary; the old result is retained only in a summary without the old commit binding. |
| `PUBLICATION_STATE_LOOKUP_FAILED`, `INLINE_PUBLICATION_FAILED`, `SUMMARY_PUBLICATION_FAILED` | Publication could not safely determine or complete a write; no ambiguous retry is attempted. |
| `COMMENT_HELPER_MISSING` | The immutable trusted publication helper was unavailable, so publication failed without posting a partial result. |
| `DANCER_AUTH_UNAVAILABLE`, `DANCER_ACTOR_MISMATCH` | The short-lived Dancer authority could not be minted or did not read back as the exact trusted App actor. |
| `PUBLICATION_REQUEST_INVALID` | A planned write was not an exact bounded `COMMENT` request, so Dancer authority was never used. |
| `PUBLICATION_EVIDENCE_INVALID`, `PUBLICATION_EVIDENCE_LIMIT_EXCEEDED` | Existing review/comment evidence was malformed, ambiguous, or exceeded the bounded read. |
| `PUBLICATION_READBACK_FAILED`, `PUBLICATION_MUTATION_AMBIGUOUS` | The exact Dancer review and comments could not be verified after the one allowed mutation attempt. |

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
4. a clean summary and a valid changed-line inline finding are both authored by
   verified Dancer, while invalid location, overflow, stale generation, and
   GitHub `422` create complete Dancer-authored summary reviews;
5. command-shaped source, ticket, and resolution data cannot execute or obtain
   credentials;
6. a prior fixed finding resolves only with exact retained provenance, while a
   human reply, legacy comment, missing artifact, current matching fingerprint,
   ambiguity, and candidate overflow all produce zero mutation; outdated state
   alone never supplies the addressed-or-superseded proof.

The disposable proofs remain unmerged. Do not enable ordinary consumer reviews
until the caller has the Dancer App secrets and both clean and inline readback
receipts prove the exact actor. Thread resolution remains separate from review
publication and produces only its own non-gating receipt.

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
