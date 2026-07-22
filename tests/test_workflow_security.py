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
CODEX_ACTION_SHA = "52fe01ec70a42f454c9d2ebd47598f9fd6893d56"
EXPECTED_WORKFLOW_PATH = (
    "happycatlabs/codex-review-workflow/.github/workflows/codex-code-review.yml"
)


class WorkflowSecurityTests(unittest.TestCase):
    def setUp(self):
        self.workflow = WORKFLOW.read_text()

    def job(self, name: str, next_name: str | None = None) -> str:
        block = self.workflow.split(f"  {name}:\n", 1)[1]
        if next_name:
            block = block.split(f"\n  {next_name}:\n", 1)[0]
        return block

    def test_only_four_bounded_jobs_remain(self):
        jobs = re.findall(r"^  ([a-z][a-z0-9-]+):$", self.workflow, re.MULTILINE)
        self.assertEqual(jobs, ["trust-guard", "prepare", "review", "publish"])
        self.assertNotIn("SENTRY", self.workflow.upper())
        self.assertNotIn("sentry-context", self.workflow)

    def test_pull_request_target_and_default_branch_guard_precede_secret_job(self):
        guard = self.job("trust-guard", "prepare")
        self.assertIn('if [ "${EVENT_NAME}" != pull_request_target ]', guard)
        self.assertIn('gh api "repos/${REPOSITORY}"', guard)
        self.assertIn('gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}"', guard)
        self.assertIn('commits/${encoded_default_branch}', guard)
        self.assertIn('if [ "${base_ref}" != "${default_branch}" ]', guard)
        self.assertIn('if [ "${base_sha}" != "${default_branch_sha}" ]', guard)
        self.assertIn('EVENT_BASE_REF: ${{ github.event.pull_request.base.ref }}', guard)
        self.assertIn('default_branch_sha=${default_branch_sha}', guard)
        self.assertNotIn("secrets.", guard)

        prepare = self.job("prepare", "review")
        review = self.job("review", "publish")
        publish = self.job("publish")
        self.assertIn("needs: trust-guard", prepare)
        self.assertIn("needs.trust-guard.result == 'success'", prepare)
        self.assertIn("needs: [trust-guard, prepare]", review)
        self.assertIn("needs.trust-guard.result == 'success'", review)
        self.assertIn("needs: [trust-guard, prepare, review]", publish)
        self.assertIn("needs.trust-guard.result == 'success'", publish)

    def test_only_review_job_receives_model_credentials(self):
        prepare = self.job("prepare", "review")
        review = self.job("review", "publish")
        publish = self.job("publish")
        self.assertNotIn("secrets.", prepare)
        self.assertNotIn("secrets.", publish)
        self.assertIn("secrets.OPENAI_API_KEY", review)
        self.assertIn("secrets.CODEX_AUTH_JSON", review)
        self.assertIn("HAS_OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY != '' }}", review)
        self.assertIn("HAS_CODEX_AUTH_JSON: ${{ secrets.CODEX_AUTH_JSON != '' }}", review)
        self.assertNotIn("OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}", review)
        self.assertNotIn("CODEX_AUTH_JSON: ${{ secrets.CODEX_AUTH_JSON }}", review)
        self.assertIn("AUTH_LEGACY_UNSAFE", review)

    def test_runner_is_fixed_to_ephemeral_github_linux(self):
        runs_on = re.findall(r"^    runs-on: (.+)$", self.workflow, flags=re.MULTILINE)
        self.assertEqual(len(runs_on), 4)
        self.assertEqual(set(runs_on), {"ubuntu-24.04"})
        self.assertNotIn("inputs.runner", self.workflow)
        docs = README.read_text() + ARCHITECTURE.read_text()
        self.assertIn("ephemeral GitHub-hosted Linux", docs)
        self.assertIn("persistent self-hosted", " ".join(docs.lower().split()))

    def test_codex_action_is_pinned_and_uses_scoped_proxy_contract(self):
        self.assertIn(f"uses: openai/codex-action@{CODEX_ACTION_SHA}", self.workflow)
        self.assertIn("openai-api-key: ${{ secrets.OPENAI_API_KEY }}", self.workflow)
        self.assertIn("safety-strategy: drop-sudo", self.workflow)
        self.assertIn("permission-profile: ':read-only'", self.workflow)
        job_env_blocks = re.findall(
            r"^    env:\n(?P<body>(?:^      .+\n)+)", self.workflow, flags=re.MULTILINE
        )
        self.assertTrue(job_env_blocks)
        self.assertTrue(all("OPENAI_API_KEY" not in block for block in job_env_blocks))

    def test_review_has_no_source_checkout_or_execution_tools(self):
        review = self.job("review", "publish")
        self.assertNotIn("actions/checkout", review)
        self.assertIn("working-directory: codex-review-input/model-workspace", review)
        self.assertIn(
            "codex-args: '[\"--ephemeral\",\"--disable\",\"shell_tool\","
            "\"--disable\",\"unified_exec\"]'",
            review,
        )
        self.assertNotIn("run: bun", review)
        self.assertNotIn("run: npm", review)

    def test_model_filesystem_contains_only_generated_prompt_and_schema(self):
        prepare = self.job("prepare", "review")
        paths = {
            name: pathlib.PurePosixPath(
                re.search(rf"^      {name}: (.+)$", prepare, re.MULTILINE).group(1)
            )
            for name in (
                "CHECKOUT_DIR",
                "MODEL_WORKSPACE",
                "STAGING_DIR",
                "TRUSTED_CONTEXT",
            )
        }
        model_workspace = paths["MODEL_WORKSPACE"]
        for name in ("CHECKOUT_DIR", "STAGING_DIR", "TRUSTED_CONTEXT"):
            self.assertNotEqual(paths[name], model_workspace)
            self.assertNotIn(model_workspace, paths[name].parents)

        self.assertEqual(
            model_workspace, pathlib.PurePosixPath("codex-review-input/model-workspace")
        )
        self.assertIn('cat <<\'JSON\' > "${MODEL_WORKSPACE}/codex-output-schema.json"', prepare)
        self.assertIn('--output "${MODEL_WORKSPACE}/codex-prompt.md"', prepare)
        self.assertIn("Assert model filesystem contains generated files only", prepare)
        self.assertIn('find "${MODEL_WORKSPACE}" -type f -print | sort', prepare)
        self.assertIn('"${MODEL_WORKSPACE}/codex-output-schema.json"', prepare)
        self.assertIn('"${MODEL_WORKSPACE}/codex-prompt.md"', prepare)
        upload = prepare.split("- name: Upload bounded review input", 1)[1]
        self.assertIn("path: ${{ env.REVIEW_BUNDLE }}", upload)
        self.assertNotIn("path: ${{ env.CHECKOUT_DIR }}", upload)
        self.assertNotIn("REVIEW_WORKSPACE", self.workflow)
        self.assertNotIn("sanitize_workspace", self.workflow)
        self.assertNotIn("sanitize-workspace", self.workflow)

    def test_pr_title_body_and_auto_discovered_config_never_enter_model_packet(self):
        for forbidden in (
            "github.event.pull_request.title",
            "github.event.pull_request.body",
            "PR_TITLE",
            "PR_BODY",
            "AGENTS.override.md",
            ".claude/settings",
        ):
            self.assertNotIn(forbidden, self.workflow)
        self.assertIn("# Trusted default-branch guidance", self.workflow)
        self.assertIn("# Untrusted pull request data", self.workflow)
        self.assertIn("git\", \"-C\", str(checkout), \"show\", f\"{default_sha}:{rel_path}\"", self.workflow)
        self.assertIn('if not copy_default_file("REVIEW.md")', self.workflow)

    def test_diff_is_exact_bounded_utf8_and_non_binary(self):
        prepare = self.job("prepare", "review")
        self.assertIn('"--function-context", "--unified=20"', prepare)
        self.assertIn('"--no-ext-diff", "--no-textconv"', prepare)
        self.assertIn('["diff", "--numstat"', prepare)
        self.assertIn('columns[0] == b"-" and columns[1] == b"-"', prepare)
        self.assertIn("status_path.read_bytes()", prepare)
        self.assertIn("diff_path.read_bytes()", prepare)
        self.assertIn('.decode("utf-8", errors="strict")', prepare)
        self.assertIn("MAX_PROMPT_BYTES = 2_000_000", prepare)
        self.assertIn("INPUT_TRUNCATED", self.workflow)
        self.assertIn('"diff_encoding": "utf-8"', self.workflow)
        self.assertIn('"binary_files": False', self.workflow)

    def test_prepare_rejects_non_ancestor_base_before_diff_generation(self):
        prepare = self.job("prepare", "review")
        checkout = prepare.index("Checkout exact pull request head as data")
        ancestry = prepare.index("Require reviewed base to be an ancestor of head")
        diff_generation = prepare.index("Build trusted guidance and untrusted diff data")

        self.assertLess(checkout, ancestry)
        self.assertLess(ancestry, diff_generation)
        self.assertIn('"merge-base",', prepare)
        self.assertIn('"--is-ancestor",', prepare)
        self.assertIn("BASE_NOT_ANCESTOR", prepare)
        self.assertIn(
            '--error-output "${REVIEW_BUNDLE}/review-execution.json"', prepare
        )

    def test_untrusted_marker_sequences_fail_before_model_input_is_written(self):
        prepare = self.job("prepare", "review")
        self.assertIn("class UntrustedMarkerCollisionError", prepare)
        self.assertIn('for marker in ("<<<BEGIN", "<<<END")', prepare)
        self.assertIn("reject_untrusted_marker_collisions(status, diff)", prepare)
        self.assertIn("UNTRUSTED_MARKER_COLLISION", prepare)
        self.assertEqual(
            prepare.count(
                '--error-output "${REVIEW_BUNDLE}/review-execution.json"'
            ),
            2,
        )
        self.assertNotIn("escape_untrusted_markers", self.workflow)
        self.assertNotIn("untrusted_marker_escape_count", self.workflow)
        self.assertNotIn('.replace("<<<BEGIN"', self.workflow)

    def test_packet_activation_and_review_input_share_prepare_code_path(self):
        prepare = self.job("prepare", "review")
        self.assertIn("select_packets(", prepare)
        self.assertIn("--changed-files \"${STAGING_DIR}/changed-files.txt\"", prepare)
        self.assertIn("--activated-packets \"${METADATA_DIR}/activated-packets.json\"", prepare)
        self.assertIn('"review_scope": "diff_v1"', prepare)
        self.assertIn('"base_sha": base_sha', prepare)
        self.assertIn('"state": os.environ["REVIEW_STATE"]', prepare)

    def test_v1_has_no_incremental_or_sticky_state(self):
        for stale_contract in (
            "[PRIOR]",
            "prior-review.json",
            "incremental-mode.txt",
            "codex-review-state:",
            "minimizeComment",
            "LAST_REVIEWED_SHA",
            "sticky-comment-node-id",
        ):
            self.assertNotIn(stale_contract, self.workflow)
        self.assertIn("Publish fresh review comment", self.workflow)

    def test_publisher_refetches_complete_pr_identity_and_default_branch(self):
        publish = self.job("publish")
        self.assertIn('gh api "repos/${REPOSITORY}"', publish)
        self.assertIn('gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}"', publish)
        for field in (
            "state",
            "head_sha",
            "base_ref",
            "base_sha",
            "default_branch",
            "default_branch_sha",
        ):
            self.assertIn(field, publish)
        for code in (
            "PR_STATE_INVALID",
            "BASE_BRANCH_INVALID",
            "BASE_REF_DRIFT",
            "STALE_HEAD",
            "STALE_BASE",
        ):
            self.assertIn(code, publish)

    def test_publisher_resolves_actual_reusable_workflow_provenance(self):
        publish = self.job("publish")
        self.assertIn("actions: read", publish)
        self.assertIn('actions/runs/${GITHUB_RUN_ID}', publish)
        self.assertIn('run.get("referenced_workflows", [])', publish)
        self.assertIn(EXPECTED_WORKFLOW_PATH, publish)
        self.assertIn('matches[0].get("sha", "")', publish)
        self.assertIn('path != f"{expected}@{actual_sha}"', publish)
        self.assertIn("WORKFLOW_PROVENANCE_MISSING", publish)
        self.assertNotIn("CLAIMED_WORKFLOW_REVISION", publish)
        self.assertNotIn("inputs.workflow-revision", publish)
        self.assertIn("--provenance codex-review-result/workflow-provenance.json", publish)

    def test_model_comment_uses_literal_safe_gh_field(self):
        publish = self.job("publish")
        comment_step = publish.split(
            "      - name: Publish fresh review comment\n", 1
        )[1].split("\n      - name: Write run summary\n", 1)[0]
        self.assertIn(
            '--raw-field body="$(cat codex-review-result/comment-body.md)"',
            comment_step,
        )
        self.assertNotIn("--field", comment_step)

        shell = comment_step.split("        run: |\n", 1)[1]
        shell = "\n".join(
            line[10:] if line.startswith("          ") else line
            for line in shell.splitlines()
        )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            result_dir = root / "codex-review-result"
            result_dir.mkdir()
            (result_dir / "comment-body.md").write_text("@/proc/self/environ")
            binary_dir = root / "bin"
            binary_dir.mkdir()
            fake_gh = binary_dir / "gh"
            fake_gh.write_text(
                '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$GH_CAPTURE"\n'
            )
            fake_gh.chmod(0o755)
            capture = root / "gh-args.txt"
            environment = {
                **os.environ,
                "PATH": f"{binary_dir}:{os.environ['PATH']}",
                "GH_CAPTURE": str(capture),
                "GH_TOKEN": "fixture-token",
                "REPOSITORY": "example/repository",
                "PR_NUMBER": "17",
            }

            subprocess.run(
                ["bash", "-c", shell],
                cwd=root,
                env=environment,
                check=True,
            )

            arguments = capture.read_text().splitlines()
            self.assertIn("--raw-field", arguments)
            self.assertNotIn("--field", arguments)
            self.assertIn("body=@/proc/self/environ", arguments)

    def test_machine_artifact_is_bound_and_non_clean_fails_job(self):
        publish = self.job("publish")
        for field in (
            '"head_sha"',
            '"base_ref"',
            '"base_sha"',
            '"state"',
            '"review_scope"',
            '"coverage"',
        ):
            self.assertIn(field, self.workflow)
        self.assertIn("RESULT_ARTIFACT: codex-review-result", publish)
        self.assertIn("name: ${{ env.RESULT_ARTIFACT }}", publish)
        self.assertIn('if [ "${verdict}" != clean ]', publish)

    def test_docs_show_exact_base_controlled_caller_trigger(self):
        docs = README.read_text() + ARCHITECTURE.read_text()
        self.assertIn("pull_request_target:", docs)
        self.assertIn("branches: [master]", docs)
        self.assertIn(
            "types: [opened, reopened, synchronize, ready_for_review, edited]", docs
        )
        self.assertIn("diff_v1", docs)
        self.assertIn("FABLE-188", docs)
        self.assertIn("base.sha/ancestor", docs)


if __name__ == "__main__":
    unittest.main()
