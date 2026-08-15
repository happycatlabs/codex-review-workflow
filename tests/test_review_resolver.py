from __future__ import annotations

import copy
import io
import json
import pathlib
import sys
import unittest
import zipfile
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import review_publisher  # noqa: E402
import review_resolution  # noqa: E402
import review_resolver  # noqa: E402
from tests.test_review_resolution import (  # noqa: E402
    BASE_SHA,
    HEAD_SHA,
    PULL_NUMBER,
    REPOSITORY,
    RUN_ID,
    finding,
    publication_pair,
)


PRIOR_HEAD = "c" * 40
PRIOR_RUN_ID = 41
PRIOR_REVIEW_ID = 800
THREAD_ID = "PRRT_kwDOThread"
COMMENT_NODE_ID = "PRRC_kwDOComment"
COMMENT_ID = 80001


def graphql_thread(
    *, resolved: bool = False, outdated: bool = False, replies: int = 1
) -> dict:
    nodes = [
        {
            "id": COMMENT_NODE_ID,
            "fullDatabaseId": str(COMMENT_ID),
            "author": {"login": review_resolver.GRAPHQL_DANCER_LOGIN},
            "replyTo": None,
        }
    ]
    if replies > 1:
        nodes.append(
            {
                "id": "human-reply",
                "fullDatabaseId": "80002",
                "author": {"login": "human"},
                "replyTo": {"id": COMMENT_NODE_ID},
            }
        )
    return {
        "id": THREAD_ID,
        "isResolved": resolved,
        "isOutdated": outdated,
        "path": "lib/example.ts",
        "line": 7,
        "startLine": None,
        "originalLine": 7,
        "originalStartLine": None,
        "diffSide": "RIGHT",
        "startDiffSide": None,
        "subjectType": "LINE",
        "viewerCanResolve": True,
        "resolvedBy": None,
        "comments": {
            "totalCount": replies,
            "pageInfo": {"hasNextPage": False},
            "nodes": nodes,
        },
    }


class FakeGitHub:
    def __init__(self, current_pair, prior_pair):
        self.current_result, self.current_receipt, self.current_request = current_pair
        self.prior_result, self.prior_receipt, self.prior_request = prior_pair
        self.thread = graphql_thread()
        self.mutation_calls = 0
        self.mutation_raises = False
        self.resolve_on_error = False
        self.mutation_error_status = 0
        self.head_sha = HEAD_SHA
        self.drift_after_resolved_readback = False
        self.current_review = self._review(
            self.current_receipt, self.current_request
        )
        self.prior_review = self._review(self.prior_receipt, self.prior_request)
        expected = self.prior_request["comments"][0]
        self.comment = {
            "id": COMMENT_ID,
            "node_id": COMMENT_NODE_ID,
            "pull_request_review_id": PRIOR_REVIEW_ID,
            "in_reply_to_id": None,
            "user": {
                "login": review_publisher.EXPECTED_DANCER_LOGIN,
                "id": review_publisher.EXPECTED_DANCER_ACTOR_ID,
            },
            "body": expected["body"],
            "path": expected["path"],
            "line": expected["line"],
            "side": expected["side"],
            "original_line": expected["line"],
            "original_side": expected["side"],
            "start_line": expected.get("start_line"),
            "start_side": expected.get("start_side"),
            "original_start_line": expected.get("start_line"),
            "commit_id": PRIOR_HEAD,
            "original_commit_id": PRIOR_HEAD,
        }

    @staticmethod
    def _review(receipt, request):
        prepared, _ = review_publisher.marked_request(request)
        review_id = receipt["review"]["id"]
        return {
            "id": review_id,
            "state": "COMMENTED",
            "body": prepared["body"],
            "commit_id": request["commit_id"],
            "html_url": receipt["review"]["url"],
            "user": {
                "login": review_publisher.EXPECTED_DANCER_LOGIN,
                "id": review_publisher.EXPECTED_DANCER_ACTOR_ID,
            },
        }

    def request(self, method: str, path: str, payload=None):
        if method == "POST" and path == "/graphql":
            return {
                "data": {
                    "viewer": {
                        "login": review_publisher.EXPECTED_DANCER_LOGIN,
                        "databaseId": review_publisher.EXPECTED_DANCER_ACTOR_ID,
                    }
                }
            }
        if method == "GET" and path == "/repos/happycatlabs/fable":
            return {
                "id": 979193317,
                "full_name": REPOSITORY,
                "default_branch": "master",
            }
        if method == "GET" and path == f"/repos/happycatlabs/fable/pulls/{PULL_NUMBER}":
            return {
                "number": PULL_NUMBER,
                "state": "open",
                "merged_at": None,
                "user": {
                    "login": review_publisher.EXPECTED_DANCER_LOGIN,
                    "id": review_publisher.EXPECTED_DANCER_ACTOR_ID,
                },
                "head": {
                    "ref": "codex/test-head",
                    "sha": self.head_sha,
                    "repo": {"id": 979193317, "full_name": REPOSITORY},
                },
                "base": {
                    "ref": "master",
                    "sha": BASE_SHA,
                    "repo": {"id": 979193317, "full_name": REPOSITORY},
                },
                "stack": None,
            }
        if method == "GET" and path == "/repos/happycatlabs/fable/commits/master":
            return {"sha": BASE_SHA}
        if method == "GET" and path.endswith(f"/pulls/comments/{COMMENT_ID}"):
            return copy.deepcopy(self.comment)
        if method == "GET" and path.endswith(f"/reviews/{PRIOR_REVIEW_ID}"):
            return copy.deepcopy(self.prior_review)
        if method == "GET" and path.endswith(
            f"/reviews/{self.current_receipt['review']['id']}"
        ):
            return copy.deepcopy(self.current_review)
        if method == "GET" and "/comments?" in path:
            if f"/reviews/{PRIOR_REVIEW_ID}/comments" in path:
                page = int(path.rsplit("page=", 1)[1])
                return (
                    [
                        {
                            "id": COMMENT_ID,
                            "pull_request_review_id": PRIOR_REVIEW_ID,
                        }
                    ]
                    if page == 1
                    else []
                )
            return []
        if method == "GET" and "/contents/lib/example.ts?ref=" in path:
            import base64

            raw = b"export const fallback = () => true;\n"
            return {
                "type": "file",
                "path": "lib/example.ts",
                "encoding": "base64",
                "size": len(raw),
                "content": base64.b64encode(raw).decode(),
            }
        if method == "GET" and "/actions/artifacts" in path:
            return {"artifacts": []}
        raise AssertionError((method, path, payload))

    def graphql(self, query: str, variables: dict):
        if query == review_resolver.THREAD_QUERY:
            return {
                "repository": {
                    "pullRequest": {
                        "number": PULL_NUMBER,
                        "state": "OPEN",
                        "headRefOid": HEAD_SHA,
                        "baseRefName": "master",
                        "baseRefOid": BASE_SHA,
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [copy.deepcopy(self.thread)],
                        },
                    }
                }
            }
        if query == review_resolver.THREAD_READBACK_QUERY:
            node = copy.deepcopy(self.thread)
            if self.thread["isResolved"] and self.drift_after_resolved_readback:
                self.head_sha = "f" * 40
            return {"node": node}
        if query == review_resolver.RESOLVE_MUTATION:
            self.mutation_calls += 1
            if self.mutation_raises:
                if self.resolve_on_error:
                    self.thread["isResolved"] = True
                    self.thread["resolvedBy"] = {
                        "login": "informational-user",
                    }
                raise review_resolver.GitHubApiError(
                    self.mutation_error_status, "response lost"
                )
            self.thread["isResolved"] = True
            self.thread["resolvedBy"] = {
                "login": "informational-user",
            }
            return {
                "resolveReviewThread": {
                    "clientMutationId": variables["clientMutationId"],
                    "thread": {
                        "id": THREAD_ID,
                        "isResolved": True,
                        "resolvedBy": copy.deepcopy(self.thread["resolvedBy"]),
                    },
                }
            }
        raise AssertionError((query, variables))


def prepared_state():
    current = publication_pair([], head=HEAD_SHA, run_id=RUN_ID, review_id=900)
    prior = publication_pair(
        [finding()],
        head=PRIOR_HEAD,
        run_id=PRIOR_RUN_ID,
        review_id=PRIOR_REVIEW_ID,
    )
    fake = FakeGitHub(current, prior)
    with (
        patch.object(review_resolver, "GitHubClient", return_value=fake),
        patch.object(
            review_resolver,
            "collect_provenance",
            return_value=[
                review_resolution.validate_inline_provenance(
                    prior[0],
                    prior[1],
                    repository=REPOSITORY,
                    pull_number=PULL_NUMBER,
                )
            ],
        ),
    ):
        packet = review_resolver.prepare(
            repository=REPOSITORY,
            pull_number=PULL_NUMBER,
            run_id=RUN_ID,
            workflow_sha="e" * 40,
            current_result=current[0],
            current_receipt=current[1],
            token="read-token",
        )
    plan = review_resolution.build_plan(
        packet,
        {
            "current_head_sha": HEAD_SHA,
            "decisions": [
                {
                    "thread_id": THREAD_ID,
                    "prior_fingerprint": packet["candidates"][0]["provenance"]["fingerprint"],
                    "decision": "RESOLVE_ADDRESSED",
                    "reason": "The current exact-head function returns the correct fallback.",
                }
            ]
        },
    )
    return current, prior, fake, packet, plan


class ResolutionPrepareTests(unittest.TestCase):
    def test_prepares_exact_single_root_with_prior_receipt(self):
        _, _, _, packet, _ = prepared_state()

        self.assertEqual(packet["status"], "ready")
        self.assertEqual(packet["candidate_count"], 1)
        self.assertEqual(packet["model_candidate_count"], 1)
        self.assertEqual(packet["candidates"][0]["thread_id"], THREAD_ID)

    def test_human_reply_is_untouched(self):
        current = publication_pair([], head=HEAD_SHA, run_id=RUN_ID, review_id=900)
        prior = publication_pair(
            [finding()], head=PRIOR_HEAD, run_id=PRIOR_RUN_ID, review_id=PRIOR_REVIEW_ID
        )
        fake = FakeGitHub(current, prior)
        fake.thread = graphql_thread(replies=2)

        with (
            patch.object(review_resolver, "GitHubClient", return_value=fake),
            patch.object(review_resolver, "collect_provenance", return_value=[]),
        ):
            packet = review_resolver.prepare(
                repository=REPOSITORY,
                pull_number=PULL_NUMBER,
                run_id=RUN_ID,
                workflow_sha="e" * 40,
                current_result=current[0],
                current_receipt=current[1],
                token="read-token",
            )

        self.assertEqual(packet["status"], "no_candidates")
        self.assertEqual(packet["candidates"], [])
        self.assertEqual(
            packet["observations"],
            [
                {
                    "thread_id": THREAD_ID,
                    "reason": "thread_has_replies",
                    "is_resolved": False,
                }
            ],
        )
        plan = review_resolution.build_plan(packet, None)
        with patch.object(review_resolver, "GitHubClient", return_value=fake):
            receipt = review_resolver.apply(
                repository=REPOSITORY,
                pull_number=PULL_NUMBER,
                run_id=RUN_ID,
                current_result=current[0],
                current_receipt=current[1],
                packet=packet,
                plan=plan,
                read_token="read-token",
                dancer_token="",
            )
        self.assertEqual(fake.mutation_calls, 0)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["resolve_count"], 0)
        self.assertIsNone(receipt["actor"])
        self.assertEqual(receipt["observations"][0]["is_resolved"], False)

    def test_missing_prior_artifact_is_untouched(self):
        current = publication_pair([], head=HEAD_SHA, run_id=RUN_ID, review_id=900)
        prior = publication_pair(
            [finding()], head=PRIOR_HEAD, run_id=PRIOR_RUN_ID, review_id=PRIOR_REVIEW_ID
        )
        fake = FakeGitHub(current, prior)

        with (
            patch.object(review_resolver, "GitHubClient", return_value=fake),
            patch.object(review_resolver, "collect_provenance", return_value=[]),
        ):
            packet = review_resolver.prepare(
                repository=REPOSITORY,
                pull_number=PULL_NUMBER,
                run_id=RUN_ID,
                workflow_sha="e" * 40,
                current_result=current[0],
                current_receipt=current[1],
                token="read-token",
            )

        self.assertEqual(packet["status"], "no_candidates")
        self.assertEqual(
            packet["excluded"], {"provenance_missing_or_ambiguous": 1}
        )

    def test_outdated_thread_is_not_automatically_resolved(self):
        current = publication_pair([], head=HEAD_SHA, run_id=RUN_ID, review_id=900)
        prior = publication_pair(
            [finding()], head=PRIOR_HEAD, run_id=PRIOR_RUN_ID, review_id=PRIOR_REVIEW_ID
        )
        fake = FakeGitHub(current, prior)
        fake.thread = graphql_thread(outdated=True)

        with (
            patch.object(review_resolver, "GitHubClient", return_value=fake),
            patch.object(
                review_resolver,
                "collect_provenance",
                return_value=[
                    review_resolution.validate_inline_provenance(
                        prior[0],
                        prior[1],
                        repository=REPOSITORY,
                        pull_number=PULL_NUMBER,
                    )
                ],
            ),
        ):
            packet = review_resolver.prepare(
                repository=REPOSITORY,
                pull_number=PULL_NUMBER,
                run_id=RUN_ID,
                workflow_sha="e" * 40,
                current_result=current[0],
                current_receipt=current[1],
                token="read-token",
            )

        self.assertEqual(packet["status"], "ready")
        self.assertTrue(packet["candidates"][0]["thread_snapshot"]["is_outdated"])
        self.assertIsNone(packet["candidates"][0]["deterministic_decision"])

    def test_outdated_thread_uses_exact_original_modern_coordinates(self):
        thread = graphql_thread(outdated=True)
        thread["line"] = None
        thread["startLine"] = None

        normalized = review_resolver.normalize_thread(thread)

        self.assertIsNotNone(normalized)
        self.assertEqual(normalized["line"], 7)
        self.assertTrue(normalized["is_outdated"])

    def test_live_outdated_thread_shape_omits_unsupported_original_diff_side(self):
        thread = graphql_thread(outdated=True)
        thread.update(
            {
                "id": "PRRT_kwDOOl1N5c6Zb1nU",
                "path": "lib/autonomy/fable-pr-disposition.ts",
                "line": None,
                "startLine": None,
                "originalLine": 448,
                "originalStartLine": None,
            }
        )
        thread["comments"]["nodes"][0].update(
            {
                "id": "PRRC_kwDOOl1N5c7hwrKg",
                "fullDatabaseId": "3787633312",
            }
        )
        rest_comment = {
            "id": 3787633312,
            "node_id": "PRRC_kwDOOl1N5c7hwrKg",
            "pull_request_review_id": 4941742767,
            "in_reply_to_id": None,
            "user": {
                "login": review_publisher.EXPECTED_DANCER_LOGIN,
                "id": review_publisher.EXPECTED_DANCER_ACTOR_ID,
            },
            "body": "trusted finding body",
            "path": "lib/autonomy/fable-pr-disposition.ts",
            "line": None,
            "side": "RIGHT",
            "original_line": 448,
            "original_side": None,
            "start_line": None,
            "start_side": None,
            "original_start_line": None,
            "commit_id": "30a18b432d9234e200019b6713efc951a0d785d4",
            "original_commit_id": "30a18b432d9234e200019b6713efc951a0d785d4",
        }

        normalized = review_resolver.normalize_thread(thread)
        rest = review_resolver._modern_comment_snapshot(rest_comment)

        self.assertNotIn("originalDiffSide", review_resolver.THREAD_QUERY)
        self.assertNotIn("originalDiffSide", review_resolver.THREAD_READBACK_QUERY)
        self.assertIsNotNone(normalized)
        self.assertIsNotNone(rest)
        self.assertEqual(normalized["line"], 448)
        self.assertEqual(normalized["side"], "RIGHT")
        self.assertEqual(rest["line"], normalized["line"])
        self.assertEqual(rest["side"], normalized["side"])
        self.assertEqual(rest["path"], normalized["path"])
        self.assertEqual(rest["id"], normalized["comment"]["database_id"])
        self.assertEqual(rest["node_id"], normalized["comment"]["id"])

    def test_live_outdated_comment_preserves_proven_right_side(self):
        current = publication_pair([], head=HEAD_SHA, run_id=RUN_ID, review_id=900)
        prior = publication_pair(
            [finding()], head=PRIOR_HEAD, run_id=PRIOR_RUN_ID, review_id=PRIOR_REVIEW_ID
        )
        fake = FakeGitHub(current, prior)
        fake.thread = graphql_thread(outdated=True)
        fake.thread.update(
            {
                "id": "PRRT_kwDOOl1N5c6Zb1nU",
                "line": None,
                "originalLine": 7,
            }
        )
        fake.comment.update(
            {
                "line": None,
                "side": "RIGHT",
                "original_line": 7,
                "original_side": None,
            }
        )

        with (
            patch.object(review_resolver, "GitHubClient", return_value=fake),
            patch.object(
                review_resolver,
                "collect_provenance",
                return_value=[
                    review_resolution.validate_inline_provenance(
                        prior[0],
                        prior[1],
                        repository=REPOSITORY,
                        pull_number=PULL_NUMBER,
                    )
                ],
            ),
        ):
            packet = review_resolver.prepare(
                repository=REPOSITORY,
                pull_number=PULL_NUMBER,
                run_id=RUN_ID,
                workflow_sha="e" * 40,
                current_result=current[0],
                current_receipt=current[1],
                token="read-token",
            )

        self.assertEqual(packet["status"], "ready")
        self.assertEqual(packet["candidate_count"], 1)
        self.assertEqual(packet["candidates"][0]["thread_id"], fake.thread["id"])
        self.assertEqual(packet["candidates"][0]["comment_snapshot"]["line"], 7)
        self.assertEqual(
            packet["candidates"][0]["comment_snapshot"]["side"], "RIGHT"
        )

    def test_outdated_comment_side_evidence_fails_closed(self):
        current = publication_pair([], head=HEAD_SHA, run_id=RUN_ID, review_id=900)
        prior = publication_pair(
            [finding()], head=PRIOR_HEAD, run_id=PRIOR_RUN_ID, review_id=PRIOR_REVIEW_ID
        )
        base = FakeGitHub(current, prior).comment
        base.update({"line": None, "original_line": 7})

        variants = (
            {"side": "LEFT", "original_side": None},
            {"side": "RIGHT", "original_side": "LEFT"},
            {"side": None, "original_side": None},
            {"side": "RIGHT", "original_side": None, "original_line": None},
        )
        for changes in variants:
            with self.subTest(changes=changes):
                comment = copy.deepcopy(base)
                comment.update(changes)
                self.assertIsNone(review_resolver._modern_comment_snapshot(comment))

    def test_outdated_range_defers_original_side_proof_to_exact_rest_comment(self):
        thread = graphql_thread(outdated=True)
        thread.update(
            {
                "line": None,
                "startLine": None,
                "originalLine": 9,
                "originalStartLine": 7,
            }
        )

        normalized = review_resolver.normalize_thread(thread)

        self.assertIsNotNone(normalized)
        self.assertEqual(normalized["line"], 9)
        self.assertEqual(normalized["start_line"], 7)
        self.assertEqual(normalized["start_side"], "RIGHT")


class ResolutionProvenanceTests(unittest.TestCase):
    def test_prior_artifact_is_bound_to_exact_run_and_reusable_workflow(self):
        current = publication_pair([], head=HEAD_SHA, run_id=RUN_ID, review_id=900)
        prior = publication_pair(
            [finding()],
            head=PRIOR_HEAD,
            run_id=PRIOR_RUN_ID,
            review_id=PRIOR_REVIEW_ID,
        )

        def run(run_id, head, *, status, conclusion):
            sha = "e" * 40
            return {
                "id": run_id,
                "event": "pull_request_target",
                "head_sha": head,
                "status": status,
                "conclusion": conclusion,
                "path": ".github/workflows/codex-code-review.yml",
                "repository": {"full_name": REPOSITORY},
                "referenced_workflows": [
                    {
                        "sha": sha,
                        "path": (
                            f"{review_resolver.review_contract.EXPECTED_WORKFLOW_PATH}"
                            f"@{sha}"
                        ),
                    }
                ],
            }

        class ArtifactGitHub:
            def request(self, method, path, payload=None):
                if path.endswith(f"/actions/runs/{RUN_ID}"):
                    return run(RUN_ID, HEAD_SHA, status="in_progress", conclusion=None)
                if path.endswith(f"/actions/runs/{PRIOR_RUN_ID}"):
                    return run(
                        PRIOR_RUN_ID,
                        PRIOR_HEAD,
                        status="completed",
                        conclusion="failure",
                    )
                if "/actions/artifacts?" in path:
                    return {
                        "artifacts": [
                            {
                                "id": 700,
                                "name": "codex-review-result",
                                "expired": False,
                                "workflow_run": {
                                    "id": PRIOR_RUN_ID,
                                    "head_sha": PRIOR_HEAD,
                                },
                            }
                        ]
                    }
                raise AssertionError((method, path, payload))

            def request_bytes(self, method, path, payload=None):
                output = io.BytesIO()
                with zipfile.ZipFile(output, "w") as archive:
                    archive.writestr(
                        "codex-review-result.json", json.dumps(prior[0])
                    )
                    archive.writestr(
                        "publication-receipt.json", json.dumps(prior[1])
                    )
                return output.getvalue()

        proven = review_resolver.collect_provenance(
            ArtifactGitHub(),
            REPOSITORY,
            PULL_NUMBER,
            RUN_ID,
            current[0],
            current[1],
            "e" * 40,
        )

        self.assertEqual(len(proven), 1)
        self.assertEqual(proven[0][1]["actions_run_id"], PRIOR_RUN_ID)


class ResolutionApplyTests(unittest.TestCase):
    def test_keep_only_plan_needs_no_dancer_authority(self):
        current, _, fake, packet, plan = prepared_state()
        plan["decisions"][0]["decision"] = "KEEP_AMBIGUOUS"

        with patch.object(review_resolver, "GitHubClient", return_value=fake):
            receipt = review_resolver.apply(
                repository=REPOSITORY,
                pull_number=PULL_NUMBER,
                run_id=RUN_ID,
                current_result=current[0],
                current_receipt=current[1],
                packet=packet,
                plan=plan,
                read_token="read-token",
                dancer_token="",
            )

        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(fake.mutation_calls, 0)
        self.assertEqual(receipt["results"][0]["mutation"], "untouched")

    def test_one_fixed_mutation_and_exact_readback(self):
        current, _, fake, packet, plan = prepared_state()

        with patch.object(review_resolver, "GitHubClient", return_value=fake):
            receipt = review_resolver.apply(
                repository=REPOSITORY,
                pull_number=PULL_NUMBER,
                run_id=RUN_ID,
                current_result=current[0],
                current_receipt=current[1],
                packet=packet,
                plan=plan,
                read_token="read-token",
                dancer_token="dancer-token",
            )

        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(fake.mutation_calls, 1)
        self.assertTrue(receipt["results"][0]["is_resolved"])
        self.assertEqual(
            receipt["actor"],
            {
                "login": review_publisher.EXPECTED_DANCER_LOGIN,
                "id": review_publisher.EXPECTED_DANCER_ACTOR_ID,
            },
        )

    def test_live_schema_uses_only_user_shaped_resolved_by_evidence(self):
        self.assertIn(
            "resolvedBy {\n        login\n      }",
            review_resolver.THREAD_READBACK_QUERY,
        )
        self.assertIn(
            "resolvedBy {\n        login\n      }", review_resolver.RESOLVE_MUTATION
        )
        self.assertNotIn("... on Bot", review_resolver.THREAD_READBACK_QUERY)
        self.assertNotIn("... on Bot", review_resolver.RESOLVE_MUTATION)

    def test_ambiguous_response_reads_back_without_retry(self):
        current, _, fake, packet, plan = prepared_state()
        fake.mutation_raises = True
        fake.resolve_on_error = True

        with patch.object(review_resolver, "GitHubClient", return_value=fake):
            receipt = review_resolver.apply(
                repository=REPOSITORY,
                pull_number=PULL_NUMBER,
                run_id=RUN_ID,
                current_result=current[0],
                current_receipt=current[1],
                packet=packet,
                plan=plan,
                read_token="read-token",
                dancer_token="dancer-token",
            )

        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(fake.mutation_calls, 1)
        self.assertEqual(receipt["results"][0]["mutation"], "ambiguous_response")

    def test_http_422_fails_closed_without_retry(self):
        current, _, fake, packet, plan = prepared_state()
        fake.mutation_raises = True
        fake.mutation_error_status = 422

        with patch.object(review_resolver, "GitHubClient", return_value=fake):
            receipt = review_resolver.apply(
                repository=REPOSITORY,
                pull_number=PULL_NUMBER,
                run_id=RUN_ID,
                current_result=current[0],
                current_receipt=current[1],
                packet=packet,
                plan=plan,
                read_token="read-token",
                dancer_token="dancer-token",
            )

        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["error"], "RESOLUTION_MUTATION_REJECTED")
        self.assertEqual(fake.mutation_calls, 1)

    def test_receipt_omits_untrusted_model_reason(self):
        current, _, fake, packet, plan = prepared_state()
        plan["decisions"][0]["decision"] = "KEEP_AMBIGUOUS"
        plan["decisions"][0]["reason"] = "raw model prose must not persist"

        with patch.object(review_resolver, "GitHubClient", return_value=fake):
            receipt = review_resolver.apply(
                repository=REPOSITORY,
                pull_number=PULL_NUMBER,
                run_id=RUN_ID,
                current_result=current[0],
                current_receipt=current[1],
                packet=packet,
                plan=plan,
                read_token="read-token",
                dancer_token="",
            )

        self.assertNotIn("reason", receipt["results"][0])
        self.assertEqual(
            receipt["results"][0]["prior_fingerprint"],
            packet["candidates"][0]["provenance"]["fingerprint"],
        )

    def test_thread_drift_causes_zero_mutations(self):
        current, _, fake, packet, plan = prepared_state()
        fake.thread["line"] = 8

        with patch.object(review_resolver, "GitHubClient", return_value=fake):
            receipt = review_resolver.apply(
                repository=REPOSITORY,
                pull_number=PULL_NUMBER,
                run_id=RUN_ID,
                current_result=current[0],
                current_receipt=current[1],
                packet=packet,
                plan=plan,
                read_token="read-token",
                dancer_token="dancer-token",
            )

        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(fake.mutation_calls, 0)

    def test_head_change_during_final_proof_has_zero_mutations(self):
        current, _, fake, packet, plan = prepared_state()
        original_proof = review_resolver._prove_candidate_live
        proof_calls = 0

        def proof_then_drift(*args, **kwargs):
            nonlocal proof_calls
            original_proof(*args, **kwargs)
            proof_calls += 1
            if proof_calls == 3:
                fake.head_sha = "f" * 40

        with (
            patch.object(review_resolver, "GitHubClient", return_value=fake),
            patch.object(
                review_resolver,
                "_prove_candidate_live",
                side_effect=proof_then_drift,
            ),
        ):
            receipt = review_resolver.apply(
                repository=REPOSITORY,
                pull_number=PULL_NUMBER,
                run_id=RUN_ID,
                current_result=current[0],
                current_receipt=current[1],
                packet=packet,
                plan=plan,
                read_token="read-token",
                dancer_token="dancer-token",
            )

        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["error"], "RESOLUTION_GENERATION_DRIFT")
        self.assertEqual(fake.mutation_calls, 0)

    def test_head_change_after_readback_never_claims_completed(self):
        current, _, fake, packet, plan = prepared_state()
        fake.drift_after_resolved_readback = True

        with patch.object(review_resolver, "GitHubClient", return_value=fake):
            receipt = review_resolver.apply(
                repository=REPOSITORY,
                pull_number=PULL_NUMBER,
                run_id=RUN_ID,
                current_result=current[0],
                current_receipt=current[1],
                packet=packet,
                plan=plan,
                read_token="read-token",
                dancer_token="dancer-token",
            )

        self.assertEqual(fake.mutation_calls, 1)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["error"], "RESOLUTION_GENERATION_DRIFT")

    def test_overflow_never_constructs_client_or_mutates(self):
        current = publication_pair([], head=HEAD_SHA, run_id=RUN_ID, review_id=900)
        packet = {
            "schema_version": review_resolution.CANDIDATE_PACKET_VERSION,
            "status": "overflow",
            "repository": REPOSITORY,
            "pull_number": PULL_NUMBER,
            "actions_run_id": RUN_ID,
            "workflow_revision": "e" * 40,
            "current_generation": {
                "head_sha": HEAD_SHA,
                "base_ref": "master",
                "base_sha": BASE_SHA,
            },
            "current_publication": {
                "actions_run_id": RUN_ID,
                "review_id": 900,
                "request_sha256": current[1]["review"]["request_sha256"],
                "result_sha256": review_resolution.canonical_sha256(current[0]),
                "receipt_sha256": review_resolution.canonical_sha256(current[1]),
            },
            "candidate_count": 0,
            "model_candidate_count": 0,
            "excluded": {},
            "observations": [],
            "candidates": [],
            "overflow_count": 21,
        }
        plan = review_resolution.build_plan(packet, None)

        with patch.object(review_resolver, "GitHubClient") as client:
            receipt = review_resolver.apply(
                repository=REPOSITORY,
                pull_number=PULL_NUMBER,
                run_id=RUN_ID,
                current_result=current[0],
                current_receipt=current[1],
                packet=packet,
                plan=plan,
                read_token="read-token",
                dancer_token="dancer-token",
            )

        client.assert_not_called()
        self.assertEqual(receipt["status"], "overflow")
        self.assertEqual(receipt["resolve_count"], 0)


if __name__ == "__main__":
    unittest.main()
