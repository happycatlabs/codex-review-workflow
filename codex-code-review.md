# `codex-code-review` workflow — architecture and extension guide

Companion to `.github/workflows/codex-code-review.yml`. Read this before changing the workflow, the prompt, the auth path, or the comment-publishing logic. This workflow is the PR review gate for every consumer repo — getting it wrong stalls every PR across the org.

This doc is for agents (and humans). It explains *why* the workflow is shaped the way it is, where the load-bearing pieces are, and the patterns to follow when extending it.

## Repo layout

This is a `workflow_call`-only reusable workflow. Consumer repos in `happycatlabs/*` invoke it from a thin caller:

```yaml
# Consumer: .github/workflows/codex-code-review.yml
on:
  pull_request:
    types: [opened, reopened, synchronize, ready_for_review]
jobs:
  review:
    uses: happycatlabs/codex-review-workflow/.github/workflows/codex-code-review.yml@main
    secrets: inherit
    # Optional inputs — see the workflow's on.workflow_call.inputs for the
    # full list. Defaults work for most repos.
    # with:
    #   sentry-project: my-project
    #   sentry-ticket-regex: '\bMYREPO-\d+\b'
```

The consumer repo provides:

1. The thin caller above.
2. `CODEX_AUTH_JSON` secret (contents of `~/.codex/auth.json` from `codex login` on a developer machine).
3. Optional: a `REVIEW.md` at the repo root and/or `.review/*.md` packets for project-specific context. The workflow reads these at runtime.
4. Optional: `SENTRY_AUTH_TOKEN` secret if the repo passes `sentry-project` as an input.

## What the workflow does

On every non-draft PR event (`opened` / `reopened` / `synchronize` / `ready_for_review`):

1. Checks out the PR merge commit and fetches base/head refs.
2. Resolves *incremental review state* from a sticky PR comment.
3. Builds an isolated review workspace containing only the files the model is allowed to look at.
4. Builds a structured-output review prompt (gpt-5.5-shaped) with the diff, prior-review context, and PR metadata.
5. Runs `codex exec` against a Codex/ChatGPT subscription (no API key) with the prompt and a JSON output schema.
6. Posts a fresh sticky comment carrying the verdict + a hidden state marker, then minimizes the previous sticky as `OUTDATED`.
7. Gates the PR on the JSON `result` field (`NO_ISSUES` passes; anything else fails).

The whole thing is one job, top-to-bottom, in a single workflow file.

## State machine: sticky-comment review state

The workflow does *incremental* reviews on subsequent pushes. To do that, it needs to remember:

- The SHA it last reviewed (so it can compute "files changed since last review")
- The prior review's `{result, comment_body}` JSON (so the model can dedupe findings and treat the prior review as accepted unless overridden)

Both are stored in a hidden marker at the bottom of the active sticky comment:

```
<!-- codex-review-state:v1 sha=<HEAD_SHA> state=<base64-encoded-JSON> -->
```

HTML comments render invisibly in the GitHub Markdown view. The base64 encoding keeps the JSON safe from Markdown escaping concerns.

### Lookup

The "Resolve incremental review state" step paginates the PR's comments and picks the most recently *updated* comment that contains the marker:

```
[.[] | select(.body | contains("<!-- codex-review-state:v1"))] | sort_by(.updated_at) | last
```

It then captures both the integer `id` (for REST endpoints) and the `node_id` (the GraphQL global ID, needed for `minimizeComment`).

### Partition

Given `LAST_REVIEWED_SHA`, the workflow computes:

- `FOCUS_FILES` = files changed in `LAST_REVIEWED_SHA..HEAD` — the model analyzes these in detail.
- `PRIOR_FILES` = files changed in `BASE..LAST_REVIEWED_SHA` — already reviewed; the model only re-evaluates them if a new commit elsewhere makes a previously-invisible issue surface.

The full `BASE..HEAD` diff is still passed to the model. Cross-commit ripple effects stay visible. We *steer attention* with `[FOCUS]` / `[PRIOR]` markers in the changed-files manifest; we don't hard-exclude. (LLMs are reliably better at attention-steering than skip-instructions.)

### Fallback to full review

A full review (`FOCUS_FROM_SHA = BASE_SHA`, no Prior-review section) is the fallback when:

- No sticky comment exists yet (first review on the PR).
- The recorded SHA is not in the current git history (rebase or force-push orphaned it). Detected via `git cat-file -e`.
- PR title or any new commit message contains `[full-review]` (manual escape hatch).

When extending, **always preserve the full-review fallback**. It's the safety net that makes the incremental path tolerable.

### Publish: post-then-minimize, not update-in-place

We tried PATCHing the sticky comment in place. Don't go back to that — see the rationale section below.

The current pattern: each run POSTs a fresh comment with the new marker, then collapses the previous sticky via the `minimizeComment` GraphQL mutation with `classifier: OUTDATED`. The previous comment stays in the timeline as a "marked as outdated" line reviewers can expand for history; the new comment lands at the bottom of the timeline, anchored to the commits it actually describes.

## Auth: ChatGPT subscription, not API key

`openai/codex-action@main` only accepts `openai-api-key` as auth and offers no input for ChatGPT-subscription auth (see [openai/codex#3820](https://github.com/openai/codex/issues/3820), open). To bill against a Codex subscription, the workflow drops the action and invokes `codex exec` directly:

1. Install the CLI: `npm install -g @openai/codex@<pinned>`.
2. Write `~/.codex/auth.json` from the `CODEX_AUTH_JSON` repo secret (mode 600).
3. Run `codex exec` with `--model`, `--sandbox`, `--cd`, `--output-schema`, `--output-last-message`, and the prompt piped via stdin.

`CODEX_AUTH_JSON` is the entire content of `~/.codex/auth.json` from a developer's machine after running `codex login`. To refresh:

```
codex login                                   # generates ~/.codex/auth.json
gh secret set CODEX_AUTH_JSON \
  -R happycatlabs/fable < ~/.codex/auth.json  # never prints the token
```

The OAuth refresh token in the file is long-lived (months). When it expires, the workflow fails with a clear codex-CLI error and the secret needs re-rotating. There's no auto-refresh in CI.

### Model selection

ChatGPT-subscription auth restricts which models the API will accept. Models like `gpt-5.4-2026-03-05` are API-only and return:

```
"The 'gpt-5.4-2026-03-05' model is not supported when using Codex with a ChatGPT account."
```

Use a subscription-eligible model (currently `gpt-5.5`). Verify the model is available via `codex` docs ([developers.openai.com/codex/models](https://developers.openai.com/codex/models)) before changing.

## Prompt design

The prompt follows OpenAI's [gpt-5.5 prompting guidance](https://developers.openai.com/api/docs/guides/prompt-guidance?model=gpt-5.5). Two core principles:

1. **Outcome-first, not process-heavy.** Define the destination, success criteria, and constraints; let the model choose the path. Don't spell out the procedure.
2. **Decision rules over absolutes.** Reserve `ALWAYS` / `NEVER` / `MUST` for true invariants (safety, schema, contracts). Use decision rules for judgment calls.

### Section layout

The prompt has stable sections in this order:

| Section | Purpose | When to edit |
|---|---|---|
| Role | One-line framing of who the model is and what it produces. | Rarely. Only if the gating output contract changes shape. |
| Personality | Tone and demeanor (terse, specific, no hedging, don't explain code). | When you want different review *style*, not different *behavior*. |
| Goal | What outcome the review produces. Anchors "no issues found is preferred." | Rarely. |
| Workspace | What files the model can see, including `[FOCUS]`/`[PRIOR]` markers. | When you add new context files the workspace prep copies in. |
| Review mode | How to behave on first review vs re-review. | When the incremental-review semantics change. |
| Project context | Repo-specific rules (allowlist, Convex skill, Sentry, self-review exclusion). | When repo-specific surfaces are added/removed. |
| What counts as a finding | The bar — file, line, exact code, triggering input. | Rarely. |
| Severity | CRITICAL / BUG / RISK definitions. | When severity semantics change. |
| Invariants | True absolutes: diff-is-source-of-truth, code movement, Convex backwards-compat, repo structure, output schema. | Add when a new invariant emerges from a real incident. |
| Decision rules | Judgment calls: speculation filter, comments-as-evidence, root-cause framing, etc. | Most extensions go here. |
| Don't suggest | Style preferences that aren't findings. | When you find a recurring false-positive class. |
| Output | The JSON shape and the comment-body shape. | When the output schema changes. |
| Stop | Termination conditions. | Rarely. |

### Where to add a new rule

When a finding pattern recurs and you want to teach the model:

- **Is it a true invariant?** (Will violation always be a problem?) → add to **Invariants**, mark severity, give the failure mode.
- **Is it a judgment call?** (Whether it's a problem depends on the diff context) → add to **Decision rules** as a one-line decision pattern.
- **Is it a *domain rule* the model wouldn't infer?** (Convex backwards-compat, repo structure) → goes in **Invariants** if hard, **Project context** if soft.
- **Is it a class of false positive?** → add to **Don't suggest** with one short justification.

Avoid duplicating across sections — the model will weight any rule listed twice. If you find yourself wanting to repeat, that's a sign the rule belongs in only one section and you weren't sure which.

### Heredoc gotcha

The prompt body is built with `cat <<'PROMPT' > ...` (single-quoted PROMPT). This means **no shell interpolation inside the body** — `${VAR}` strings are literal. Move all dynamic interpolation (PR title, body, SHAs, repo, diff) into the post-heredoc `{ echo ...; }` block, where vars expand normally. Treating the body as static is intentional: it keeps the prompt stable, cacheable, and reviewable as a unit.

## Workspace isolation

The model never sees the full repo. The "Prepare isolated review workspace" step copies in:

- A small set of required context files (`AGENTS.md`, `REVIEW.md`, `docs/review.md`, selected skill files).
- `.review/` — feature-scoped review packets, picked per-diff via YAML frontmatter `applies_to`.
- The PR-changed files at HEAD (only).
- A few `.github/tmp/*` files: changed-files manifest with `[FOCUS]`/`[PRIOR]` tags, prior review JSON, new-commit messages, sentry context.
- A `CLAUDE.md` symlink to `AGENTS.md` (codex CLI conventions).

Then it deletes the full checkout (`Remove full checkout before review` step) so the codex sandbox can't reach repo files outside the workspace.

When extending, **respect this isolation**. If you want the model to read a new file, copy it into the workspace explicitly. Don't widen the codex sandbox.

## Common change patterns

### Add a new context file the model should read

1. Copy it into the workspace in `Prepare isolated review workspace` (extend `required_files` or add a `copy_dir` call).
2. Mention the file in the prompt's **Workspace** section so the model knows it's available.
3. If it carries hard rules, mention "Read X before reviewing — overrides generic instincts" in **Project context**.

### Tighten a recurring false-positive

Add a one-liner to **Don't suggest** with a short justification. Don't add a verbose anti-pattern list — the gpt-5.5 guidance pushes against process-heavy scaffolding. If the model still flags it, the issue is the diff context, not the prompt.

### Change the model

1. Confirm the new model is available on ChatGPT-subscription auth (test by running `codex exec --model <new>` locally with `~/.codex/auth.json` in place).
2. Update `--model` in the `Run Codex structured review` step.
3. Re-skim the prompt — gpt-5.5 prefers outcome-first; older models might need more procedural hand-holding. Don't migrate verbatim across model generations.

### Change the output contract

If the JSON schema changes (`{result, comment_body}` becomes something else):

1. Update `Generate structured output schema` step.
2. Update the prompt's **Output** section.
3. Update `Inspect structured output`, `Publish review comment`, and `Gate PR on Codex result` to read the new fields.
4. Update the sticky marker payload (`state=<base64-of-new-shape>`) — old comments with the v1 marker will still parse but the body inside won't match what the new lookup expects. Bump the marker version (`codex-review-state:v2`) and have the lookup tolerate both.

### Change incremental-review semantics

If you want different scope rules (e.g., re-review files within N hops of changed files):

1. Edit the FOCUS/PRIOR computation in `Resolve incremental review state`.
2. Update the **Review mode** section in the prompt to match.
3. Bump the marker version if you also change the on-disk state shape.

## Local development

This repo (`happycatlabs/codex-review-workflow`) is the source of truth. Develop here, then validate against a consumer repo's PR.

```
git clone https://github.com/happycatlabs/codex-review-workflow.git /tmp/codex-review-workflow
cd /tmp/codex-review-workflow
# edit .github/workflows/codex-code-review.yml
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/codex-code-review.yml'))"  # syntax check
```

To test changes against a real consumer before merging to `main`, push your edits to a branch on this repo, then on the consumer repo open a PR whose caller pins to that branch:

```yaml
uses: happycatlabs/codex-review-workflow/.github/workflows/codex-code-review.yml@my-test-branch
```

Once the consumer PR's review run looks right, merge here, and the consumer can flip its caller back to `@main` (or pin to a tag, if you start tagging releases).

### Validating without paying for a CI run

You can run the prompt-build locally if you populate the env vars and have a checkout of a consumer repo:

```
export REPOSITORY=<consumer-org>/<consumer-repo>
export PR_NUMBER=<n>
export BASE_SHA=$(git rev-parse origin/<base-branch>)
export HEAD_SHA=$(git rev-parse HEAD)
export CHECKOUT_DIR=/path/to/consumer-checkout
export REVIEW_WORKSPACE=/tmp/codex-workspace
# extract the relevant `run:` blocks from this workflow and execute them
```

Then run `codex exec --model gpt-5.5 ... < /tmp/codex-workspace/codex-prompt.md` against your own `~/.codex/auth.json`.

## Debugging a failed run

The workflow uploads everything needed to reproduce a review under `Upload Codex artifacts`:

- `codex-prompt.md` — the exact prompt the model received.
- `codex-output-schema.json` — the JSON schema the output had to satisfy.
- `codex-output.json` — what the model produced (or empty if the run failed before output).
- `.github/tmp/changed-files.txt` — the FOCUS/PRIOR-tagged manifest.
- `.github/tmp/prior-review.json` — what was loaded from the previous sticky.
- `.github/tmp/new-commit-messages.txt` — commits since the last review.
- `.github/tmp/incremental-mode.txt` — `incremental` or `full`.
- `.github/tmp/sentry-review-context.md` — Sentry data passed in (if any).

Download via `gh run download <run-id> -R <consumer-repo> -n codex-review-artifacts`.

### Common failure modes

| Symptom | Likely cause |
|---|---|
| `model is not supported when using Codex with a ChatGPT account` | API-only model used; pick a subscription-eligible one. |
| `Codex output file missing` | The codex run errored before writing JSON; check the step's stderr. |
| Sticky comment lookup returned `null` despite a comment existing | Marker was lost (someone edited the body and removed the HTML comment). Falls back to full review — non-fatal but worth investigating. |
| `404` on private package fetch | Bun auth path; verify project-local `.npmrc` is being written. |

## Why some choices were made

These are decision records, not code descriptions. Don't undo them without a stronger reason than the original.

- **Subscription auth, not API key**: API-key billing was hitting quota. The Codex subscription includes generous review-length usage at no incremental cost.
- **`codex exec` directly, not `openai/codex-action@main`**: the action only accepts `openai-api-key` (no subscription input).
- **`gpt-5.5`, not `gpt-5.4-*`**: subscription auth restricts to a curated set; `gpt-5.5` is currently the strongest available.
- **gpt-5.5 prompt structure**: the prior prompt was tuned for an older model and over-specified procedure. gpt-5.5 chooses solution paths efficiently and degrades when over-instructed.
- **Sticky comment as state store**: simplest persistence that survives across runs and is debuggable in the PR UI itself. Workflow artifacts and a custom GitHub Check are cleaner alternatives if you outgrow this — but only do it once you've validated the prompt shape.
- **Post-then-minimize, not PATCH-in-place**: PATCHing kept the verdict at its original timeline position, anchoring it above newer commits it actually described. Posting a fresh comment per run keeps the verdict at the bottom of the timeline, alongside the commits it covers; minimizing the previous sticky as OUTDATED preserves history without clutter.
- **Attention markers (`[FOCUS]`/`[PRIOR]`), not hard exclusions**: LLMs reliably ignore "skip this file" when they can see the file content. Steering attention works; hard exclusions cause flaky output.
- **Full BASE..HEAD diff always passed**: cross-commit ripple effects (e.g., commit B adds a caller of a function broken in commit A) only catch if the model can see both. Narrowing the diff to just FOCUS files would save tokens but lose this — the cost is acceptable.
- **`[full-review]` escape hatch in PR title or commit message**: the smallest possible mechanism to force a full re-review without setting up an `issue_comment` event handler. Document it in the PR template if you want it discoverable.

## References

- [OpenAI gpt-5.5 prompting guidance](https://developers.openai.com/api/docs/guides/prompt-guidance?model=gpt-5.5)
- [Codex CLI reference](https://developers.openai.com/codex/cli/reference) (also vendored at `incident-platform-docs/codex-cli-reference.md` in the parent workspace)
- [openai/codex#3820](https://github.com/openai/codex/issues/3820) — open feature request for ChatGPT-subscription auth in `codex-action`
- [oven-sh/bun#13764](https://github.com/oven-sh/bun/issues/13764), [#14553](https://github.com/oven-sh/bun/issues/14553) — Bun .npmrc bugs
- [GitHub `minimizeComment` GraphQL mutation](https://docs.github.com/en/graphql/reference/mutations#minimizecomment)
