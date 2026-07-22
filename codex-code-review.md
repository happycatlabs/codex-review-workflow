# Codex review architecture

This document defines the V1 authority and consumption contract for
`.github/workflows/codex-code-review.yml`.

## Four-job trust flow

### 1. Guard

The first job requires the inherited event to be `pull_request_target` before
any API lookup or secret-bearing job. It then reads the repository, current
pull request, and current default-branch commit from GitHub's API and requires
all of the following:

- the pull request is open;
- its current `base.ref` equals the repository's current `default_branch`;
- event base/default metadata has not drifted from the API response;
- head, PR base, and independently resolved default-branch identities are full
  commit SHAs;
- PR `base.sha` equals the independently resolved default-branch SHA.

The independently resolved commit is the trusted default-branch revision for
this review. The guard exports head SHA, base ref, base SHA, state, default
branch, and trusted default-branch SHA. Every later job depends on successful
guard completion. A caller
using `pull_request`, `workflow_dispatch`, a side-branch target, or another
event cannot reach the model credential.

The caller itself must be loaded from the trusted base and contain only the
reusable invocation:

```yaml
on:
  pull_request_target:
    branches: [master]
    types: [opened, reopened, synchronize, ready_for_review, edited]
```

The `edited` event covers retargeting, while the API guard remains the actual
authority. Never check out or execute pull-request code in this caller.

### 2. Prepare data packet

Preparation has no secrets. It checks out the guarded head with credentials
disabled only to let trusted `git` read objects as data. It does not run builds,
package managers, hooks, scripts, binaries, or any repository-controlled
program.

It records this immutable review input:

- `head_sha`
- `base_ref`
- `base_sha`
- `state`
- `review_scope: "diff_v1"`

Before writing that input or generating any diff, preparation runs
`git merge-base --is-ancestor BASE_SHA HEAD_SHA`. GitHub can report the live
default tip as PR `base.sha` while a behind-base PR head has not incorporated
that tip. A two-tree `git diff BASE_SHA HEAD_SHA` would then include reversed
default-only changes. That chronology returns `BASE_NOT_ANCESTOR`; it never
reaches Codex and never falls through to `INPUT_TRUNCATED`.

Approved guidance is read with `git show` from the guarded default-branch SHA.
`REVIEW.md` is the required always-on general lens; optional context includes
`AGENTS.md`, `docs/review.md`, the approved Convex review skill, and matching
`.review/*.md` packets. The same helper that selects packets writes
`activated-packets.json`; `general` is always present, and zero matching feature
packets reports `["general"]`.
Guidance is inserted as explicitly delimited trusted text in the generated
prompt. It is never installed as model-discoverable filesystem configuration.

Pull-request title and body are not read. No PR source or configuration file is
copied into the model working directory. Preparation creates only:

- `model-workspace/codex-prompt.md`
- `model-workspace/codex-output-schema.json`

The prompt contains exact `BASE..HEAD` name-status output and a
`--function-context --unified=20` diff, both generated with external diff and
text conversion disabled. `git diff --numstat` rejects every binary entry.
Status and diff are decoded as strict UTF-8; invalid bytes stop preparation.
Those checks occur before complete coverage can be written.

The prompt has a deterministic 2,000,000-byte cap. Coverage records original
and included prompt/diff byte counts, prompt/diff hashes, strict UTF-8 encoding,
binary absence, and whether truncation occurred. A literal `<<<BEGIN` or
`<<<END` sequence in untrusted status/diff data collides with the reserved
prompt boundaries. Preparation fails before writing a model prompt or coverage
claim and publication returns `UNTRUSTED_MARKER_COLLISION`; the raw data is not
rewritten or claimed as reviewed. Truncation is likewise never silently
reviewed: Codex is skipped and publication returns `INPUT_TRUNCATED`.

### 3. No-tools Codex

The review job has no checkout. Its working directory contains only the
generated prompt and schema, then the structured output written by the pinned
action. Codex receives these exact additional arguments:

```json
["--ephemeral", "--disable", "shell_tool", "--disable", "unified_exec"]
```

Local proof against `@openai/codex` 0.144.1 established that an unknown
`--disable` feature exits nonzero and that `shell_tool` and `unified_exec` are
both stable disabled features. Static tests lock the exact JSON-array action
input; the live canary must prove the pinned action/CLI combination still
honors it.

The prompt clearly labels the patch as untrusted data. Removing process and
source-file access prevents command execution and auto-discovered PR authority;
it does not eliminate prompt-persuasion risk. V1 bounds that residual risk with
trusted guidance, structured output validation, conservative non-clean
semantics, and later independent AND-gates. There is no source MCP, retrieval,
or changed-file browsing in V1.

### 4. Trusted publish

Publication runs in a fresh job with no model credential. Immediately before
finalization it refetches repository default branch and its commit plus PR
state, head SHA, base ref, and base SHA. It again requires PR `base.sha` to
equal the independently resolved default-branch SHA. Closed PRs, non-default
targets, retargeting, head drift, base advancement, disagreement between APIs,
or lookup failure can never publish `clean`.

It also reads the current Actions run with `actions: read`, requires exactly one
immutable reusable-workflow entry for
`happycatlabs/codex-review-workflow/.github/workflows/codex-code-review.yml`,
compares GitHub's reported `.sha` with the path suffix, and emits the
API-reported SHA. The caller's immutable `uses:` pin is the
revision policy; the reusable workflow does not accept a duplicate
caller-supplied revision claim from the same trust domain.

The job posts a fresh human-readable comment, uploads one named machine
artifact, writes a short run summary, and fails unless `verdict == "clean"`.
There is no incremental state, prior-comment marker, sticky comment, or
minimized-comment lifecycle.

GitHub documents the `referenced_workflows` field in the
[workflow run API](https://docs.github.com/en/rest/actions/workflow-runs) and
recommends a commit SHA as the safest reusable-workflow reference in
[reusing workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows).

## Machine result

Artifact name: `codex-review-result`

Artifact file: `codex-review-result.json`

```json
{
  "schema_version": "codex-review-result/v1",
  "verdict": "clean | blocking_findings | error",
  "head_sha": "40-character reviewed PR head SHA",
  "base_ref": "reviewed default branch name",
  "base_sha": "40-character reviewed base SHA",
  "state": "open",
  "review_scope": "diff_v1",
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
    "trusted_guidance_bytes": 254
  },
  "blocking_count": 0,
  "non_blocking_count": 0,
  "finding_fingerprints": [],
  "workflow_revision": "GitHub-reported reusable-workflow SHA",
  "reviewer_revision": "codex-action@SHA;codex-cli@VERSION;model@MODEL",
  "error": null
}
```

`clean` means zero model findings in a complete supplied diff. Any finding
produces `blocking_findings` and fails the job in V1, including a `RISK` tagged
`blocking: false`. The two counts preserve model classification for humans and
future policy work; they do not relax this V1 gate.

For `error`, `error` is bounded workflow-owned
`{"code":"...","reason":"..."}` text. Raw provider output is not copied into
the reason.

### Consumption contract

Artifact upload happens before the reusable run's terminal gate. Artifact
presence alone is not authority. FABLE-196 must independently require all of
the following from one run:

- terminal overall run `conclusion: success`;
- a supported schema and `review_scope: "diff_v1"`;
- complete, non-truncated, strict UTF-8, non-binary coverage;
- current head/base/state/default-branch equality;
- accepted immutable workflow and reviewer revisions;
- `verdict: "clean"`;
- the run's own expected immutable `referenced_workflows` provenance.

An accepted reusable-workflow revision is the API-reported SHA and must be an
ancestor of or equal to the reusable-workflow repository's current protected
default-branch head, or appear in an explicit allowlist committed to Fable's
protected default branch. Repository existence alone is not acceptance.

Do not parse the comment or summary for authority.

### Error codes

| Code | Meaning |
|---|---|
| `AUTH_MISSING` | No supported credential exists. |
| `AUTH_LEGACY_UNSAFE` | Only stateless `CODEX_AUTH_JSON` was available. |
| `PREPARE_FAILED` | The bounded packet could not be prepared, including binary or invalid UTF-8 diff data. |
| `BASE_NOT_ANCESTOR` | The live reviewed default-branch base is not an ancestor of the exact PR head. |
| `UNTRUSTED_MARKER_COLLISION` | Raw status or diff data contains a reserved prompt-boundary marker. |
| `REVIEW_FAILED` | Codex, provider, runner, or timeout failed. |
| `MODEL_OUTPUT_MISSING` | Codex wrote no structured output. |
| `MODEL_OUTPUT_MALFORMED` | Output was not JSON. |
| `MODEL_OUTPUT_INVALID` | Output violated schema or semantic invariants. |
| `PR_STATE_LOOKUP_FAILED` | Current PR/default-branch state could not be verified. |
| `PR_STATE_INVALID` | The PR is no longer open. |
| `BASE_BRANCH_INVALID` | Current target is not the current default branch. |
| `BASE_REF_DRIFT` | Reviewed base ref no longer matches current/default base ref. |
| `HEAD_LOOKUP_FAILED` | Current head identity was invalid. |
| `STALE_HEAD` | Current head differs from the reviewed head. |
| `STALE_BASE` | Current base SHA differs from the reviewed base. |
| `COVERAGE_INVALID` | Coverage metadata is missing or inconsistent. |
| `INPUT_TRUNCATED` | Bounded input cannot claim complete diff coverage. |
| `WORKFLOW_PROVENANCE_MISSING` | GitHub did not report one exact immutable reusable-workflow entry. |

## Finding identity

Fingerprints hash canonical `{file, line, normalized_title}` JSON. Severity and
`blocking` are intentionally excluded because they are mutable adjudication,
not finding identity. Case/whitespace-only title edits remain stable; moving a
finding or materially changing its title creates a new identity.

`CRITICAL` and `BUG` must set `blocking: true`; inconsistent output is
`MODEL_OUTPUT_INVALID`. `RISK` may carry either blocking value, but every V1
finding still makes the workflow non-clean.

## Authentication and runner

`OPENAI_API_KEY` is the only supported CI authentication path. The workflow
pins `openai/codex-action` at
`52fe01ec70a42f454c9d2ebd47598f9fd6893d56`, uses
`permission-profile: :read-only`, and sets `safety-strategy: drop-sudo`.

At that revision, the action's
[security contract](https://github.com/openai/codex-action/blob/52fe01ec70a42f454c9d2ebd47598f9fd6893d56/SECURITY.md)
requires `drop-sudo` or an unprivileged user to protect the API key. It starts a
scoped Responses proxy, removes the key from the proxy environment, and
recommends ending the job after Codex. Publication therefore uses a fresh job.
The [pinned action definition](https://github.com/openai/codex-action/blob/52fe01ec70a42f454c9d2ebd47598f9fd6893d56/action.yml)
also defines JSON-array parsing for `codex-args`.

Every job runs on ephemeral GitHub-hosted Linux (`ubuntu-24.04`). Persistent
self-hosted runners are unsupported because `drop-sudo` changes job privileges
and the credential/proxy boundary assumes a disposable host. There is no
caller-controlled runner input.

`CODEX_AUTH_JSON` is never installed or executed. Its stateless renewable OAuth
behavior is unsuitable for concurrent CI; when it is the only configured
credential, the workflow returns `AUTH_LEGACY_UNSAFE`.

## Honest V1 limit

`diff_v1` reviews supplied patch text, not complete changed-file source or the
whole repository. Even a clean result proves only that the complete bounded
diff produced no findings under the activated trusted packets. FABLE-188 must
not widen any docs-only autonomous allowlist until source scope is revisited.
Trusted source retrieval/MCP is deliberately out of scope.

## Delivery and live canaries

This repository cannot authoritatively self-reference an uncommitted SHA.
Delivery order is therefore:

1. commit and review this reusable-workflow change;
2. push it and record the immutable commit SHA;
3. update the Fable caller to the exact trigger above, pin `uses:` to that SHA,
   and pass the exact Dancer and Dependabot bot actors used to author reviewed
   work and immutable-pin update PRs;
4. run the live canaries below;
5. only then consider the result as candidate merge authority.

Required canaries:

1. clean structured output on an unchanged head/base;
2. concrete finding produces `blocking_findings` and a failed run;
3. malformed/missing model output produces `error`;
4. head push during review produces `STALE_HEAD`;
5. default-branch advance during review produces `STALE_BASE`;
6. close or retarget during review fails state/base checks;
7. side-branch target never reaches the credential-bearing review job;
8. a live base.sha/ancestor canary keeps a PR head behind a new default-only
   commit and proves preparation returns `BASE_NOT_ANCESTOR`, not
   `INPUT_TRUNCATED`;
9. oversized input skips Codex and produces `INPUT_TRUNCATED`;
10. binary and invalid UTF-8 fixtures fail preparation;
11. a raw `<<<END` or `<<<BEGIN` sequence returns
    `UNTRUSTED_MARKER_COLLISION` before Codex runs;
12. a honeypot diff containing commands, fake instructions, and references to a
    sentinel executable/file cannot execute or read either;
13. pinned action plus CLI accepts the structured-output invocation with both
    execution features disabled.

Until the immutable pin and canaries exist, FABLE-195 is not Done and this
artifact is not required merge authority.

## Local validation

Tests extract and execute the exact helper embedded in the workflow:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
actionlint -color .github/workflows/codex-code-review.yml
git diff --check
```

Fixtures cover packet selection, prompt boundaries/caps/encoding, binary
rejection, clean and finding results, malformed output, severity consistency,
stable fingerprints, exact state/head/base chronology, immutable provenance,
credential isolation, action arguments, and absence of model-visible PR files.
