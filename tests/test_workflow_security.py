from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/codex-code-review.yml"
README = ROOT / "README.md"
ARCHITECTURE = ROOT / "codex-code-review.md"
CONTRACT = ROOT / "src/review_contract.py"
SOURCE = ROOT / "src/source_context.py"
INTENT = ROOT / "src/intent_context.py"
SCHEMA = ROOT / "src/codex-output-schema.json"
PUBLICATION = ROOT / "src/review_publication.py"
PUBLISHER = ROOT / "src/review_publisher.py"
RESOLUTION = ROOT / "src/review_resolution.py"
RESOLVER = ROOT / "src/review_resolver.py"
BASE_PROVENANCE = ROOT / "src/base_provenance.py"
CODEX_ACTION_SHA = "52fe01ec70a42f454c9d2ebd47598f9fd6893d56"
APP_TOKEN_ACTION_SHA = "bcd2ba49218906704ab6c1aa796996da409d3eb1"
EXPECTED_WORKFLOW_PATH = (
    "happycatlabs/codex-review-workflow/.github/workflows/codex-code-review.yml"
)


class WorkflowSecurityTests(unittest.TestCase):
    def setUp(self):
        self.workflow = WORKFLOW.read_text()

    def job(self, name: str, next_name: str | None = None) -> str:
        block = self.workflow.split(f"  {name}:\n", 1)[1]
        return block.split(f"\n  {next_name}:\n", 1)[0] if next_name else block

    def test_eight_bounded_jobs_and_ephemeral_runners(self):
        jobs = re.findall(r"^  ([a-z][a-z0-9-]+):$", self.workflow, re.MULTILINE)
        self.assertEqual(
            jobs,
            [
                "trust-guard",
                "prepare",
                "intent",
                "review",
                "publish",
                "resolution-prepare",
                "resolution-review",
                "resolution-apply",
            ],
        )
        runs_on = re.findall(r"^    runs-on: (.+)$", self.workflow, re.MULTILINE)
        self.assertEqual(runs_on, ["blacksmith-2vcpu-ubuntu-2404"] * 8)
        self.assertNotIn("inputs.runner", self.workflow)
        self.assertNotIn("SENTRY", self.workflow.upper())

    def test_base_provenance_guard_precedes_all_sensitive_jobs(self):
        guard = self.job("trust-guard", "prepare")
        for text in (
            'if [ "${EVENT_NAME}" != pull_request_target ]',
            "review_publisher.py prove-generation",
            '--repository "${REPOSITORY}"',
            '--pull-number "${PR_NUMBER}"',
            "base_provenance_b64",
        ):
            self.assertIn(text, guard)
        self.assertNotIn(
            'if [ "${base_ref}" != "${default_branch}" ]', guard
        )
        self.assertNotIn("secrets.", guard)
        self.assertIn("needs: [trust-guard, prepare]", self.job("intent", "review"))
        self.assertIn(
            "needs: [trust-guard, prepare, intent]", self.job("review", "publish")
        )
        self.assertIn(
            "needs: [trust-guard, prepare, intent, review]", self.job("publish")
        )

    def test_trusted_helper_is_checked_out_at_exact_workflow_identity(self):
        for name, next_name in (
            ("trust-guard", "prepare"),
            ("prepare", "intent"),
            ("intent", "review"),
            ("publish", "resolution-prepare"),
            ("resolution-prepare", "resolution-review"),
        ):
            block = self.job(name, next_name)
            self.assertIn("repository: ${{ job.workflow_repository }}", block)
            self.assertIn("ref: ${{ job.workflow_sha }}", block)
            self.assertIn("persist-credentials: false", block)
            self.assertIn("sparse-checkout: src", block)
            expected_helper = {
                "trust-guard": "trusted-workflow/src/review_publisher.py",
                "resolution-prepare": "trusted-workflow/src/review_resolution.py",
            }.get(name, "trusted-workflow/src/review_contract.py")
            self.assertIn(expected_helper, block)
        self.assertNotIn("# BEGIN REVIEW_CONTRACT", self.workflow)
        self.assertNotIn("contract/review_contract.py", self.workflow)

    def test_candidate_checkout_and_credentials_never_share_execution_authority(self):
        prepare = self.job("prepare", "intent")
        intent = self.job("intent", "review")
        review = self.job("review", "publish")
        publish = self.job("publish", "resolution-prepare")
        self.assertIn("Checkout exact pull request head as data", prepare)
        self.assertNotIn("secrets.", prepare)
        self.assertNotIn("repo-checkout", intent)
        self.assertNotIn("actions/checkout", review)
        self.assertNotIn("LINEAR_CLIENT", review)
        self.assertNotIn("LINEAR_CLIENT", publish)
        self.assertNotIn("DANCER_", review)
        self.assertIn("LINEAR_CLIENT_ID: ${{ secrets.LINEAR_CLIENT_ID }}", intent)
        self.assertIn(
            "LINEAR_CLIENT_SECRET: ${{ secrets.LINEAR_CLIENT_SECRET }}", intent
        )
        self.assertIn("secrets.OPENAI_API_KEY", review)
        self.assertNotIn("OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}", review)

    def test_stacked_base_is_never_checked_out_or_executed(self):
        prepare = self.job("prepare", "intent")
        guard = self.job("trust-guard", "prepare")
        self.assertIn("Checkout exact pull request head as data", prepare)
        self.assertIn("ref: ${{ needs.trust-guard.outputs.head_sha }}", prepare)
        self.assertNotIn("ref: ${{ needs.trust-guard.outputs.base_ref }}", prepare)
        self.assertNotIn("ref: ${{ needs.trust-guard.outputs.base_sha }}", prepare)
        self.assertNotIn("actions/checkout", prepare.split(
            "      - name: Checkout exact pull request head as data\n", 1
        )[1].split("      - name: Seal trusted base provenance", 1)[0].replace(
            "uses: actions/checkout", ""
        ))
        self.assertNotIn("run:", guard.split(
            "      - name: Checkout exact reusable-workflow source\n", 1
        )[1].split("      - name: Resolve current trusted pull request state", 1)[0])
        self.assertIn(
            '["git", "-C", str(checkout), "show", f"{default_sha}:{rel_path}"]',
            prepare,
        )

    def test_complete_ancestry_uses_sealed_provenance_before_model_input(self):
        prepare = self.job("prepare", "intent")
        sealed = "      - name: Seal trusted base provenance\n"
        ancestry = "      - name: Require complete root-to-target ancestry\n"
        build = "      - name: Build trusted guidance and untrusted diff data\n"

        self.assertLess(prepare.index(sealed), prepare.index(ancestry))
        self.assertLess(prepare.index(ancestry), prepare.index(build))
        ancestry_step = prepare.split(ancestry, 1)[1].split(build, 1)[0]
        self.assertIn(
            'python3 "trusted-workflow/src/base_provenance.py" check-ancestry',
            ancestry_step,
        )
        self.assertIn(
            '--provenance "${METADATA_DIR}/base-provenance.json"',
            ancestry_step,
        )
        self.assertNotIn("review_contract.py", ancestry_step)
        self.assertNotIn("--base-sha", ancestry_step)
        self.assertNotIn("--head-sha", ancestry_step)
        self.assertEqual(prepare.count("base64.b64decode"), 1)
        self.assertIn("for directory in (staging, trusted):", prepare)
        self.assertNotIn("for directory in (staging, trusted, metadata):", prepare)

    def test_ticket_lookup_is_exact_and_not_prompt_controlled(self):
        intent_job = self.job("intent", "review")
        intent_code = INTENT.read_text()
        self.assertIn("LINEAR_TEAM_KEY: ${{ inputs.linear-team-key }}", intent_job)
        self.assertIn('--team-key "${LINEAR_TEAM_KEY}"', intent_job)
        self.assertIn('--repository "${GITHUB_REPOSITORY}"', intent_job)
        credential_step = intent_job.split(
            "      - name: Resolve one owner-bound Linear ticket\n", 1
        )[1].split("\n      - name: Build bounded prompt without credentials\n", 1)[0]
        shell = credential_step.split("        run: |\n", 1)[1]
        self.assertNotIn("${{ inputs.", shell)
        self.assertNotIn("github.event.pull_request.body", self.workflow)
        self.assertNotIn("github.event.pull_request.title", self.workflow)
        self.assertIn('{"query": query, "variables": {"identifier": ticket}}', intent_code)
        self.assertNotIn('"team": team_key', intent_code)
        self.assertIn('repository != "happycatlabs/fable"', intent_code)
        self.assertIn("TICKET_CONTEXT_TEAM_MISMATCH", CONTRACT.read_text())

    def test_review_has_generated_prompt_only_and_no_execution_tools(self):
        intent = self.job("intent", "review")
        review = self.job("review", "publish")
        self.assertIn("Assert model filesystem contains generated files only", intent)
        self.assertIn('"${MODEL_WORKSPACE}/codex-output-schema.json"', intent)
        self.assertIn('"${MODEL_WORKSPACE}/shards/${shard_id}/codex-prompt.md"', intent)
        self.assertIn(
            "working-directory: codex-review-input/model-workspace/shards/"
            "${{ matrix.shard_id }}",
            review,
        )
        self.assertIn("permissions: {}", review)
        self.assertIn(f"uses: openai/codex-action@{CODEX_ACTION_SHA}", review)
        self.assertIn("permission-profile: ':read-only'", review)
        self.assertIn("Create unprivileged Codex user", review)
        self.assertIn("sudo chmod 755 /home/codex-review", review)
        self.assertIn("safety-strategy: unprivileged-user", review)
        self.assertIn("codex-user: codex-review", review)
        self.assertNotIn("safety-strategy: drop-sudo", review)
        self.assertNotIn("codex-home:", review)
        self.assertIn(
            "codex-args: '[\"--ephemeral\",\"--disable\",\"shell_tool\","
            "\"--disable\",\"unified_exec\"]'",
            review,
        )
        for forbidden in ("actions/checkout", "run: bun", "run: npm", "repo-checkout"):
            self.assertNotIn(forbidden, review)

    def test_model_and_reasoning_effort_are_explicit(self):
        header = self.workflow.split("jobs:\n", 1)[0]
        review = self.job("review", "publish")
        resolution = self.job("resolution-review", "resolution-apply")
        publish = self.job("publish", "resolution-prepare")

        self.assertIn("default: gpt-5.6-sol", header)
        self.assertIn("default: none", header)
        for model_job in (review, resolution):
            self.assertIn("model: ${{ inputs.model }}", model_job)
            self.assertIn("effort: ${{ inputs.effort }}", model_job)
        self.assertIn(
            "model@${{ inputs.model }};effort@${{ inputs.effort }}", publish
        )

        docs = README.read_text() + ARCHITECTURE.read_text()
        self.assertIn("| `model` | `gpt-5.6-sol` |", docs)
        self.assertIn("| `effort` | `none` |", docs)
        self.assertIn("model@MODEL;effort@EFFORT", docs)

    def test_source_and_prompt_contracts_are_bounded_and_fail_closed(self):
        contract = CONTRACT.read_text()
        source = SOURCE.read_text()
        for text in (
            "MAX_PROMPT_BYTES = 2_000_000",
            "MAX_MODEL_PROMPT_BYTES = 900_000",
            "MAX_REVIEW_SHARDS = 16",
            "UNTRUSTED_MARKER_COLLISION",
            "INPUT_TRUNCATED",
            "SOURCE_CONTEXT_TRUNCATED",
            "TICKET_CONTEXT_STALE",
        ):
            self.assertIn(text, contract)
        for text in (
            "MAX_CONTEXT_BYTES = 1_250_000",
            "MAX_CONTEXT_FILES = 150",
            "MAX_SCAN_BYTES = 40_000_000",
            "LOOKUP_TIMEOUT_SECONDS = 20",
            'specifier.startswith("@/")',
            "unresolved internal root-alias import",
            "IGNORED_ALIAS_PREFIXES",
            '"git", "-C", str(repository), "cat-file", "blob"',
        ):
            self.assertIn(text, source)
        self.assertIn("--no-ext-diff", self.workflow)
        self.assertIn("--no-textconv", self.workflow)
        self.assertIn('"--no-renames", "--no-ext-diff"', self.workflow)
        self.assertIn(
            '["diff", "--function-context", "--unified=20", "--find-renames",\n'
            '               "--no-ext-diff", "--no-textconv", base_sha, head_sha],',
            self.workflow,
        )
        self.assertIn("build-review-shards", self.workflow)
        self.assertIn('--comment-map "${METADATA_DIR}/comment-map.json"', self.workflow)
        self.assertIn("combine-review-shards", self.workflow)
        self.assertIn("strategy:\n      fail-fast: false\n      matrix:", self.workflow)
        self.assertIn("--lookup-context", self.workflow)

    def test_publisher_refetches_identity_and_proves_workflow_revision(self):
        publish = self.job("publish", "resolution-prepare")
        for text in (
            "review_publisher.py prove-generation",
            "base-provenance.json",
            'actions/runs/${GITHUB_RUN_ID}',
            'run.get("referenced_workflows", [])',
            EXPECTED_WORKFLOW_PATH,
            "WORKFLOW_PROVENANCE_MISSING",
            "STALE_HEAD",
            "STALE_BASE",
            "BASE_REF_DRIFT",
        ):
            self.assertIn(text, publish)
        self.assertNotIn("inputs.workflow-revision", publish)
        self.assertIn('if [ "${verdict}" != clean ]', publish)
        self.assertIn('[ "${publication}" != published ]', publish)

    def test_publication_uses_brokered_dancer_token_without_actions_fallback(self):
        publish = self.job("publish", "resolution-prepare")
        publisher = PUBLISHER.read_text()
        publisher_token = publish.split(
            "      - name: Mint repository-scoped Dancer publisher token\n", 1
        )[1].split(
            "\n      - name: Revalidate and publish Dancer COMMENT review\n", 1
        )[0]
        self.assertIn(
            f"uses: actions/create-github-app-token@{APP_TOKEN_ACTION_SHA}", publish
        )
        self.assertIn("permission-contents: read", publisher_token)
        self.assertNotIn("permission-contents: write", publisher_token)
        self.assertIn("permission-pull-requests: write", publisher_token)
        self.assertIn("pull-requests: read", publish)
        self.assertIn(
            "DANCER_GITHUB_TOKEN: ${{ steps.dancer-token.outputs.token }}", publish
        )
        dancer_step = publish.split(
            "      - name: Revalidate and publish Dancer COMMENT review\n", 1
        )[1].split("\n      - name: Upload exact-head machine result\n", 1)[0]
        self.assertNotIn("github.token", dancer_step)
        self.assertNotIn("GH_TOKEN", dancer_step)
        self.assertIn("DANCER_PRIVATE_KEY:", self.workflow)
        self.assertNotIn("DANCER_PRIVATE_KEY", self.job("review", "publish"))
        self.assertIn('"event": "COMMENT"', PUBLICATION.read_text())
        self.assertIn('"COMMENTED"', publisher)
        self.assertIn("EXPECTED_DANCER_LOGIN", publisher)
        self.assertIn("EXPECTED_DANCER_ACTOR_ID", publisher)
        self.assertIn("GITHUB_422", publisher)
        self.assertIn("STALE_BEFORE_PUBLICATION", publisher)
        self.assertNotIn("APPROVE", publisher)
        self.assertNotIn("REQUEST_CHANGES", publisher)
        self.assertNotIn("issues: write", publish)

    def test_publisher_reads_identified_review_and_inline_comment_evidence(self):
        publisher = PUBLISHER.read_text()
        self.assertIn('/pulls/{pull_number}/reviews"', publisher)
        self.assertIn('/pulls/{pull_number}/reviews/{review_id}"', publisher)
        self.assertIn('/reviews/{review_id}/comments"', publisher)
        self.assertIn('/pulls/comments/{comment_id}"', publisher)
        self.assertNotIn('/pulls/{pull_number}/comments"', publisher)
        self.assertIn("PUBLICATION_MARKER", publisher)
        self.assertIn("request_sha256", publisher)
        self.assertIn("PUBLICATION_READBACK_FAILED", publisher)
        self.assertIn("current_generation(client, repository, pull_number) != observed", publisher)
        self.assertIn("publication-receipt.json", self.workflow)
        self.assertIn("base_provenance.validate_base_provenance", PUBLISHER.read_text())
        self.assertIn('"base_provenance": proven_base', PUBLISHER.read_text())

    def test_missing_publication_helper_fails_without_posting_partial_summary(self):
        publish = self.job("publish", "resolution-prepare")
        fallback = publish.split(
            "      - name: Plan COMMENT review publication\n", 1
        )[1].split("\n      - name: Mint repository-scoped Dancer publisher token\n", 1)[0]
        self.assertIn('status:"failed"', fallback)
        self.assertIn('code:"COMMENT_HELPER_MISSING"', fallback)
        self.assertIn('fallback_reason:"COMMENT_HELPER_MISSING"', publish)

    def test_model_cannot_choose_github_review_coordinates_or_event(self):
        schema = SCHEMA.read_text()
        for forbidden in ('"side"', '"start_side"', '"position"', '"event"'):
            self.assertNotIn(forbidden, schema)
        for required in ('"file"', '"start_line"', '"line"', '"body"'):
            self.assertIn(required, schema)
        self.assertIn('"event": "COMMENT"', PUBLICATION.read_text())
        self.assertIn('"side": "RIGHT"', PUBLICATION.read_text())
        self.assertNotIn("Fingerprint:", PUBLICATION.read_text())

    def test_resolution_is_non_gating_and_separates_model_from_dancer(self):
        prepare = self.job("resolution-prepare", "resolution-review")
        review = self.job("resolution-review", "resolution-apply")
        apply = self.job("resolution-apply")
        resolver = RESOLVER.read_text()
        contract = RESOLUTION.read_text()

        self.assertIn("needs: [trust-guard, publish]", prepare)
        self.assertIn("continue-on-error: true", prepare)
        self.assertIn("permissions: {}", review)
        self.assertIn(f"uses: openai/codex-action@{CODEX_ACTION_SHA}", review)
        self.assertIn(
            'codex-args: \'["--ephemeral","--disable","shell_tool",'
            '"--disable","unified_exec"]\'',
            review,
        )
        self.assertNotIn("DANCER_", review)
        self.assertNotIn("actions/checkout", review)
        self.assertNotIn("OPENAI_API_KEY", apply)
        self.assertNotIn("actions/checkout", apply)
        self.assertIn(
            f"uses: actions/create-github-app-token@{APP_TOKEN_ACTION_SHA}", apply
        )
        resolver_token = apply.split(
            "      - name: Mint repository-scoped Dancer resolver token\n", 1
        )[1].split(
            "\n      - name: Revalidate and apply fixed thread mutations\n", 1
        )[0]
        self.assertIn("permission-contents: write", resolver_token)
        self.assertNotIn("permission-contents: read", resolver_token)
        self.assertIn("permission-pull-requests: write", resolver_token)
        self.assertIn("codex-review-resolution-receipt.json", apply)
        self.assertNotIn("codex-review-result.json.tmp", apply)
        self.assertIn("MAX_CANDIDATES = 20", contract)
        self.assertIn("KEEP_STILL_VALID", contract)
        self.assertIn("RESOLVE_MUTATION", resolver)
        self.assertIn(
            "resolveReviewThread(input: {threadId: $threadId, clientMutationId: $clientMutationId})",
            resolver,
        )
        self.assertIn("resolvedBy", resolver)
        self.assertNotIn("... on Bot", resolver)
        self.assertIn("GRAPHQL_DANCER_LOGIN", resolver)
        self.assertIn("require_dancer_actor", resolver)
        self.assertIn("workflow_sha=args.workflow_sha", resolver)
        apply_step = apply.split(
            "      - name: Revalidate and apply fixed thread mutations\n", 1
        )[1].split("\n      - name: Normalize missing non-gating receipt\n", 1)[0]
        self.assertIn("GH_TOKEN: ${{ github.token }}", apply_step)
        self.assertIn("DANCER_GITHUB_TOKEN:", apply_step)
        self.assertNotIn("minimizeComment", resolver)

    def test_sealed_resolution_helpers_include_importable_dependencies(self):
        prepare = self.job("resolution-prepare", "resolution-review")
        trusted_sources = (
            RESOLUTION,
            RESOLVER,
            PUBLICATION,
            PUBLISHER,
            CONTRACT,
            INTENT,
            SOURCE,
            BASE_PROVENANCE,
        )
        for source in trusted_sources:
            self.assertIn(
                f'cp trusted-workflow/src/{source.name} "${{RESOLUTION_INPUT}}/trusted/"',
                prepare,
            )

        with tempfile.TemporaryDirectory() as directory:
            trusted = pathlib.Path(directory) / "trusted"
            trusted.mkdir()
            for source in trusted_sources:
                shutil.copy2(source, trusted / source.name)
            for helper in ("review_resolution.py", "review_resolver.py"):
                result = subprocess.run(
                    [sys.executable, str(trusted / helper), "--help"],
                    cwd=trusted,
                    capture_output=True,
                    check=False,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_docs_keep_base_controlled_caller_and_no_incremental_state(self):
        docs = README.read_text() + ARCHITECTURE.read_text()
        self.assertIn("pull_request_target:", docs)
        self.assertIn("branches: [master]", docs)
        self.assertIn(
            "types: [opened, reopened, synchronize, ready_for_review, edited]", docs
        )
        for stale in ("prior-review.json", "LAST_REVIEWED_SHA", "minimizeComment"):
            self.assertNotIn(stale, self.workflow)
        self.assertIn("Revalidate and publish Dancer COMMENT review", self.workflow)


if __name__ == "__main__":
    unittest.main()
