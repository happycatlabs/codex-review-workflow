from __future__ import annotations

import copy
import pathlib
import sys
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import review_publication  # noqa: E402
import review_publisher  # noqa: E402


REPOSITORY = "happycatlabs/fable"
PULL_NUMBER = 205
RUN_ID = 123456789
HEAD_SHA = "a" * 40
BASE_SHA = "b" * 40
NEXT_HEAD_SHA = "c" * 40
REPOSITORY_ID = 979193317


def finding() -> dict:
    return {
        "severity": "BUG",
        "blocking": True,
        "file": "lib/example.ts",
        "start_line": 42,
        "line": 42,
        "title": "Wrong fallback",
        "body": "The fallback now returns the wrong value for signed-out users.",
        "fingerprint": "d" * 64,
    }


def result_fixture(
    findings: list[dict] | None = None,
    *,
    base_ref: str = "master",
    base_sha: str = BASE_SHA,
) -> dict:
    findings = findings or []
    return {
        "schema_version": "codex-review-result/v3",
        "verdict": "blocking_findings" if findings else "clean",
        "pull_number": PULL_NUMBER,
        "head_sha": HEAD_SHA,
        "base_ref": base_ref,
        "base_sha": base_sha,
        "state": "open",
        "review_scope": "source_context_v1",
        "coverage": {
            "complete": True,
            "truncated": False,
            "diff_bytes_included": 10,
            "source_context_bytes": 20,
        },
        "lookup_context": {"complete": True},
        "summary": "One concrete production regression." if findings else "No issues found.",
        "findings": findings,
        "blocking_count": len(findings),
        "non_blocking_count": 0,
        "finding_fingerprints": [item["fingerprint"] for item in findings],
        "workflow_revision": "e" * 40,
        "reviewer_revision": "reviewer",
        "error": None,
        "publication": {
            "status": "pending",
            "mode": "inline" if findings else "summary",
            "fallback_reason": None,
            "inline_comment_count": 0,
        },
    }


def publication_plan(
    findings: list[dict] | None = None,
    *,
    base_ref: str = "master",
    base_sha: str = BASE_SHA,
):
    result = result_fixture(findings, base_ref=base_ref, base_sha=base_sha)
    comment_map = {
        "schema_version": review_publication.COMMENT_MAP_VERSION,
        "complete": True,
        "pull_number": PULL_NUMBER,
        "head_sha": HEAD_SHA,
        "base_ref": base_ref,
        "base_sha": base_sha,
        "diff_sha256": "f" * 64,
        "files": {"lib/example.ts": [[40, 45]]},
    }
    return review_publication.plan_publication(
        result,
        comment_map,
        repository=REPOSITORY,
        run_id=RUN_ID,
    )


class FakeGitHubClient:
    def __init__(self):
        self.actor = {
            "login": review_publisher.EXPECTED_DANCER_LOGIN,
            "id": review_publisher.EXPECTED_DANCER_ACTOR_ID,
        }
        self.head_sha = HEAD_SHA
        self.base_sha = BASE_SHA
        self.reviews: list[dict] = []
        self.comments: dict[int, list[dict]] = {}
        self.post_count = 0
        self.inline_422_once = False
        self.accept_then_fail = False
        self.drop_comments_on_readback = False
        self.hidden_comment_readbacks = 0
        self.overflow_comment_readback = False
        self.corrupt_comment_reference: str | None = None
        self.fail_exact_comment_readback_status: int | None = None
        self.null_exact_comment_readbacks = 0
        self.corrupt_exact_comment: str | None = None
        self.change_head_at_pull_read: int | None = None
        self.fail_commit_read_at: int | None = None
        self.fail_review_list = False
        self.fail_review_readback = False
        self.pull_reads = 0
        self.commit_reads = 0
        self.comment_reads = 0
        self.review_read_paths: list[str] = []
        self.comment_read_paths: list[str] = []
        self.comment_reference_readbacks: list[list[dict]] = []
        self.exact_comment_read_paths: list[str] = []

    def request(self, method: str, path: str, payload=None):
        if method == "POST" and path == "/graphql":
            return {
                "data": {
                    "viewer": {
                        "login": self.actor["login"],
                        "databaseId": self.actor["id"],
                    }
                }
            }
        if method == "GET" and path == "/repos/happycatlabs/fable":
            return {
                "id": REPOSITORY_ID,
                "full_name": REPOSITORY,
                "default_branch": "master",
            }
        if method == "GET" and path == "/repos/happycatlabs/fable/pulls/205":
            self.pull_reads += 1
            if (
                self.change_head_at_pull_read is not None
                and self.pull_reads >= self.change_head_at_pull_read
            ):
                self.head_sha = NEXT_HEAD_SHA
            return {
                "number": PULL_NUMBER,
                "state": "open",
                "merged_at": None,
                "user": copy.deepcopy(self.actor),
                "head": {
                    "ref": "codex/test-head",
                    "sha": self.head_sha,
                    "repo": {"id": REPOSITORY_ID, "full_name": REPOSITORY},
                },
                "base": {
                    "ref": "master",
                    "sha": self.base_sha,
                    "repo": {"id": REPOSITORY_ID, "full_name": REPOSITORY},
                },
                "stack": None,
            }
        if method == "GET" and path == "/repos/happycatlabs/fable/commits/master":
            self.commit_reads += 1
            if (
                self.fail_commit_read_at is not None
                and self.commit_reads >= self.fail_commit_read_at
            ):
                raise review_publisher.GitHubApiError(422, "lookup rejected")
            return {"sha": self.base_sha}
        if method == "GET" and "/reviews?" in path:
            if self.fail_review_list:
                raise review_publisher.GitHubApiError(422, "evidence rejected")
            page = int(path.rsplit("page=", 1)[1])
            return copy.deepcopy(self.reviews if page == 1 else [])
        if method == "POST" and path == "/repos/happycatlabs/fable/pulls/205/reviews":
            self.post_count += 1
            if self.inline_422_once and payload.get("comments"):
                self.inline_422_once = False
                raise review_publisher.GitHubApiError(422, "validation failed")
            review_id = 900 + self.post_count
            review = {
                "id": review_id,
                "state": "COMMENTED",
                "body": payload["body"],
                "commit_id": payload.get("commit_id", self.head_sha),
                "html_url": (
                    f"https://github.com/{REPOSITORY}/pull/{PULL_NUMBER}"
                    f"#pullrequestreview-{review_id}"
                ),
                "user": copy.deepcopy(self.actor),
            }
            self.reviews.append(review)
            self.comments[review_id] = [
                {
                    "id": review_id * 100 + index,
                    **copy.deepcopy(comment),
                    "pull_request_review_id": review_id,
                    "commit_id": payload.get("commit_id", self.head_sha),
                    "user": copy.deepcopy(self.actor),
                }
                for index, comment in enumerate(payload.get("comments", []), start=1)
            ]
            if self.accept_then_fail:
                self.accept_then_fail = False
                raise review_publisher.GitHubApiError(0, "response lost")
            return copy.deepcopy(review)
        if method == "GET" and "/comments?" in path:
            self.comment_read_paths.append(path)
            review_id = int(path.split("/reviews/", 1)[1].split("/", 1)[0])
            page = int(path.rsplit("page=", 1)[1])
            if self.overflow_comment_readback:
                return [
                    {
                        "id": review_id * 100_000 + page * 1_000 + index,
                        "pull_request_review_id": review_id,
                    }
                    for index in range(review_publisher.PAGE_SIZE)
                ]
            if page > 1:
                return []
            self.comment_reads += 1
            if self.hidden_comment_readbacks > 0:
                self.hidden_comment_readbacks -= 1
                return []
            if self.drop_comments_on_readback:
                return []
            references = [
                {
                    "id": comment["id"],
                    "pull_request_review_id": comment["pull_request_review_id"],
                    "path": comment["path"],
                    "position": 9,
                    "original_position": 9,
                }
                for comment in self.comments[review_id]
            ]
            if references and self.corrupt_comment_reference == "id":
                references[0]["id"] = "invalid"
            if references and self.corrupt_comment_reference == "review":
                references[0]["pull_request_review_id"] = review_id + 1
            self.comment_reference_readbacks.append(copy.deepcopy(references))
            return copy.deepcopy(references)
        if method == "GET" and "/pulls/comments/" in path:
            self.exact_comment_read_paths.append(path)
            if self.fail_exact_comment_readback_status is not None:
                raise review_publisher.GitHubApiError(
                    self.fail_exact_comment_readback_status, "comment readback rejected"
                )
            if self.null_exact_comment_readbacks > 0:
                self.null_exact_comment_readbacks -= 1
                return None
            comment_id = int(path.rsplit("/", 1)[1])
            actual = copy.deepcopy(
                next(
                    comment
                    for comments in self.comments.values()
                    for comment in comments
                    if comment["id"] == comment_id
                )
            )
            if self.corrupt_exact_comment == "id":
                actual["id"] = comment_id + 1
            elif self.corrupt_exact_comment == "review":
                actual["pull_request_review_id"] += 1
            elif self.corrupt_exact_comment == "commit":
                actual["commit_id"] = NEXT_HEAD_SHA
            elif self.corrupt_exact_comment == "coordinates":
                actual["line"] += 1
            elif self.corrupt_exact_comment == "body":
                actual["body"] += " changed"
            elif self.corrupt_exact_comment == "actor":
                actual["user"] = {"login": "github-actions[bot]", "id": 41898282}
            return actual
        if method == "GET" and "/reviews/" in path:
            self.review_read_paths.append(path)
            if self.fail_review_readback:
                raise review_publisher.GitHubApiError(422, "readback rejected")
            review_id = int(path.rsplit("/", 1)[1])
            return copy.deepcopy(
                next(review for review in self.reviews if review["id"] == review_id)
            )
        raise AssertionError(f"unexpected request: {method} {path}")


class FakeStackedGitHubClient(FakeGitHubClient):
    def __init__(self):
        super().__init__()
        self.default_sha = "9" * 40
        self.parent_ref = "codex/parent"
        self.stack_number = 269
        self.parent_draft = False
        self.target_draft = False
        self.descendant_head_sha: str | None = None
        self.descendant_draft = False

    def _raw_node(
        self,
        *,
        number: int,
        base_ref: str,
        base_sha: str,
        head_ref: str,
        head_sha: str,
        draft: bool = False,
    ) -> dict:
        return {
            "number": number,
            "state": "open",
            "merged_at": None,
            "draft": draft,
            "user": copy.deepcopy(self.actor),
            "base": {
                "ref": base_ref,
                "sha": base_sha,
                "repo": {"id": REPOSITORY_ID, "full_name": REPOSITORY},
            },
            "head": {
                "ref": head_ref,
                "sha": head_sha,
                "repo": {"id": REPOSITORY_ID, "full_name": REPOSITORY},
            },
        }

    def request(self, method: str, path: str, payload=None):
        if method == "GET" and path == "/repos/happycatlabs/fable":
            return {
                "id": REPOSITORY_ID,
                "full_name": REPOSITORY,
                "default_branch": "master",
            }
        if method == "GET" and path == "/repos/happycatlabs/fable/commits/master":
            self.commit_reads += 1
            return {"sha": self.default_sha}
        if method == "GET" and path == "/repos/happycatlabs/fable/pulls/205":
            self.pull_reads += 1
            return {
                **self._raw_node(
                    number=PULL_NUMBER,
                    base_ref=self.parent_ref,
                    base_sha=BASE_SHA,
                    head_ref="codex/child",
                    head_sha=self.head_sha,
                    draft=self.target_draft,
                ),
                "stack": {
                    "number": self.stack_number,
                    "position": 2,
                    "size": 2,
                    "base": {"ref": "master", "sha": self.default_sha},
                },
            }
        if (
            method == "GET"
            and path
            == f"/repos/happycatlabs/fable/stacks/{self.stack_number}"
        ):
            pull_requests = [
                self._raw_node(
                    number=204,
                    base_ref="master",
                    base_sha=self.default_sha,
                    head_ref=self.parent_ref,
                    head_sha=BASE_SHA,
                    draft=self.parent_draft,
                ),
                self._raw_node(
                    number=PULL_NUMBER,
                    base_ref=self.parent_ref,
                    base_sha=BASE_SHA,
                    head_ref="codex/child",
                    head_sha=self.head_sha,
                    draft=self.target_draft,
                ),
            ]
            if self.descendant_head_sha is not None:
                pull_requests.append(
                    self._raw_node(
                        number=206,
                        base_ref="codex/child",
                        base_sha=self.head_sha,
                        head_ref="codex/descendant",
                        head_sha=self.descendant_head_sha,
                        draft=self.descendant_draft,
                    )
                )
            return {
                "number": self.stack_number,
                "open": True,
                "base": {"ref": "master"},
                "pull_requests": pull_requests,
            }
        return super().request(method, path, payload)


class FakeRetainedMergedPrefixGitHubClient(FakeStackedGitHubClient):
    def request(self, method: str, path: str, payload=None):
        if (
            method == "GET"
            and path
            == f"/repos/happycatlabs/fable/stacks/{self.stack_number}"
        ):
            active = super().request(method, path, payload)
            merged = self._raw_node(
                number=203,
                base_ref="master",
                base_sha="7" * 40,
                head_ref="codex/merged-lower",
                head_sha="8" * 40,
            )
            merged["state"] = "closed"
            merged["merged_at"] = "2026-08-15T09:42:02Z"
            return {**active, "pull_requests": [merged, *active["pull_requests"]]}
        return super().request(method, path, payload)


def prepared_base_provenance(fake: FakeGitHubClient) -> dict:
    base_provenance = review_publisher.current_generation(
        fake, REPOSITORY, PULL_NUMBER
    )["base_provenance"]
    fake.pull_reads = 0
    fake.commit_reads = 0
    return base_provenance


def publish_with(fake: FakeGitHubClient, findings: list[dict] | None = None):
    result, request, summary = publication_plan(findings)
    base_provenance = prepared_base_provenance(fake)
    with patch.object(review_publisher, "GitHubClient", return_value=fake):
        return review_publisher.publish(
            result=result,
            request=request,
            summary_request=summary,
            repository=REPOSITORY,
            run_id=RUN_ID,
            token="short-lived-token",
            base_provenance=base_provenance,
        )


class DancerPublisherTests(unittest.TestCase):
    def test_three_layer_native_stack_retains_merged_prefix_but_proves_active_child(self):
        fake = FakeRetainedMergedPrefixGitHubClient()
        result, request, summary = publication_plan(base_ref=fake.parent_ref)
        prepared = prepared_base_provenance(fake)

        with patch.object(review_publisher, "GitHubClient", return_value=fake):
            result, receipt = review_publisher.publish(
                result=result,
                request=request,
                summary_request=summary,
                repository=REPOSITORY,
                run_id=RUN_ID,
                token="short-lived-token",
                base_provenance=prepared,
            )

        self.assertEqual(result["publication"]["status"], "published")
        self.assertEqual(prepared["stack"]["size"], 2)
        self.assertEqual(
            [node["number"] for node in prepared["stack"]["nodes"]],
            [204, PULL_NUMBER],
        )
        self.assertEqual(receipt["observed_generation"]["base_provenance"], prepared)
        self.assertEqual(fake.post_count, 1)

    def test_stacked_topology_drift_has_zero_publication_mutations(self):
        fake = FakeStackedGitHubClient()
        result, request, summary = publication_plan(base_ref=fake.parent_ref)
        prepared = prepared_base_provenance(fake)
        fake.stack_number += 1

        with patch.object(review_publisher, "GitHubClient", return_value=fake):
            result, receipt = review_publisher.publish(
                result=result,
                request=request,
                summary_request=summary,
                repository=REPOSITORY,
                run_id=RUN_ID,
                token="short-lived-token",
                base_provenance=prepared,
            )

        self.assertEqual(result["publication"]["status"], "failed")
        self.assertEqual(
            result["publication"]["fallback_reason"], "STALE_BEFORE_PUBLICATION"
        )
        self.assertIsNone(receipt["review"])
        self.assertEqual(fake.post_count, 0)

    def test_descendant_changes_do_not_perturb_target_generation(self):
        fake = FakeStackedGitHubClient()
        fake.descendant_head_sha = "e" * 40
        result, request, summary = publication_plan(base_ref=fake.parent_ref)
        prepared = prepared_base_provenance(fake)
        fake.descendant_head_sha = "f" * 40
        fake.descendant_draft = True
        fake.parent_draft = True
        fake.target_draft = True

        with patch.object(review_publisher, "GitHubClient", return_value=fake):
            result, receipt = review_publisher.publish(
                result=result,
                request=request,
                summary_request=summary,
                repository=REPOSITORY,
                run_id=RUN_ID,
                token="short-lived-token",
                base_provenance=prepared,
            )

        self.assertEqual(result["publication"]["status"], "published")
        self.assertEqual(
            [node["number"] for node in prepared["stack"]["nodes"]],
            [204, PULL_NUMBER],
        )
        self.assertEqual(receipt["observed_generation"]["base_provenance"], prepared)
        self.assertEqual(fake.post_count, 1)

    def test_stacked_non_ancestry_error_has_zero_publication_mutations(self):
        fake = FakeStackedGitHubClient()
        result, request, summary = publication_plan(base_ref=fake.parent_ref)
        prepared = prepared_base_provenance(fake)
        result["verdict"] = "error"
        result["error"] = {
            "code": "BASE_NOT_ANCESTOR",
            "reason": "The active stack dependency chain is not ancestral.",
        }

        with patch.object(
            review_publisher, "GitHubClient", return_value=fake
        ) as client_class:
            result, receipt = review_publisher.publish(
                result=result,
                request=request,
                summary_request=summary,
                repository=REPOSITORY,
                run_id=RUN_ID,
                token="short-lived-token",
                base_provenance=prepared,
            )

        client_class.assert_not_called()
        self.assertEqual(result["publication"]["status"], "failed")
        self.assertEqual(
            result["publication"]["fallback_reason"], "BASE_NOT_ANCESTOR"
        )
        self.assertIsNone(receipt["actor"])
        self.assertIsNone(receipt["observed_generation"])
        self.assertIsNone(receipt["review"])
        self.assertEqual(fake.post_count, 0)

    def test_non_comment_event_in_either_request_has_zero_authority(self):
        for target_name in ("request", "summary"):
            for event in ("APPROVE", "REQUEST_CHANGES"):
                with self.subTest(target=target_name, event=event):
                    fake = FakeGitHubClient()
                    result, request, summary = publication_plan([finding()])
                    target = request if target_name == "request" else summary
                    target["event"] = event

                    with patch.object(
                        review_publisher, "GitHubClient", return_value=fake
                    ):
                        published_result, receipt = review_publisher.publish(
                            result=result,
                            request=request,
                            summary_request=summary,
                            repository=REPOSITORY,
                            run_id=RUN_ID,
                            token="short-lived-token",
                            base_provenance=prepared_base_provenance(fake),
                        )

                    self.assertEqual(
                        published_result["publication"]["fallback_reason"],
                        "PUBLICATION_REQUEST_INVALID",
                    )
                    self.assertEqual(fake.post_count, 0)
                    self.assertIsNone(receipt["actor"])
                    self.assertIsNone(receipt["review"])

    def test_missing_dancer_authority_fails_without_mutation(self):
        result, request, summary = publication_plan()

        result, receipt = review_publisher.publish(
            result=result,
            request=request,
            summary_request=summary,
            repository=REPOSITORY,
            run_id=RUN_ID,
            token="",
            base_provenance=prepared_base_provenance(FakeGitHubClient()),
        )

        self.assertEqual(result["publication"]["status"], "failed")
        self.assertEqual(
            result["publication"]["fallback_reason"], "DANCER_AUTH_UNAVAILABLE"
        )
        self.assertIsNone(receipt["actor"])
        self.assertIsNone(receipt["review"])

    def test_clean_summary_is_dancer_authored_and_read_back(self):
        fake = FakeGitHubClient()

        result, receipt = publish_with(fake)

        self.assertEqual(result["publication"]["status"], "published")
        self.assertEqual(result["publication"]["mode"], "summary")
        self.assertEqual(receipt["actor"], fake.actor)
        self.assertEqual(receipt["event"], "COMMENT")
        self.assertEqual(receipt["review"]["reused"], False)
        self.assertEqual(fake.post_count, 1)
        self.assertIn(review_publisher.PUBLICATION_MARKER, fake.reviews[0]["body"])

    def test_inline_finding_and_comment_are_both_dancer_authored(self):
        fake = FakeGitHubClient()

        result, receipt = publish_with(fake, [finding()])

        self.assertEqual(result["publication"]["mode"], "inline")
        self.assertEqual(result["publication"]["inline_comment_count"], 1)
        review_id = receipt["review"]["id"]
        self.assertEqual(fake.comments[review_id][0]["user"], fake.actor)
        self.assertNotIn(finding()["fingerprint"], fake.comments[review_id][0]["body"])

    def test_run_31836590886_comment_readback_retries_without_reposting(self):
        # The live POST persisted both records, but GitHub's first comment-list GET
        # omitted the new inline comment before the same evidence became visible.
        fake = FakeGitHubClient()
        fake.hidden_comment_readbacks = 1

        with patch.object(review_publisher.time, "sleep") as sleep:
            result, receipt = publish_with(fake, [finding()])

        self.assertEqual(result["publication"]["status"], "published")
        self.assertEqual(result["publication"]["mode"], "inline")
        self.assertEqual(result["publication"]["inline_comment_count"], 1)
        self.assertEqual(fake.post_count, 1)
        self.assertEqual(fake.comment_reads, 2)
        sleep.assert_called_once_with(
            review_publisher.READBACK_RETRY_DELAYS_SECONDS[0]
        )
        self.assertEqual(receipt["review"]["reused"], False)

    def test_run_31838541417_uses_extended_readback_window_without_reposting(self):
        # The live inline review and comment remained absent from the strict
        # comment-list read through the original immediate + 1/2/4s window.
        # Create Review returns the review ID but not individual comment IDs,
        # so each retry uses that exact review and its scoped comment collection.
        fake = FakeGitHubClient()
        fake.hidden_comment_readbacks = 4

        with patch.object(review_publisher.time, "sleep") as sleep:
            result, receipt = publish_with(fake, [finding()])

        self.assertEqual(result["publication"]["status"], "published")
        self.assertEqual(result["publication"]["mode"], "inline")
        self.assertEqual(result["publication"]["inline_comment_count"], 1)
        self.assertEqual(fake.post_count, 1)
        self.assertEqual(fake.comment_reads, 5)
        self.assertEqual(
            [arguments.args[0] for arguments in sleep.call_args_list],
            [1, 2, 4, 8],
        )
        self.assertEqual(
            fake.review_read_paths,
            [f"/repos/happycatlabs/fable/pulls/205/reviews/901"] * 5,
        )
        self.assertEqual(
            fake.comment_read_paths,
            [
                "/repos/happycatlabs/fable/pulls/205/reviews/901/comments"
                "?per_page=100&page=1"
            ]
            * 5,
        )
        self.assertEqual(
            fake.exact_comment_read_paths,
            ["/repos/happycatlabs/fable/pulls/comments/90101"],
        )
        self.assertEqual(receipt["review"]["reused"], False)

    def test_run_31840325453_resolves_modern_coordinates_by_exact_comment_id(self):
        # GitHub's review-scoped comment list exposed only legacy position fields
        # in the live run. The exact comment resource carried line/side evidence.
        fake = FakeGitHubClient()

        result, receipt = publish_with(fake, [finding()])

        self.assertEqual(result["publication"]["status"], "published")
        self.assertEqual(result["publication"]["mode"], "inline")
        self.assertEqual(result["publication"]["inline_comment_count"], 1)
        self.assertEqual(fake.post_count, 1)
        reference = fake.comment_reference_readbacks[0][0]
        self.assertEqual(reference["position"], 9)
        self.assertNotIn("line", reference)
        self.assertNotIn("side", reference)
        review_id = receipt["review"]["id"]
        comment_id = fake.comments[review_id][0]["id"]
        self.assertEqual(
            fake.exact_comment_read_paths,
            [f"/repos/happycatlabs/fable/pulls/comments/{comment_id}"],
        )

    def test_invalid_review_comment_reference_fails_closed_without_reposting(self):
        for corruption in ("id", "review"):
            with self.subTest(corruption=corruption):
                fake = FakeGitHubClient()
                fake.corrupt_comment_reference = corruption

                with patch.object(review_publisher.time, "sleep") as sleep:
                    result, receipt = publish_with(fake, [finding()])

                self.assertEqual(result["publication"]["status"], "failed")
                self.assertEqual(
                    result["publication"]["fallback_reason"],
                    "PUBLICATION_READBACK_FAILED",
                )
                self.assertEqual(fake.post_count, 1)
                self.assertEqual(fake.exact_comment_read_paths, [])
                self.assertIsNone(receipt["review"])
                self.assertEqual(
                    sleep.call_count,
                    len(review_publisher.READBACK_RETRY_DELAYS_SECONDS),
                )

    def test_review_comment_reference_overflow_fails_closed_without_reposting(self):
        fake = FakeGitHubClient()
        fake.overflow_comment_readback = True

        with patch.object(review_publisher.time, "sleep") as sleep:
            result, receipt = publish_with(fake, [finding()])

        self.assertEqual(result["publication"]["status"], "failed")
        self.assertEqual(
            result["publication"]["fallback_reason"],
            "PUBLICATION_EVIDENCE_LIMIT_EXCEEDED",
        )
        self.assertEqual(fake.post_count, 1)
        self.assertEqual(fake.exact_comment_read_paths, [])
        self.assertIsNone(receipt["review"])
        sleep.assert_not_called()

    def test_exact_comment_404_or_422_fails_after_one_mutation_without_retry(self):
        for status in (404, 422):
            with self.subTest(status=status):
                fake = FakeGitHubClient()
                fake.fail_exact_comment_readback_status = status

                with patch.object(review_publisher.time, "sleep") as sleep:
                    result, receipt = publish_with(fake, [finding()])

                self.assertEqual(result["publication"]["status"], "failed")
                self.assertEqual(
                    result["publication"]["fallback_reason"],
                    "PUBLICATION_STATE_LOOKUP_FAILED",
                )
                self.assertEqual(fake.post_count, 1)
                self.assertEqual(len(fake.exact_comment_read_paths), 1)
                self.assertIsNone(receipt["review"])
                sleep.assert_not_called()

    def test_exact_comment_evidence_exhaustion_never_reposts(self):
        fake = FakeGitHubClient()
        fake.null_exact_comment_readbacks = 100

        with patch.object(review_publisher.time, "sleep") as sleep:
            result, receipt = publish_with(fake, [finding()])

        self.assertEqual(result["publication"]["status"], "failed")
        self.assertEqual(
            result["publication"]["fallback_reason"],
            "PUBLICATION_READBACK_FAILED",
        )
        self.assertEqual(fake.post_count, 1)
        self.assertEqual(
            len(fake.exact_comment_read_paths),
            len(review_publisher.READBACK_RETRY_DELAYS_SECONDS) + 1,
        )
        self.assertIsNone(receipt["review"])
        self.assertEqual(
            sleep.call_count,
            len(review_publisher.READBACK_RETRY_DELAYS_SECONDS),
        )

    def test_exact_comment_identity_body_head_and_anchor_stay_strict(self):
        for corruption in ("id", "review", "commit", "coordinates", "body"):
            with self.subTest(corruption=corruption):
                fake = FakeGitHubClient()
                fake.corrupt_exact_comment = corruption

                with patch.object(review_publisher.time, "sleep") as sleep:
                    result, receipt = publish_with(fake, [finding()])

                self.assertEqual(result["publication"]["status"], "failed")
                self.assertEqual(fake.post_count, 1)
                self.assertIsNone(receipt["review"])
                self.assertEqual(
                    sleep.call_count,
                    len(review_publisher.READBACK_RETRY_DELAYS_SECONDS),
                )

    def test_exact_comment_actor_mismatch_fails_without_retry_or_fallback(self):
        fake = FakeGitHubClient()
        fake.corrupt_exact_comment = "actor"

        with patch.object(review_publisher.time, "sleep") as sleep:
            result, receipt = publish_with(fake, [finding()])

        self.assertEqual(result["publication"]["status"], "failed")
        self.assertEqual(
            result["publication"]["fallback_reason"], "DANCER_ACTOR_MISMATCH"
        )
        self.assertEqual(fake.post_count, 1)
        self.assertIsNone(receipt["review"])
        sleep.assert_not_called()

    def test_identical_request_reuses_exact_review_and_comment_readback(self):
        fake = FakeGitHubClient()
        result, request, summary = publication_plan([finding()])
        base_provenance = prepared_base_provenance(fake)

        with patch.object(review_publisher, "GitHubClient", return_value=fake):
            first_result, first_receipt = review_publisher.publish(
                result=copy.deepcopy(result),
                request=request,
                summary_request=summary,
                repository=REPOSITORY,
                run_id=RUN_ID,
                token="short-lived-token",
                base_provenance=base_provenance,
            )
            second_result, second_receipt = review_publisher.publish(
                result=copy.deepcopy(result),
                request=request,
                summary_request=summary,
                repository=REPOSITORY,
                run_id=RUN_ID,
                token="short-lived-token",
                base_provenance=base_provenance,
            )

        self.assertEqual(first_result["publication"], second_result["publication"])
        self.assertEqual(first_receipt["review"]["id"], second_receipt["review"]["id"])
        self.assertTrue(second_receipt["review"]["reused"])
        self.assertEqual(fake.post_count, 1)

    def test_identical_request_revalidates_after_reuse_readback(self):
        fake = FakeGitHubClient()
        result, request, summary = publication_plan([finding()])
        base_provenance = prepared_base_provenance(fake)

        with patch.object(review_publisher, "GitHubClient", return_value=fake):
            review_publisher.publish(
                result=copy.deepcopy(result),
                request=request,
                summary_request=summary,
                repository=REPOSITORY,
                run_id=RUN_ID,
                token="short-lived-token",
                base_provenance=base_provenance,
            )
            fake.change_head_at_pull_read = 4
            reused_result, reused_receipt = review_publisher.publish(
                result=copy.deepcopy(result),
                request=request,
                summary_request=summary,
                repository=REPOSITORY,
                run_id=RUN_ID,
                token="short-lived-token",
                base_provenance=base_provenance,
            )

        self.assertEqual(reused_result["publication"]["status"], "published")
        self.assertEqual(reused_result["publication"]["mode"], "summary")
        self.assertEqual(
            reused_result["publication"]["fallback_reason"],
            "STALE_BEFORE_PUBLICATION",
        )
        self.assertFalse(reused_receipt["review"]["reused"])
        self.assertEqual(fake.post_count, 2)

    def test_lost_post_response_recovers_without_second_mutation(self):
        fake = FakeGitHubClient()
        fake.accept_then_fail = True

        result, receipt = publish_with(fake)

        self.assertEqual(result["publication"]["status"], "published")
        self.assertTrue(receipt["review"]["reused"])
        self.assertEqual(fake.post_count, 1)

    def test_wrong_actor_fails_without_actions_or_human_fallback(self):
        fake = FakeGitHubClient()
        fake.actor = {"login": "github-actions[bot]", "id": 41898282}

        result, receipt = publish_with(fake)

        self.assertEqual(result["publication"]["status"], "failed")
        self.assertEqual(
            result["publication"]["fallback_reason"], "DANCER_ACTOR_MISMATCH"
        )
        self.assertIsNone(receipt["review"])
        self.assertEqual(fake.post_count, 0)

    def test_stale_generation_publishes_complete_unbound_summary(self):
        fake = FakeGitHubClient()
        fake.head_sha = NEXT_HEAD_SHA

        result, receipt = publish_with(fake, [finding()])

        self.assertEqual(result["publication"]["status"], "published")
        self.assertEqual(result["publication"]["mode"], "summary")
        self.assertEqual(
            result["publication"]["fallback_reason"], "STALE_BEFORE_PUBLICATION"
        )
        self.assertEqual(result["verdict"], "error")
        self.assertIn("Wrong fallback", fake.reviews[0]["body"])
        self.assertIn("> [!CAUTION]", fake.reviews[0]["body"])
        self.assertNotIn(finding()["fingerprint"], fake.reviews[0]["body"])
        self.assertEqual(receipt["review"]["commit_id"], NEXT_HEAD_SHA)

    def test_stale_clean_generation_never_publishes_a_clean_note(self):
        fake = FakeGitHubClient()
        fake.head_sha = NEXT_HEAD_SHA

        result, receipt = publish_with(fake)

        self.assertEqual(result["publication"]["status"], "published")
        self.assertEqual(
            result["publication"]["fallback_reason"], "STALE_BEFORE_PUBLICATION"
        )
        body = fake.reviews[0]["body"]
        self.assertIn("> [!CAUTION]", body)
        self.assertIn("no current clean conclusion is available", body)
        self.assertNotIn("> [!NOTE]", body)
        self.assertNotIn("No issues found.", body)
        self.assertEqual(receipt["review"]["commit_id"], NEXT_HEAD_SHA)

    def test_inline_422_revalidates_and_publishes_complete_summary_once(self):
        fake = FakeGitHubClient()
        fake.inline_422_once = True

        result, receipt = publish_with(fake, [finding()])

        self.assertEqual(result["publication"]["status"], "published")
        self.assertEqual(result["publication"]["mode"], "summary")
        self.assertEqual(result["publication"]["fallback_reason"], "GITHUB_422")
        self.assertEqual(fake.post_count, 2)
        self.assertIn("Wrong fallback", fake.reviews[0]["body"])
        self.assertEqual(receipt["mode"], "summary")

    def test_lookup_or_evidence_422_never_authorizes_a_summary_mutation(self):
        cases = ("state", "evidence")
        for case in cases:
            with self.subTest(case=case):
                fake = FakeGitHubClient()
                if case == "state":
                    fake.fail_commit_read_at = 2
                else:
                    fake.fail_review_list = True

                result, receipt = publish_with(fake, [finding()])

                self.assertEqual(result["publication"]["status"], "failed")
                self.assertEqual(fake.post_count, 0)
                self.assertIsNone(receipt["review"])

    def test_readback_422_fails_after_one_inline_mutation_without_fallback(self):
        fake = FakeGitHubClient()
        fake.fail_review_readback = True

        with patch.object(review_publisher.time, "sleep") as sleep:
            result, receipt = publish_with(fake, [finding()])

        self.assertEqual(result["publication"]["status"], "failed")
        self.assertEqual(fake.post_count, 1)
        self.assertEqual(len(fake.reviews[0].get("body", "")) > 0, True)
        self.assertEqual(len(fake.reviews), 1)
        self.assertIsNone(receipt["review"])
        sleep.assert_not_called()

    def test_missing_inline_comment_readback_fails_closed(self):
        fake = FakeGitHubClient()
        fake.drop_comments_on_readback = True

        with patch.object(review_publisher.time, "sleep") as sleep:
            result, receipt = publish_with(fake, [finding()])

        self.assertEqual(result["publication"]["status"], "failed")
        self.assertEqual(
            result["publication"]["fallback_reason"], "PUBLICATION_READBACK_FAILED"
        )
        self.assertEqual(fake.post_count, 1)
        self.assertEqual(
            fake.comment_reads,
            len(review_publisher.READBACK_RETRY_DELAYS_SECONDS) + 1,
        )
        self.assertIsNone(receipt["review"])
        self.assertEqual(
            sleep.call_count,
            len(review_publisher.READBACK_RETRY_DELAYS_SECONDS),
        )

    def test_generation_change_during_evidence_read_uses_stale_summary(self):
        fake = FakeGitHubClient()
        fake.change_head_at_pull_read = 2

        result, receipt = publish_with(fake)

        self.assertEqual(result["publication"]["status"], "published")
        self.assertEqual(result["publication"]["mode"], "summary")
        self.assertEqual(
            result["publication"]["fallback_reason"], "STALE_BEFORE_PUBLICATION"
        )
        self.assertEqual(fake.post_count, 1)
        self.assertIn("> [!CAUTION]", fake.reviews[0]["body"])
        self.assertNotIn("> [!NOTE]", fake.reviews[0]["body"])
        self.assertIsNotNone(receipt["review"])


if __name__ == "__main__":
    unittest.main()
