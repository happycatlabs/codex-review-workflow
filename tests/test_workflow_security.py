from __future__ import annotations

import os
import pathlib
import re
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/codex-code-review.yml"
README = ROOT / "README.md"
ARCHITECTURE = ROOT / "codex-code-review.md"
CONTRACT = ROOT / "src/review_contract.py"
SOURCE = ROOT / "src/source_context.py"
INTENT = ROOT / "src/intent_context.py"
CODEX_ACTION_SHA = "52fe01ec70a42f454c9d2ebd47598f9fd6893d56"
EXPECTED_WORKFLOW_PATH = (
    "happycatlabs/codex-review-workflow/.github/workflows/codex-code-review.yml"
)


class WorkflowSecurityTests(unittest.TestCase):
    def setUp(self):
        self.workflow = WORKFLOW.read_text()

    def job(self, name: str, next_name: str | None = None) -> str:
        block = self.workflow.split(f"  {name}:\n", 1)[1]
        return block.split(f"\n  {next_name}:\n", 1)[0] if next_name else block

    def test_five_bounded_jobs_and_ephemeral_runners(self):
        jobs = re.findall(r"^  ([a-z][a-z0-9-]+):$", self.workflow, re.MULTILINE)
        self.assertEqual(
            jobs, ["trust-guard", "prepare", "intent", "review", "publish"]
        )
        runs_on = re.findall(r"^    runs-on: (.+)$", self.workflow, re.MULTILINE)
        self.assertEqual(runs_on, ["ubuntu-24.04"] * 5)
        self.assertNotIn("inputs.runner", self.workflow)
        self.assertNotIn("SENTRY", self.workflow.upper())

    def test_default_branch_guard_precedes_all_sensitive_jobs(self):
        guard = self.job("trust-guard", "prepare")
        for text in (
            'if [ "${EVENT_NAME}" != pull_request_target ]',
            'gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}"',
            'if [ "${base_ref}" != "${default_branch}" ]',
            'if [ "${base_sha}" != "${default_branch_sha}" ]',
        ):
            self.assertIn(text, guard)
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
            ("prepare", "intent"),
            ("intent", "review"),
            ("publish", None),
        ):
            block = self.job(name, next_name)
            self.assertIn("repository: ${{ job.workflow_repository }}", block)
            self.assertIn("ref: ${{ job.workflow_sha }}", block)
            self.assertIn("persist-credentials: false", block)
            self.assertIn("sparse-checkout: src", block)
            self.assertIn("trusted-workflow/src/review_contract.py", block)
        self.assertNotIn("# BEGIN REVIEW_CONTRACT", self.workflow)
        self.assertNotIn("contract/review_contract.py", self.workflow)

    def test_candidate_checkout_and_credentials_never_share_execution_authority(self):
        prepare = self.job("prepare", "intent")
        intent = self.job("intent", "review")
        review = self.job("review", "publish")
        publish = self.job("publish")
        self.assertIn("Checkout exact pull request head as data", prepare)
        self.assertNotIn("secrets.", prepare)
        self.assertNotIn("repo-checkout", intent)
        self.assertNotIn("actions/checkout", review)
        self.assertNotIn("LINEAR_CLIENT", review)
        self.assertNotIn("LINEAR_CLIENT", publish)
        self.assertIn("LINEAR_CLIENT_ID: ${{ secrets.LINEAR_CLIENT_ID }}", intent)
        self.assertIn(
            "LINEAR_CLIENT_SECRET: ${{ secrets.LINEAR_CLIENT_SECRET }}", intent
        )
        self.assertIn("secrets.OPENAI_API_KEY", review)
        self.assertNotIn("OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}", review)

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
        self.assertIn('"${MODEL_WORKSPACE}/codex-prompt.md"', intent)
        self.assertIn("working-directory: codex-review-input/model-workspace", review)
        self.assertIn("permissions: {}", review)
        self.assertIn(f"uses: openai/codex-action@{CODEX_ACTION_SHA}", review)
        self.assertIn("permission-profile: ':read-only'", review)
        self.assertIn("safety-strategy: drop-sudo", review)
        self.assertIn(
            "codex-args: '[\"--ephemeral\",\"--disable\",\"shell_tool\","
            "\"--disable\",\"unified_exec\"]'",
            review,
        )
        for forbidden in ("actions/checkout", "run: bun", "run: npm", "repo-checkout"):
            self.assertNotIn(forbidden, review)

    def test_source_and_prompt_contracts_are_bounded_and_fail_closed(self):
        contract = CONTRACT.read_text()
        source = SOURCE.read_text()
        for text in (
            "MAX_PROMPT_BYTES = 2_000_000",
            "UNTRUSTED_MARKER_COLLISION",
            "INPUT_TRUNCATED",
            "SOURCE_CONTEXT_TRUNCATED",
            "TICKET_CONTEXT_STALE",
        ):
            self.assertIn(text, contract)
        for text in (
            "MAX_CONTEXT_BYTES = 500_000",
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
        self.assertIn("--lookup-context", self.workflow)

    def test_publisher_refetches_identity_and_proves_workflow_revision(self):
        publish = self.job("publish")
        for text in (
            'gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}"',
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

    def test_model_comment_uses_literal_safe_gh_field(self):
        publish = self.job("publish")
        step = publish.split("      - name: Publish fresh review comment\n", 1)[1]
        step = step.split("\n      - name: Write run summary\n", 1)[0]
        self.assertIn(
            '--raw-field body="$(cat codex-review-result/comment-body.md)"', step
        )
        shell = step.split("        run: |\n", 1)[1]
        shell = "\n".join(
            line[10:] if line.startswith("          ") else line
            for line in shell.splitlines()
        )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "codex-review-result").mkdir()
            (root / "codex-review-result/comment-body.md").write_text(
                "@/proc/self/environ"
            )
            (root / "bin").mkdir()
            fake_gh = root / "bin/gh"
            fake_gh.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$GH_CAPTURE"\n')
            fake_gh.chmod(0o755)
            capture = root / "args"
            subprocess.run(
                ["bash", "-c", shell],
                cwd=root,
                env={
                    **os.environ,
                    "PATH": f"{root / 'bin'}:{os.environ['PATH']}",
                    "GH_CAPTURE": str(capture),
                    "GH_TOKEN": "fixture",
                    "REPOSITORY": "example/repo",
                    "PR_NUMBER": "17",
                },
                check=True,
            )
            arguments = capture.read_text().splitlines()
            self.assertIn("--raw-field", arguments)
            self.assertNotIn("--field", arguments)
            self.assertIn("body=@/proc/self/environ", arguments)

    def test_docs_keep_base_controlled_caller_and_no_incremental_state(self):
        docs = README.read_text() + ARCHITECTURE.read_text()
        self.assertIn("pull_request_target:", docs)
        self.assertIn("branches: [master]", docs)
        self.assertIn(
            "types: [opened, reopened, synchronize, ready_for_review, edited]", docs
        )
        for stale in ("prior-review.json", "LAST_REVIEWED_SHA", "minimizeComment"):
            self.assertNotIn(stale, self.workflow)
        self.assertIn("Publish fresh review comment", self.workflow)


if __name__ == "__main__":
    unittest.main()
